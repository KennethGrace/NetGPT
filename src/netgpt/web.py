"""
The web page for netgpt-chat: the same conversation as the terminal, in a browser.

Anyone who can load the page can query the network, so every route requires a
token. The token arrives once in the link printed at startup; the server swaps
it for an HttpOnly, SameSite=Strict cookie and redirects to a clean URL. The page
is static (static/) under a strict Content Security Policy, and it renders model
output as escaped text, since tool results carry device data the model may echo.

There is one conversation, answered one question at a time, as Ollama answers
one request at a time anyway. A question's progress (thinking, text, tool calls)
streams to the page as newline-delimited JSON events. The answer keeps going if
the page goes away, and a reload replays the conversation from its history.
"""

from __future__ import annotations

import asyncio
import json
import secrets
from collections.abc import AsyncIterator, Awaitable, Callable
from importlib.resources import files
from typing import Any

import uvicorn
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse, RedirectResponse, Response, StreamingResponse
from starlette.routing import Route

from netgpt.chat import Chat, ChatError, ChatObserver

COOKIE = "netgpt_token"
COOKIE_MAX_AGE = 30 * 24 * 3600
MAX_QUESTION_CHARS = 8000

SECURITY_HEADERS = {
    "Content-Security-Policy": (
        "default-src 'none'; script-src 'self'; style-src 'self'; connect-src 'self'; "
        "img-src 'self' data:; base-uri 'none'; form-action 'none'; frame-ancestors 'none'"
    ),
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "no-referrer",
    "Cache-Control": "no-store",
}

SIGN_IN_PAGE = """<!doctype html>
<meta charset="utf-8"><title>NetGPT</title>
<p>Not signed in. Open the link that <code>netgpt-chat --web</code> printed when it started.</p>
"""

Event = dict[str, Any]
Endpoint = Callable[[Request], Awaitable[Response]]


class StreamObserver(ChatObserver):
    """Turns a question's progress into events for the page."""

    def __init__(self, events: asyncio.Queue[Event | None]) -> None:
        self._events = events

    def thinking(self, text: str) -> None:
        self._events.put_nowait({"type": "thinking", "text": text})

    def content(self, text: str) -> None:
        self._events.put_nowait({"type": "content", "text": text})

    def tool_call(self, name: str, arguments: dict[str, Any], result: str, is_error: bool) -> None:
        self._events.put_nowait(
            {"type": "tool", "name": name, "arguments": arguments, "result": result, "is_error": is_error}
        )


def conversation_events(messages: list[dict[str, Any]]) -> list[Event]:
    """
    Replay a conversation as the events the page renders, so a reload shows it again.
    History doesn't record which tool calls failed; their results say so in words.
    """
    events: list[Event] = []
    arguments: dict[str, dict[str, Any]] = {}
    for message in messages:
        if message["role"] == "user":
            events.append({"type": "user", "text": message["content"]})
        elif message["role"] == "assistant":
            if message.get("thinking"):
                events.append({"type": "thinking", "text": message["thinking"]})
            calls = message.get("tool_calls") or []
            for call in calls:
                arguments[call.get("id", "")] = call["function"].get("arguments") or {}
            if not calls:
                events.append({"type": "answer", "text": message["content"]})
        elif message["role"] == "tool":
            events.append(
                {
                    "type": "tool",
                    "name": message["tool_name"],
                    "arguments": arguments.get(message.get("tool_call_id", ""), {}),
                    "result": message["content"],
                    "is_error": False,
                }
            )
    return events


def build_app(chat: Chat, token: str) -> Starlette:
    """Build the web app serving one conversation, for browsers holding the token."""
    static = files("netgpt") / "static"
    assets = {name: (static / name).read_text(encoding="utf-8") for name in ("index.html", "app.js", "app.css")}
    expected = token.encode()
    busy = asyncio.Lock()
    running: set[asyncio.Task[None]] = set()

    def signed_in(request: Request) -> bool:
        return secrets.compare_digest(request.cookies.get(COOKIE, "").encode(), expected)

    def requires_sign_in(endpoint: Endpoint) -> Endpoint:
        async def guarded(request: Request) -> Response:
            if not signed_in(request):
                return JSONResponse({"error": "Not signed in."}, status_code=401, headers=SECURITY_HEADERS)
            return await endpoint(request)

        return guarded

    def asset(name: str, media_type: str) -> Endpoint:
        async def serve_asset(request: Request) -> Response:
            return Response(assets[name], media_type=media_type, headers=SECURITY_HEADERS)

        return serve_asset

    async def index(request: Request) -> Response:
        offered = request.query_params.get("token")
        if offered is not None and secrets.compare_digest(offered.encode(), expected):
            # Swap the token in the link for a cookie, and drop it from the address bar.
            response = RedirectResponse("./", status_code=303, headers=SECURITY_HEADERS)
            response.set_cookie(COOKIE, token, max_age=COOKIE_MAX_AGE, httponly=True, samesite="strict")
            return response
        if not signed_in(request):
            return HTMLResponse(SIGN_IN_PAGE, status_code=401, headers=SECURITY_HEADERS)
        return HTMLResponse(assets["index.html"], headers=SECURITY_HEADERS)

    async def conversation(request: Request) -> Response:
        return JSONResponse(
            {"model": chat.model, "think": chat.think, "busy": busy.locked(), "events": conversation_events(chat.messages)},
            headers=SECURITY_HEADERS,
        )

    async def answer(question: str, events: asyncio.Queue[Event | None]) -> None:
        """Answer in the background, so the answer survives the page going away. Holds the lock throughout."""
        try:
            text = await chat.ask(question, StreamObserver(events))
            events.put_nowait({"type": "answer", "text": text})
        except ChatError as error:
            events.put_nowait({"type": "error", "text": str(error)})
        finally:
            busy.release()
            events.put_nowait(None)

    async def stream(events: asyncio.Queue[Event | None]) -> AsyncIterator[str]:
        while (event := await events.get()) is not None:
            yield json.dumps(event) + "\n"

    async def ask(request: Request) -> Response:
        try:
            question = str((await request.json())["question"]).strip()
        except (ValueError, KeyError, TypeError):
            return JSONResponse({"error": "Send JSON with a question."}, status_code=400, headers=SECURITY_HEADERS)
        if not question or len(question) > MAX_QUESTION_CHARS:
            message = f"Questions must be 1 to {MAX_QUESTION_CHARS} characters."
            return JSONResponse({"error": message}, status_code=400, headers=SECURITY_HEADERS)
        # No await between the check and taking the lock, so two questions can't both get in.
        if busy.locked():
            message = "Still answering the previous question."
            return JSONResponse({"error": message}, status_code=409, headers=SECURITY_HEADERS)
        await busy.acquire()
        events: asyncio.Queue[Event | None] = asyncio.Queue()
        task = asyncio.create_task(answer(question, events))
        running.add(task)
        task.add_done_callback(running.discard)
        return StreamingResponse(stream(events), media_type="application/x-ndjson", headers=SECURITY_HEADERS)

    async def reset(request: Request) -> Response:
        if busy.locked():
            message = "Still answering; wait for the answer before starting over."
            return JSONResponse({"error": message}, status_code=409, headers=SECURITY_HEADERS)
        chat.reset()
        return Response(status_code=204, headers=SECURITY_HEADERS)

    return Starlette(
        routes=[
            Route("/", index),
            Route("/app.js", requires_sign_in(asset("app.js", "text/javascript"))),
            Route("/app.css", requires_sign_in(asset("app.css", "text/css"))),
            Route("/api/conversation", requires_sign_in(conversation)),
            Route("/api/ask", requires_sign_in(ask), methods=["POST"]),
            Route("/api/reset", requires_sign_in(reset), methods=["POST"]),
        ]
    )


async def serve(chat: Chat, host: str, port: int, token: str) -> None:
    """Serve the chat page until interrupted."""
    # No access log: the first request's URL carries the token.
    config = uvicorn.Config(build_app(chat, token), host=host, port=port, log_level="warning", access_log=False)
    await uvicorn.Server(config).serve()

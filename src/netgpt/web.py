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

When the model calls display, its tool event carries the view, and the page draws
it on the map (map.js, laid out by the vendored d3-force library). When it calls
chart, the event carries the chart, which the page draws in the conversation
(chart.js) and refreshes from /api/chart each time the poller has new samples.
While NetGPT polls, the map also asks /api/traffic for every port's latest rates
and recent errors, and draws them on the links.
"""

from __future__ import annotations

import asyncio
import json
import secrets
from collections.abc import AsyncIterator, Awaitable, Callable
from importlib.resources import files
from typing import Any

import uvicorn
from pydantic import BaseModel, Field, ValidationError
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse, RedirectResponse, Response, StreamingResponse
from starlette.routing import Route

from netgpt.chat import Chat, ChatError, ChatObserver
from netgpt.statistics import MAX_CHART_PORTS, ChartError, Metric, Poller

COOKIE = "netgpt_token"
DISPLAY_TOOL = "display"
CHART_TOOL = "chart"
ASSETS = {
    "index.html": "text/html",
    "app.js": "text/javascript",
    "map.js": "text/javascript",
    "chart.js": "text/javascript",
    "d3-force.js": "text/javascript",
    "app.css": "text/css",
}
COOKIE_MAX_AGE = 30 * 24 * 3600
# The map marks the ports that counted errors this many minutes back.
MAP_MINUTES = 5.0
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


class ChartRequest(BaseModel):
    """A chart the page already has, asked for again with the latest samples."""

    title: str = Field(max_length=200)
    ports: list[str] = Field(min_length=1, max_length=MAX_CHART_PORTS)
    metric: Metric
    minutes: float = Field(gt=0)


class StreamObserver(ChatObserver):
    """Turns a question's progress into events for the page."""

    def __init__(self, events: asyncio.Queue[Event | None]) -> None:
        self._events = events

    def thinking(self, text: str) -> None:
        self._events.put_nowait({"type": "thinking", "text": text})

    def content(self, text: str) -> None:
        self._events.put_nowait({"type": "content", "text": text})

    def tool_call(
        self, name: str, arguments: dict[str, Any], result: str, is_error: bool, structured: dict[str, Any] | None = None
    ) -> None:
        self._events.put_nowait(tool_event(name, arguments, result, is_error, structured))


def tool_event(
    name: str, arguments: dict[str, Any], result: str, is_error: bool, structured: dict[str, Any] | None
) -> Event:
    """The event for one tool call. A view the model displayed comes along for the map, and a chart for the page."""
    event: Event = {"type": "tool", "name": name, "arguments": arguments, "result": result, "is_error": is_error}
    if name == DISPLAY_TOOL and structured is not None and not is_error:
        event["view"] = structured
    if name == CHART_TOOL and structured is not None and not is_error:
        event["chart"] = structured
    return event


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
            call_arguments = arguments.get(message.get("tool_call_id", ""), {})
            events.append(
                tool_event(message["tool_name"], call_arguments, message["content"], False, message.get("_structured"))
            )
    return events


def build_app(chat: Chat, token: str, statistics: Poller | None = None) -> Starlette:
    """Build the web app serving one conversation, for browsers holding the token. Charts need the poller."""
    static = files("netgpt") / "static"
    assets = {name: (static / name).read_text(encoding="utf-8") for name in ASSETS}
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

    def asset(name: str) -> Endpoint:
        async def serve_asset(request: Request) -> Response:
            return Response(assets[name], media_type=ASSETS[name], headers=SECURITY_HEADERS)

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

    async def chart(request: Request) -> Response:
        if statistics is None:
            return JSONResponse({"error": "NetGPT isn't polling."}, status_code=404, headers=SECURITY_HEADERS)
        try:
            asked = ChartRequest.model_validate(await request.json())
            drawn = await statistics.chart(asked.title, asked.ports, asked.metric, asked.minutes)
        except (ValueError, ValidationError) as error:
            # ChartError is a ValueError, and names the ports that are polled.
            message = str(error) if isinstance(error, ChartError) else "Send JSON with a title, ports, metric, and minutes."
            return JSONResponse({"error": message}, status_code=400, headers=SECURITY_HEADERS)
        return JSONResponse(drawn.model_dump(mode="json"), headers=SECURITY_HEADERS)

    async def traffic(request: Request) -> Response:
        if statistics is None:
            return JSONResponse({"error": "NetGPT isn't polling."}, status_code=404, headers=SECURITY_HEADERS)
        items, _ = await statistics.traffic(None, MAP_MINUTES)
        ports = [item.model_dump(mode="json") for item in items]
        return JSONResponse({"interval": statistics.interval, "minutes": MAP_MINUTES, "ports": ports}, headers=SECURITY_HEADERS)

    async def reset(request: Request) -> Response:
        if busy.locked():
            message = "Still answering; wait for the answer before starting over."
            return JSONResponse({"error": message}, status_code=409, headers=SECURITY_HEADERS)
        chat.reset()
        return Response(status_code=204, headers=SECURITY_HEADERS)

    return Starlette(
        routes=[
            Route("/", index),
            *(Route(f"/{name}", requires_sign_in(asset(name))) for name in ASSETS if name != "index.html"),
            Route("/api/conversation", requires_sign_in(conversation)),
            Route("/api/ask", requires_sign_in(ask), methods=["POST"]),
            Route("/api/reset", requires_sign_in(reset), methods=["POST"]),
            Route("/api/chart", requires_sign_in(chart), methods=["POST"]),
            Route("/api/traffic", requires_sign_in(traffic)),
        ]
    )


async def serve(chat: Chat, host: str, port: int, token: str, statistics: Poller | None = None) -> None:
    """Serve the chat page until interrupted."""
    # No access log: the first request's URL carries the token.
    config = uvicorn.Config(build_app(chat, token, statistics), host=host, port=port, log_level="warning", access_log=False)
    await uvicorn.Server(config).serve()

"""
Tests for the netgpt-chat web page: sign-in, the streamed answer events, the
replayed conversation, and one question at a time. The app runs in-process
through httpx's ASGI transport, with a scripted Ollama playing the model.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator, Awaitable, Callable
from typing import Any

import httpx
import pytest
from mcp import Client

from netgpt.chat import Chat
from netgpt.inventory import Inventory
from netgpt.server import build_server
from netgpt.web import build_app

from fakes import FakePlatform, ScriptedOllama, calls, says

TOKEN = "correct-horse-battery-staple"

Browser = Callable[..., Awaitable[httpx.AsyncClient]]


def events(response: httpx.Response) -> list[dict[str, Any]]:
    return [json.loads(line) for line in response.text.splitlines() if line.strip()]


@pytest.fixture
async def browser(inventory: Inventory) -> AsyncIterator[Browser]:
    """Yields a factory: given a scripted Ollama, it returns a browser for the app, signed in or not."""
    # The page can draw, so its server offers display, as netgpt-chat --web does.
    async with Client(build_server(inventory, platforms={"fake": FakePlatform}, display=True)) as client:
        opened: list[httpx.AsyncClient] = []

        async def open_page(ollama: ScriptedOllama, signed_in: bool = True) -> httpx.AsyncClient:
            http = ollama.client()
            chat = Chat(client, http, "scripted", {}, think=True)
            await chat.start()
            page = httpx.AsyncClient(transport=httpx.ASGITransport(app=build_app(chat, TOKEN)), base_url="http://netgpt.test")
            opened.extend([http, page])
            if signed_in:
                await page.get("/", params={"token": TOKEN})
            return page

        yield open_page
        for http in opened:
            await http.aclose()


@pytest.mark.anyio
async def test_nothing_is_served_without_the_token(browser: Browser) -> None:
    page = await browser(ScriptedOllama(says("Never asked.")), signed_in=False)
    wrong = await page.get("/", params={"token": "guess"})
    assert wrong.status_code == 401 and "set-cookie" not in wrong.headers
    paths = ["/", "/app.js", "/map.js", "/d3-force.js", "/app.css", "/api/conversation"]
    for method, path in [("GET", path) for path in paths] + [("POST", "/api/ask"), ("POST", "/api/reset")]:
        response = await page.request(method, path, json={"question": "hi"} if method == "POST" else None)
        assert response.status_code == 401, path


@pytest.mark.anyio
async def test_token_link_becomes_a_strict_cookie_and_a_clean_url(browser: Browser) -> None:
    page = await browser(ScriptedOllama(), signed_in=False)
    response = await page.get("/", params={"token": TOKEN})
    assert response.status_code == 303 and response.headers["location"] == "./"
    cookie = response.headers["set-cookie"].lower()
    assert "httponly" in cookie and "samesite=strict" in cookie
    index = await page.get("/")
    assert index.status_code == 200 and "app.js" in index.text
    assert "script-src 'self'" in index.headers["content-security-policy"]
    for script in ("/app.js", "/map.js", "/d3-force.js"):
        assert (await page.get(script)).headers["content-type"].startswith("text/javascript")


@pytest.mark.anyio
async def test_answer_streams_thinking_tool_calls_and_answer(browser: Browser) -> None:
    thinking_then_call = [{"role": "assistant", "content": "", "thinking": "Check LLDP."}, calls("lldp_neighbors", {"devices": "leaf1"})]
    page = await browser(ScriptedOllama(thinking_then_call, says("leaf1 is cabled to spine1.")))
    response = await page.post("/api/ask", json={"question": "What is leaf1 cabled to?"})
    assert response.headers["content-type"].startswith("application/x-ndjson")
    streamed = events(response)
    assert [event["type"] for event in streamed] == ["thinking", "tool", "content", "answer"]
    tool = streamed[1]
    assert (tool["name"], tool["arguments"], tool["is_error"]) == ("lldp_neighbors", {"devices": "leaf1"}, False)
    assert "spine1" in tool["result"]
    assert streamed[-1]["text"] == "leaf1 is cabled to spine1."


@pytest.mark.anyio
async def test_reload_replays_the_conversation_and_reset_clears_it(browser: Browser) -> None:
    page = await browser(ScriptedOllama([{"role": "assistant", "content": "", "thinking": "Hmm."}, calls("list_devices", {})], says("Four devices.")))
    await page.post("/api/ask", json={"question": "How many devices?"})
    conversation = (await page.get("/api/conversation")).json()
    assert (conversation["model"], conversation["think"], conversation["busy"]) == ("scripted", True, False)
    assert [event["type"] for event in conversation["events"]] == ["user", "thinking", "tool", "answer"]
    assert conversation["events"][2]["name"] == "list_devices"
    assert (await page.post("/api/reset")).status_code == 204
    assert (await page.get("/api/conversation")).json()["events"] == []


@pytest.mark.anyio
async def test_one_question_at_a_time(browser: Browser) -> None:
    ollama = ScriptedOllama(says("First."), gated=True)
    page = await browser(ollama)
    first = asyncio.create_task(page.post("/api/ask", json={"question": "One"}))
    await ollama.entered.wait()
    assert (await page.post("/api/ask", json={"question": "Two"})).status_code == 409
    assert (await page.post("/api/reset")).status_code == 409
    assert (await page.get("/api/conversation")).json()["busy"] is True
    ollama.gate.set()
    assert events(await first)[-1] == {"type": "answer", "text": "First."}
    assert (await page.get("/api/conversation")).json()["busy"] is False


@pytest.mark.anyio
async def test_failures_arrive_as_error_events(browser: Browser) -> None:
    page = await browser(ScriptedOllama())
    response = await page.post("/api/ask", json={"question": "Anyone home?"})
    assert events(response) == [{"type": "error", "text": "Ollama: model 'missing' not found"}]
    assert (await page.post("/api/ask", json={"question": "  "})).status_code == 400
    assert (await page.post("/api/ask", json={"nope": 1})).status_code == 400


@pytest.mark.anyio
async def test_a_displayed_view_reaches_the_map_and_survives_a_reload(browser: Browser) -> None:
    page = await browser(ScriptedOllama(calls("display", {"title": "leaf1's host", "focus": ["198.51.100.11"]}), says("See the map.")))
    streamed = events(await page.post("/api/ask", json={"question": "Where is leaf1's host?"}))
    tool = streamed[0]
    assert tool["result"].startswith('The user now sees "leaf1\'s host"')
    assert tool["view"]["emphasis"]["198.51.100.11"] == "focus"
    replayed = (await page.get("/api/conversation")).json()["events"]
    assert replayed[1]["view"] == tool["view"]


@pytest.mark.anyio
async def test_only_display_results_carry_a_view(browser: Browser) -> None:
    page = await browser(ScriptedOllama(calls("display", {"title": "Nope", "focus": ["leaf9"]}), calls("topology", {}), says("Hmm.")))
    streamed = events(await page.post("/api/ask", json={"question": "Show leaf9."}))
    refused, graph = streamed[0], streamed[1]
    assert refused["is_error"] and "view" not in refused
    assert graph["name"] == "topology" and "view" not in graph


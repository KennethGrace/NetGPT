"""
Tests for netgpt-chat's tool-calling loop. A scripted Ollama, served through
httpx's mock transport, plays the model; the tools are the real NetGPT server on
the fake platform in conftest.py.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from typing import Any

import httpx
import pytest
from mcp import Client

from netgpt.chat import Chat, ChatError, supports_thinking


def calls(name: str, arguments: dict[str, Any], call_id: str = "call_1", content: str = "") -> dict[str, Any]:
    tool_calls = [{"id": call_id, "function": {"name": name, "arguments": arguments}}]
    return {"role": "assistant", "content": content, "tool_calls": tool_calls}


def says(text: str) -> dict[str, Any]:
    return {"role": "assistant", "content": text}


class ScriptedOllama:
    """Replies to /api/chat with the scripted messages in order and keeps every request."""

    def __init__(self, *replies: dict[str, Any]) -> None:
        self.replies = list(replies)
        self.requests: list[dict[str, Any]] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(json.loads(request.content))
        if not self.replies:
            return httpx.Response(404, json={"error": "model 'missing' not found"})
        return httpx.Response(200, json={"message": self.replies.pop(0), "done": True})

    def messages(self, request: int) -> list[dict[str, Any]]:
        return self.requests[request]["messages"]


@pytest.fixture
async def chat_with(client: Client) -> AsyncIterator[Any]:
    """Build a Chat against a scripted Ollama; yields a factory taking the script."""
    async with client:
        http_clients: list[httpx.AsyncClient] = []

        async def build(ollama: ScriptedOllama, max_rounds: int = 10) -> Chat:
            http = httpx.AsyncClient(base_url="http://ollama.test", transport=httpx.MockTransport(ollama.handler))
            http_clients.append(http)
            chat = Chat(client, http, "scripted", {"num_ctx": 8192}, max_rounds=max_rounds)
            await chat.start()
            return chat

        yield build
        for http in http_clients:
            await http.aclose()


@pytest.mark.anyio
async def test_tool_results_go_back_to_the_model(chat_with: Any) -> None:
    ollama = ScriptedOllama(calls("lldp_neighbors", {"devices": ["leaf1"]}), says("leaf1 is cabled to spine1."))
    chat = await chat_with(ollama)
    assert await chat.ask("What is leaf1 cabled to?") == "leaf1 is cabled to spine1."

    first = ollama.requests[0]
    assert first["messages"][0]["role"] == "system" and "list_devices" in first["messages"][0]["content"]
    assert len(first["tools"]) == 8 and first["options"] == {"num_ctx": 8192} and first["think"] is False
    tool_message = ollama.messages(1)[-1]
    assert (tool_message["role"], tool_message["tool_name"], tool_message["tool_call_id"]) == ("tool", "lldp_neighbors", "call_1")
    assert json.loads(tool_message["content"])["items"][0]["neighbor"] == "spine1"


@pytest.mark.anyio
async def test_narration_alongside_tool_calls_is_dropped_from_history(chat_with: Any) -> None:
    narrated = calls("list_devices", {}, content="First I will list the devices.\n\n")
    ollama = ScriptedOllama(narrated, says("Four devices."))
    chat = await chat_with(ollama)
    await chat.ask("How many devices?")
    assistant = ollama.messages(1)[2]
    assert assistant["content"] == "" and assistant["tool_calls"] == narrated["tool_calls"]


@pytest.mark.anyio
async def test_refused_command_reaches_the_model_not_the_device(chat_with: Any, commands_run: list[str]) -> None:
    ollama = ScriptedOllama(calls("show", {"device": "leaf1", "command": "info > backup.cfg"}), says("I can't write files."))
    chat = await chat_with(ollama)
    await chat.ask("Save leaf1's config to backup.cfg.")
    assert "'>' is not allowed" in ollama.messages(1)[-1]["content"]
    assert commands_run == []


@pytest.mark.anyio
@pytest.mark.parametrize("blank", ["", "<|tool_response>", "  <turn|> \n"])
async def test_blank_reply_is_resampled(chat_with: Any, blank: str) -> None:
    ollama = ScriptedOllama(calls("list_devices", {}), says(blank), says("Four devices."))
    chat = await chat_with(ollama)
    assert await chat.ask("How many devices?") == "Four devices."
    # The blank reply was discarded, not kept in the conversation.
    assert ollama.messages(2) == ollama.messages(1)
    assert [message["role"] for message in chat.messages] == ["system", "user", "assistant", "tool", "assistant"]


@pytest.mark.anyio
async def test_conversation_carries_over_until_reset(chat_with: Any) -> None:
    ollama = ScriptedOllama(says("Hello."), says("Still here."), says("Fresh start."))
    chat = await chat_with(ollama)
    await chat.ask("Hi")
    await chat.ask("Are you there?")
    assert [message["content"] for message in ollama.messages(1)[1:]] == ["Hi", "Hello.", "Are you there?"]
    chat.reset()
    await chat.ask("New topic")
    assert [message["role"] for message in ollama.messages(2)] == ["system", "user"]


@pytest.mark.anyio
async def test_endless_tool_calls_give_up_and_leave_history_clean(chat_with: Any) -> None:
    ollama = ScriptedOllama(*(calls("list_devices", {}, f"call_{n}") for n in range(3)))
    chat = await chat_with(ollama, max_rounds=3)
    with pytest.raises(ChatError, match="still calling tools after 3 turns"):
        await chat.ask("Loop forever")
    assert [message["role"] for message in chat.messages] == ["system"]


@pytest.mark.anyio
async def test_thinking_follows_the_model_capabilities() -> None:
    capabilities = {"gemma4:e4b": ["completion", "tools", "thinking"], "mistral:latest": ["completion", "tools"]}

    def show(request: httpx.Request) -> httpx.Response:
        model = json.loads(request.content)["model"]
        if model not in capabilities:
            return httpx.Response(404, json={"error": f"model '{model}' not found"})
        return httpx.Response(200, json={"capabilities": capabilities[model]})

    async with httpx.AsyncClient(base_url="http://ollama.test", transport=httpx.MockTransport(show)) as http:
        assert await supports_thinking(http, "gemma4:e4b") is True
        assert await supports_thinking(http, "mistral:latest") is False
        with pytest.raises(ChatError, match="model 'nope' not found"):
            await supports_thinking(http, "nope")


@pytest.mark.anyio
async def test_ollama_errors_are_reported(chat_with: Any) -> None:
    chat = await chat_with(ScriptedOllama())
    with pytest.raises(ChatError, match="model 'missing' not found"):
        await chat.ask("Anyone home?")

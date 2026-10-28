"""
Tests for netgpt-chat's tool-calling loop. A scripted Ollama, served through
httpx's mock transport, plays the model; the tools are the real NetGPT server on
the fake platform from fakes.py.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from typing import Any

import httpx
import pytest
from mcp import Client

from netgpt.chat import OUT_OF_TOOLS, Chat, ChatError, ChatObserver, plain_schema, supports_thinking

from fakes import ScriptedOllama, calls, says


class RecordingObserver(ChatObserver):
    def __init__(self) -> None:
        self.events: list[tuple[str, Any]] = []

    def thinking(self, text: str) -> None:
        self.events.append(("thinking", text))

    def content(self, text: str) -> None:
        self.events.append(("content", text))

    def tool_call(
        self, name: str, arguments: dict[str, Any], result: str, is_error: bool, structured: dict[str, Any] | None = None
    ) -> None:
        self.events.append(("tool", (name, arguments, is_error)))


@pytest.fixture
async def chat_with(client: Client) -> AsyncIterator[Any]:
    """Build a Chat against a scripted Ollama; yields a factory taking the script."""
    async with client:
        http_clients: list[httpx.AsyncClient] = []

        async def build(ollama: ScriptedOllama, max_rounds: int = 10) -> Chat:
            http = ollama.client()
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
    assert len(first["tools"]) == 11 and first["options"] == {"num_ctx": 8192}
    assert first["think"] is False and first["stream"] is True
    tool_message = ollama.messages(1)[-1]
    assert (tool_message["role"], tool_message["tool_name"], tool_message["tool_call_id"]) == ("tool", "lldp_neighbors", "call_1")
    assert json.loads(tool_message["content"])["items"][0]["neighbor"] == "spine1"
    # The structured result stays in the history, for the page to redraw, but never reaches Ollama.
    assert chat.messages[3]["_structured"]["items"][0]["neighbor"] == "spine1"
    assert "_structured" not in tool_message


@pytest.mark.anyio
async def test_streamed_pieces_reach_the_observer_and_the_history(chat_with: Any) -> None:
    streamed = [
        {"role": "assistant", "content": "", "thinking": "Need LLDP. "},
        {"role": "assistant", "content": "", "thinking": "Ask for leaf1."},
        calls("lldp_neighbors", {"devices": ["leaf1"]}),
    ]
    answer = [{"role": "assistant", "content": "leaf1 is cabled "}, {"role": "assistant", "content": "to spine1."}]
    chat = await chat_with(ScriptedOllama(streamed, answer))
    observer = RecordingObserver()
    assert await chat.ask("What is leaf1 cabled to?", observer) == "leaf1 is cabled to spine1."
    assert observer.events == [
        ("thinking", "Need LLDP. "),
        ("thinking", "Ask for leaf1."),
        ("tool", ("lldp_neighbors", {"devices": ["leaf1"]}, False)),
        ("content", "leaf1 is cabled "),
        ("content", "to spine1."),
    ]
    assert chat.messages[2]["thinking"] == "Need LLDP. Ask for leaf1."
    assert chat.messages[2]["tool_calls"][0]["function"]["name"] == "lldp_neighbors"


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
    observer = RecordingObserver()
    await chat.ask("Save leaf1's config to backup.cfg.", observer)
    assert "'>' is not allowed" in ollama.messages(1)[-1]["content"]
    assert ("tool", ("show", {"device": "leaf1", "command": "info > backup.cfg"}, True)) in observer.events
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
async def test_running_out_of_tool_calls_still_gets_an_answer(chat_with: Any) -> None:
    script = [calls("list_devices", {}, f"call_{n}") for n in range(3)]
    ollama = ScriptedOllama(*script, says("Four devices, from what I found."))
    chat = await chat_with(ollama, max_rounds=3)
    assert await chat.ask("How many devices?") == "Four devices, from what I found."
    last = ollama.requests[-1]
    assert last["tools"] == [] and last["messages"][-1] == {"role": "user", "content": OUT_OF_TOOLS}
    assert [message["role"] for message in chat.messages] == ["system", "user", *["assistant", "tool"] * 3, "user", "assistant"]


@pytest.mark.anyio
async def test_a_model_that_only_calls_tools_gives_up_and_leaves_history_clean(chat_with: Any) -> None:
    ollama = ScriptedOllama(*(calls("list_devices", {}, f"call_{n}") for n in range(4)))
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


def test_schemas_reach_ollama_without_references_or_null_unions() -> None:
    # Ollama drops $ref, so a nested model would reach the model as an empty object.
    schema = {
        "type": "object",
        "properties": {
            "devices": {"anyOf": [{"type": "array", "items": {"type": "string"}}, {"type": "null"}], "default": None},
            "path": {"anyOf": [{"$ref": "#/$defs/Ends"}, {"type": "null"}], "default": None},
            "plane": {"enum": ["data", "all"], "type": "string"},
        },
        "$defs": {"Ends": {"type": "object", "properties": {"source": {"type": "string"}}, "required": ["source"]}},
    }
    assert plain_schema(schema) == {
        "type": "object",
        "properties": {
            "devices": {"type": "array", "items": {"type": "string"}, "default": None},
            "path": {"type": "object", "properties": {"source": {"type": "string"}}, "required": ["source"], "default": None},
            "plane": {"enum": ["data", "all"], "type": "string"},
        },
    }


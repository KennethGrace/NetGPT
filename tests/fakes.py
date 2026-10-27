"""
Fakes shared by the tests: a platform that answers from canned data, and an
Ollama server that streams scripted replies.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

import httpx

from netgpt.guard import NOKIA_SRLINUX
from netgpt.platforms import Platform, PlatformError
from netgpt.primitives import LldpNeighbor


class FakePlatform(Platform):
    """Answers from canned data; a device named 'broken' fails like an unreachable device."""

    policy = NOKIA_SRLINUX
    commands_run: list[str] = []

    async def _run_checked_cli(self, command: str) -> str:
        FakePlatform.commands_run.append(command)
        return f"{self.device.name}: ran {command}"

    async def lldp_neighbors(self) -> list[LldpNeighbor]:
        if self.device.name == "broken":
            raise PlatformError("broken: connection refused")
        return [LldpNeighbor(device=self.device.name, interface="ethernet-1/49", neighbor="spine1")]


def calls(name: str, arguments: dict[str, Any], call_id: str = "call_1", content: str = "") -> dict[str, Any]:
    """A model reply that calls one tool."""
    tool_calls = [{"id": call_id, "function": {"name": name, "arguments": arguments}}]
    return {"role": "assistant", "content": content, "tool_calls": tool_calls}


def says(text: str) -> dict[str, Any]:
    """A model reply that answers."""
    return {"role": "assistant", "content": text}


class ScriptedOllama:
    """
    Streams the scripted replies to /api/chat in order, and keeps every request.
    A reply given as a list is streamed as those chunks. With gated=True, each
    reply waits for gate to be set, so a test can hold an answer in flight.
    """

    def __init__(self, *replies: dict[str, Any] | list[dict[str, Any]], gated: bool = False) -> None:
        self.replies = list(replies)
        self.requests: list[dict[str, Any]] = []
        self.entered = asyncio.Event()
        self.gate = asyncio.Event()
        if not gated:
            self.gate.set()

    async def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(json.loads(request.content))
        self.entered.set()
        await self.gate.wait()
        if not self.replies:
            return httpx.Response(404, json={"error": "model 'missing' not found"})
        reply = self.replies.pop(0)
        chunks = reply if isinstance(reply, list) else [reply]
        lines = [json.dumps({"message": chunk, "done": False}) for chunk in chunks]
        lines.append(json.dumps({"message": {"role": "assistant", "content": ""}, "done": True}))
        return httpx.Response(200, text="\n".join(lines) + "\n", headers={"Content-Type": "application/x-ndjson"})

    def client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(base_url="http://ollama.test", transport=httpx.MockTransport(self.handler))

    def messages(self, request: int) -> list[dict[str, Any]]:
        return self.requests[request]["messages"]

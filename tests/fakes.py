"""
Fakes shared by the tests: a platform that answers from canned data, a transport
that replays recorded SR Linux responses, and an Ollama server that streams
scripted replies.
"""

from __future__ import annotations

import asyncio
import json
import re
from pathlib import Path
from typing import Any

import httpx

from netgpt.guard import NOKIA_SRLINUX
from netgpt.platforms import Platform, PlatformError
from netgpt.primitives import ArpEntry, LldpNeighbor

FIXTURES = Path(__file__).parent / "fixtures" / "srlinux"


def replay(fixtures: dict[str, str]) -> tuple[httpx.MockTransport, list[str]]:
    """
    Answer each SR Linux JSON-RPC request with the fixture whose key prefixes its
    CLI command or YANG path. Returns the transport and the list of requests made.
    """
    requests: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        command = json.loads(request.content)["params"]["commands"][0]
        key = command["path"] if isinstance(command, dict) else command
        requests.append(key)
        name = next(fixture for prefix, fixture in fixtures.items() if key.startswith(prefix))
        return httpx.Response(200, json=json.loads((FIXTURES / name).read_text()))

    return httpx.MockTransport(handler), requests


class FakePlatform(Platform):
    """
    Answers from canned data: each device is cabled to spine1 (outside the
    inventory) on ethernet-1/49, with one host behind ethernet-1/1. A device
    named 'broken' fails like an unreachable device.
    """

    policy = NOKIA_SRLINUX
    management_interfaces = re.compile(r"^mgmt\d+$")
    commands_run: list[str] = []

    async def _run_checked_cli(self, command: str) -> str:
        FakePlatform.commands_run.append(command)
        return f"{self.device.name}: ran {command}"

    async def lldp_neighbors(self) -> list[LldpNeighbor]:
        if self.device.name == "broken":
            raise PlatformError("broken: connection refused")
        return [LldpNeighbor(device=self.device.name, interface="ethernet-1/49", neighbor="spine1")]

    async def arp_table(self) -> list[ArpEntry]:
        if self.device.name == "broken":
            raise PlatformError("broken: connection refused")
        host = {"leaf1": "198.51.100.11", "leaf2": "198.51.100.12"}[self.device.name]
        return [ArpEntry(device=self.device.name, interface="ethernet-1/1.0", ip_address=host, mac="02:00:00:00:00:01")]


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

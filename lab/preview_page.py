"""
Serve the chat page against a real inventory, with a scripted model in place of
Ollama, so the page can be worked on without waiting for a model: every question
gets its turn of the script below, streamed at a reading pace, with real tool calls
on the real devices (the WAN lab's, by default) and real polled traffic.

    python lab/preview_page.py --listen 127.0.0.1:8090
    # then open http://127.0.0.1:8090/?token=preview

The answers are written for the WAN lab (lab/wan/) as it is deployed. Questions past
the end of the script get a note that the script has run out.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import httpx
from mcp import Client

from netgpt.chat import Chat, answer_limit
from netgpt.engine import Engine
from netgpt.inventory import Inventory
from netgpt.server import build_server
from netgpt.service import listen_address
from netgpt.statistics import Poller
from netgpt.web import serve

CONTEXT = 65536


def thinks(text: str) -> list[dict[str, Any]]:
    """A reply's reasoning, a few words at a time."""
    words = text.split(" ")
    return [{"role": "assistant", "content": "", "thinking": " ".join(words[index:index + 4]) + " "}
            for index in range(0, len(words), 4)]


def calls(*tools: tuple[str, dict[str, Any]]) -> list[dict[str, Any]]:
    return [{"role": "assistant", "content": "",
             "tool_calls": [{"id": f"call_{index}", "function": {"name": name, "arguments": arguments}}
                            for index, (name, arguments) in enumerate(tools)]}]


def says(text: str) -> list[dict[str, Any]]:
    """An answer, a line at a time."""
    return [{"role": "assistant", "content": line + "\n"} for line in text.split("\n")]


# Each question's replies, in order: a reply is the chunks it streams.
SCRIPT: list[list[list[dict[str, Any]]]] = [
    [
        thinks("The user wants the whole network. A drawing at layer 2 shows the cables and where the hosts are; "
               "I'll highlight the WAN between the three sites.") + calls(("topology", {"layer": "l2"})),
        calls(("display", {"title": "Three sites on the WAN", "layer": "l2",
                           "highlight": ["hq-core--ops-core", "hq-core--branch", "branch--ops-core"],
                           "notes": {"hq-core--ops-core": "25G", "hq-core--branch": "10G", "branch--ops-core": "1G"}})),
        says(
            "The network is **three sites joined by the WAN**, drawn on the map:\n\n"
            "- **HQ**: `hq-core` is the site's router. `hq-sw1` bridges the campus LAN behind it, with "
            "10.10.10.11 and 10.10.10.12 on it, and `hq-dc` routes the datacenter, where 10.10.100.10 is.\n"
            "- **Ops**: `ops-core`, with 10.20.10.11 on its own port, and `ops-dc` in front of 10.20.100.10.\n"
            "- **Branch**: `branch`, with 10.30.10.11 behind ethernet-1/10.\n\n"
            "| Link | Ends | Speed |\n|---|---|---|\n"
            "| hq-core ↔ ops-core | ethernet-1/1 both ends | 25G |\n"
            "| hq-core ↔ branch | hq-core ethernet-1/20, branch ethernet-1/1 | 10G |\n"
            "| ops-core ↔ branch | ops-core ethernet-1/20, branch ethernet-1/2 | 1G |\n\n"
            "Every cable is reported by both of its ends."
        ),
    ],
    [
        thinks("A path question: follow the routing tables from branch to 10.20.100.10, then draw it. The OSPF "
               "costs will say why it doesn't take the direct 1G link.")
        + calls(("forwarding_path", {"source": "branch", "destination": "10.20.100.10"}),
                ("ospf_interfaces", {"devices": ["branch", "hq-core", "ops-core"]})),
        calls(("display", {"title": "branch to ops-srv1", "layer": "l2",
                           "path": {"source": "branch", "destination": "10.20.100.10"}})),
        says(
            "Packets from `branch` to **10.20.100.10** go **through HQ**, not over the direct link to Ops:\n\n"
            "1. `branch` sends them out **ethernet-1/1** to `hq-core` (OSPF route 10.20.100.0/24).\n"
            "2. `hq-core` forwards out **ethernet-1/1**, across the 25G link, to `ops-core`.\n"
            "3. `ops-core` hands them to `ops-dc` on **ethernet-1/3**.\n"
            "4. `ops-dc` delivers them on **ethernet-1/10**, where 10.20.100.10 is.\n\n"
            "OSPF picks the cheaper way: through HQ costs **10 + 4 = 14** to ops-core, while the direct 1G link "
            "from branch ethernet-1/2 costs **100**. That link only carries traffic if the way through HQ fails."
        ),
    ],
    [
        thinks("Traffic on hq-core's ports: read the polled rates, and chart the WAN links so the user can watch them.")
        + calls(("interface_traffic", {"devices": ["hq-core"], "minutes": 15})),
        calls(("chart", {"title": "hq-core's links", "ports": ["hq-core:ethernet-1/1", "hq-core:ethernet-1/20",
                                                             "hq-core:ethernet-1/3"], "minutes": 15})),
        says(
            "`hq-core` carries every flow between the sites, and all of its links are **lightly used**: each flows "
            "at a few Mbit/s, well under 1% of its speed.\n\n"
            "- **ethernet-1/1** to `ops-core` (25G) carries HQ's and the branch's traffic to Ops.\n"
            "- **ethernet-1/20** to `branch` (10G) carries the branch's traffic both ways.\n"
            "- **ethernet-1/3** to `hq-dc` (10G) carries what reaches 10.10.100.10.\n\n"
            "The chart keeps updating every 30 seconds."
        ),
    ],
]

OUT_OF_SCRIPT = says("That's the end of the preview's script. Restart `lab/preview_page.py` to play it again.")


class ScriptedModel:
    """Plays the script as Ollama would stream it, at a reading pace."""

    def __init__(self, pace: float) -> None:
        self.pace = pace
        self.questions = 0
        self.replies: list[list[dict[str, Any]]] = []

    def next_reply(self, messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
        if messages and messages[-1]["role"] == "user":
            # A new question takes the next turn of the script.
            self.replies = list(SCRIPT[self.questions]) if self.questions < len(SCRIPT) else [OUT_OF_SCRIPT]
            self.questions += 1
        return self.replies.pop(0) if self.replies else OUT_OF_SCRIPT

    async def handle(self, request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/show":
            return httpx.Response(200, json={"capabilities": ["completion", "tools", "thinking"]})
        chunks = self.next_reply(json.loads(request.content)["messages"])

        async def stream() -> AsyncIterator[bytes]:
            for chunk in chunks:
                await asyncio.sleep(self.pace)
                yield (json.dumps({"message": chunk, "done": False}) + "\n").encode()
            yield (json.dumps({"message": {"role": "assistant", "content": ""}, "done": True}) + "\n").encode()

        return httpx.Response(200, content=stream(), headers={"Content-Type": "application/x-ndjson"})


async def run(args: argparse.Namespace) -> None:
    inventory = Inventory.load(args.inventory)
    statistics = Poller(inventory, interval=args.poll_interval) if args.poll_interval > 0 else None
    engine = Engine(inventory, statistics=statistics)
    model = ScriptedModel(args.pace)
    async with (
        Client(build_server(engine, display=True, max_answer=answer_limit(CONTEXT))) as client,
        httpx.AsyncClient(base_url="http://scripted.preview", transport=httpx.MockTransport(model.handle)) as http,
    ):
        chat = Chat(client, http, "scripted preview", {"num_ctx": CONTEXT}, think=True)
        await chat.start()
        host, port = args.listen
        print(f"Preview at http://{host}:{port}/?token={args.token}", flush=True)
        await serve(chat, host, port, args.token, engine)


def main() -> None:
    parser = argparse.ArgumentParser(description="Serve the chat page with a scripted model, for working on the page.")
    parser.add_argument("--inventory", type=Path, default=Path("lab/wan/inventory.yml"), help="The devices to read.")
    parser.add_argument("--listen", type=listen_address, default=("127.0.0.1", 8090), help="[HOST:]PORT to serve on.")
    parser.add_argument("--token", default=os.environ.get("NETGPT_PREVIEW_TOKEN", "preview"), help="The sign-in token.")
    parser.add_argument("--pace", type=float, default=0.04, help="Seconds between the script's streamed chunks.")
    parser.add_argument("--poll-interval", type=float, default=30, help="Seconds between polls of the counters; 0 for none.")
    args = parser.parse_args()
    asyncio.run(run(args))


if __name__ == "__main__":
    main()

"""
Play the gremlin's game with a model. The gremlin breaks one thing, the model hears
the users' complaint and investigates with NetGPT's tools, and its answer is judged
against what the gremlin did. Every round goes on a scoreboard, so models, and
versions of NetGPT, can be compared.

The model runs here, through netgpt-chat's tool loop, with a fresh conversation each
round and no map to draw on. Run it from the repository root with the lab deployed:

    python lab/game.py --model gemma4:26b-mlx --ollama http://HOST:11434    # each fault once, in random order
    python lab/game.py --model ... --faults loop blackhole --rounds 3
    python lab/game.py --scoreboard                                         # add --answers to read them
    python lab/game.py --rejudge --model ... --ollama ...                   # judge rounds no judge has seen

A judge model reads each answer beside what the gremlin broke, and decides whether
the root cause the answer settles on is the right one: found, partly, or missed.
The judge is the model that played unless --judge names another. A keyword rubric
(gremlin.Fault.clues) scores each answer too, and is kept beside the verdict. The
rubric can't tell which device an answer blames, and the judge can be wrong, so read
the answers where the two disagree.

Rounds are appended to lab/.scoreboard.jsonl, each marked with the NetGPT commit it
was played against, plus a fingerprint of any uncommitted changes to src/.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import logging
import random
import statistics
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Any

import httpx
from mcp import Client

import gremlin
from gremlin import Fault, GremlinError
from netgpt.chat import (
    DEFAULT_CONTEXT,
    DEFAULT_MODEL,
    DEFAULT_OLLAMA,
    Chat,
    OUT_OF_TOOLS,
    ChatError,
    TerminalObserver,
    ollama_post,
    supports_thinking,
)
from netgpt.inventory import Inventory
from netgpt.server import build_server

SCOREBOARD = Path("lab/.scoreboard.jsonl")

JUDGE_PROMPT = """\
Someone broke a lab network on purpose. What they broke, on {device}:

    {cause}

Users complained, and a troubleshooter investigated the network and wrote this diagnosis:

---
{answer}
---

Did the diagnosis find what was broken? Go by the root cause it settles on, not everything it mentions on the way.
- blamed_device: the one device the diagnosis names as the root cause. Write "none" if it settles on no device,
  or hedges between several causes.
- blamed_problem: what the diagnosis says is wrong on that device.
- reason: how the blamed device and problem compare with what was broken.
- verdict: "found" if the blamed device is {device} and the blamed problem is what was broken, in any words.
  "partly" if the blamed device is {device} but the problem is wrong, or too vague to fix. "missed" otherwise.
"""

# Ollama holds the judge's reply to this schema. Naming what the diagnosis blames is easier than judging it,
# so the judge does that first, and the verdict follows.
JUDGEMENT = {
    "type": "object",
    "properties": {
        "blamed_device": {"type": "string"},
        "blamed_problem": {"type": "string"},
        "reason": {"type": "string"},
        "verdict": {"type": "string", "enum": ["found", "partly", "missed"]},
    },
    "required": ["blamed_device", "blamed_problem", "reason", "verdict"],
}

# Every device a fault can be on: the names a judge's blamed_device is read for.
DEVICES = {fault.device for fault in gremlin.FAULTS.values()}


class Watcher(TerminalObserver):
    """Shows each tool call as it happens, and keeps count of them for the scoreboard."""

    def __init__(self) -> None:
        super().__init__(verbose=False)
        self.calls: list[str] = []
        self.errors = 0

    def tool_call(
        self, name: str, arguments: dict[str, Any], result: str, is_error: bool, structured: dict[str, Any] | None = None
    ) -> None:
        super().tool_call(name, arguments, result, is_error, structured)
        self.calls.append(name)
        self.errors += is_error


def question(fault: Fault) -> str:
    """The complaint as users would bring it, with the hosts' addresses, since NetGPT knows hosts only by address."""
    hosts = ", ".join(f"{name} is {address}" for name, address in gremlin.HOSTS.items())
    return f"Users report a problem: {fault.complaint} ({hosts}.) What's wrong?"


def rubric(fault: Fault, answer: str | None) -> str:
    """Whether an answer names the device and a clue from each group: found or missed."""
    if answer is None:
        return "no answer"
    text = answer.lower()
    named = fault.device in text and all(any(clue in text for clue in group) for group in fault.clues)
    return "found" if named else "missed"


async def judge(http: httpx.AsyncClient, model: str, fault: Fault, answer: str) -> tuple[str, str]:
    """Ask a model whether an answer found the fault. Returns its verdict and its reason."""
    prompt = JUDGE_PROMPT.format(device=fault.device, cause=fault.answer, answer=answer.strip())
    reply = await ollama_post(
        http,
        "/api/chat",
        {
            "model": model,
            "messages": [{"role": "user", "content": prompt}],
            "format": JUDGEMENT,
            "stream": False,
            "think": False,
            "options": {"temperature": 0},
        },
    )
    try:
        judgement = json.loads(reply["message"]["content"])
        blamed, verdict = judgement["blamed_device"].lower(), judgement["verdict"]
        reason = f"blames {judgement['blamed_device']}: {judgement['blamed_problem']} {judgement['reason']}"
    except (KeyError, TypeError, ValueError, AttributeError) as error:
        raise ChatError(f"The judge, {model}, gave no verdict: {reply.get('message')!r}") from error
    # A diagnosis that blames another device, or several, missed, whatever the judge's verdict says.
    if {device for device in DEVICES if device in blamed} != {fault.device}:
        verdict = "missed"
    return verdict, reason


async def judge_round(http: httpx.AsyncClient, model: str, record: dict[str, Any]) -> None:
    """Add the judge's verdict and reason to a round; a round without an answer needs no judge."""
    if record["answer"] is None:
        record.update(verdict="no answer", reason=None, judge=None)
        return
    verdict, reason = await judge(http, model, gremlin.FAULTS[record["fault"]], record["answer"])
    record.update(verdict=verdict, reason=reason, judge=model)


def netgpt_version() -> str:
    """
    The commit NetGPT is at. With uncommitted changes to src/, a fingerprint of them
    follows a "+", so each state of the code played gets its own row.
    """
    try:
        commit = subprocess.run(["git", "rev-parse", "--short", "HEAD"], capture_output=True, text=True, check=True)
        changes = subprocess.run(["git", "diff", "HEAD", "--", "src"], capture_output=True, check=True)
    except (OSError, subprocess.CalledProcessError):
        return "unknown"
    fingerprint = f"+{hashlib.sha1(changes.stdout).hexdigest()[:7]}" if changes.stdout else ""
    return commit.stdout.strip() + fingerprint


async def play_round(chat: Chat, inventory: Inventory, fault: Fault) -> dict[str, Any]:
    """Break the fault, let the model investigate, and put the lab back. The round is judged afterwards."""
    watcher = Watcher()
    gremlin.inflict(inventory, fault)
    started = time.monotonic()
    answer, error = None, None
    try:
        chat.reset()
        answer = await chat.ask(question(fault), watcher)
    except ChatError as failure:
        error = str(failure)
    finally:
        seconds = time.monotonic() - started
        gremlin.mend(inventory, fault)
    silent = gremlin.settle(inventory)
    if silent:
        raise GremlinError(f"The lab didn't come back after repairing {fault.name}. No answer: {', '.join(silent)}.")
    return {
        "when": datetime.now().isoformat(timespec="seconds"),
        "fault": fault.name,
        "rubric": rubric(fault, answer),
        "seconds": round(seconds, 1),
        "replies": sum(message["role"] == "assistant" for message in chat.messages) if answer is not None else None,
        # Whether the answer came from the reply with no tools offered, after every other reply called tools.
        "last_word": answer is not None and any(message["content"] == OUT_OF_TOOLS for message in chat.messages),
        "tool_calls": watcher.calls,
        "tool_errors": watcher.errors,
        "answer": answer,
        "error": error,
    }


def show_round(record: dict[str, Any]) -> None:
    fault = gremlin.FAULTS[record["fault"]]
    disagreement = f"; the rubric said {record['rubric']}" if record.get("rubric", record["verdict"]) != record["verdict"] else ""
    last_word = ", answered when out of tool calls" if record.get("last_word") else ""
    print(
        f"{fault.name}: {record['verdict'].upper()} in {record['seconds']:.0f} s, "
        f"{len(record['tool_calls'])} tool calls ({record['tool_errors']} errors){last_word}{disagreement}"
    )
    print(f"  answer: {' '.join((record['answer'] or record['error']).split())}")
    if record.get("reason"):
        print(f"  judge:  {record['reason']}")
    print(f"  cause:  {fault.answer}", flush=True)


async def play(args: argparse.Namespace) -> None:
    inventory = Inventory.load(args.inventory)
    faults = [gremlin.FAULTS[name] for name in args.faults or gremlin.FAULTS] * args.rounds
    random.shuffle(faults)
    version = netgpt_version()
    async with (
        Client(build_server(inventory)) as client,
        httpx.AsyncClient(base_url=args.ollama, timeout=httpx.Timeout(args.timeout, connect=10.0)) as http,
    ):
        think = args.think if args.think is not None else await supports_thinking(http, args.model)
        chat = Chat(client, http, args.model, {"num_ctx": args.context}, think=think, max_rounds=args.max_rounds)
        await chat.start()
        for number, fault in enumerate(faults, 1):
            print(f"\nRound {number} of {len(faults)}: {fault.complaint}", flush=True)
            record = await play_round(chat, inventory, fault)
            await judge_round(http, args.judge or args.model, record)
            record.update(model=args.model, think=think, max_rounds=args.max_rounds, version=version)
            with SCOREBOARD.open("a") as log:
                log.write(json.dumps(record) + "\n")
            show_round(record)


def load_scoreboard() -> list[dict[str, Any]]:
    return [json.loads(line) for line in SCOREBOARD.read_text().splitlines()] if SCOREBOARD.exists() else []


async def rejudge(args: argparse.Namespace) -> None:
    """
    Judge every round on the scoreboard that no judge has seen. Rounds from before
    there was a judge carry the rubric's verdict, which is kept as their rubric.
    Don't run it while a game is playing: it rewrites the scoreboard.
    """
    records = load_scoreboard()
    async with httpx.AsyncClient(base_url=args.ollama, timeout=httpx.Timeout(args.timeout, connect=10.0)) as http:
        for record in records:
            if "judge" in record:
                continue
            record.setdefault("rubric", record["verdict"])
            await judge_round(http, args.judge or args.model, record)
            if record["answer"] is not None:
                print(f"\n{record['when']} {record['model']}")
                show_round(record)
    SCOREBOARD.write_text("".join(json.dumps(record) + "\n" for record in records))


def scoreboard(answers: bool) -> None:
    """
    Print found/played per fault for each model, NetGPT version, and limit on the
    model's replies, and optionally every answer.
    """
    records = load_scoreboard()
    if not records:
        print("No rounds played yet.")
        return
    groups: dict[tuple[str, str, int], list[dict[str, Any]]] = {}
    for record in records:
        groups.setdefault((record["model"], record["version"], record["max_rounds"]), []).append(record)
    names = list(gremlin.FAULTS)
    print(
        f"{'model':<20} {'netgpt':<14}{'replies':>8}" + "".join(f"{name:>12}" for name in names)
        + f"{'found':>8}{'partly':>8}{'no answer':>11}{'last word':>11}{'median s':>10}"
    )
    for (model, version, limit), rounds in groups.items():
        cells = []
        for name in names:
            played = [record for record in rounds if record["fault"] == name]
            cells.append(f"{sum(record['verdict'] == 'found' for record in played)}/{len(played)}" if played else "-")
        found = sum(record["verdict"] == "found" for record in rounds)
        partly = sum(record["verdict"] == "partly" for record in rounds)
        unanswered = sum(record["verdict"] == "no answer" for record in rounds)
        last_word = sum(bool(record.get("last_word")) for record in rounds)
        median = statistics.median(record["seconds"] for record in rounds)
        print(
            f"{model:<20} {version:<14}{f'≤{limit}':>8}" + "".join(f"{cell:>12}" for cell in cells)
            + f"{f'{found}/{len(rounds)}':>8}{partly:>8}{unanswered:>11}{last_word:>11}{median:>10.0f}"
        )
    if answers:
        for record in records:
            print(f"\n{record['when']} {record['model']} @ {record['version']}")
            show_round(record)


def main() -> None:
    parser = argparse.ArgumentParser(description="Play the gremlin's game: break the lab, ask a model why, keep score.")
    parser.add_argument("--faults", nargs="+", choices=list(gremlin.FAULTS), help="Faults to play (default: all).")
    parser.add_argument("--rounds", type=int, default=1, help="Rounds of each fault (default: %(default)s).")
    parser.add_argument("--model", default=DEFAULT_MODEL, help=f"Ollama model (default: {DEFAULT_MODEL}).")
    parser.add_argument("--judge", help="Ollama model that judges the answers (default: --model).")
    parser.add_argument("--ollama", default=DEFAULT_OLLAMA, help=f"Ollama server URL (default: {DEFAULT_OLLAMA}).")
    parser.add_argument("--context", type=int, default=DEFAULT_CONTEXT, help="Context window in tokens.")
    parser.add_argument(
        "--think", action=argparse.BooleanOptionalAction, default=None, help="Let the model reason (default: when it can)."
    )
    parser.add_argument("--max-rounds", type=int, default=10, help="Model replies allowed per question (default: %(default)s).")
    parser.add_argument("--timeout", type=float, default=900.0, help="Seconds to wait for Ollama (default: %(default)s).")
    parser.add_argument("--inventory", type=Path, default=Path("lab/inventory.yml"), help="The lab inventory.")
    parser.add_argument("--scoreboard", action="store_true", help="Print the scoreboard instead of playing.")
    parser.add_argument("--answers", action="store_true", help="With --scoreboard, print every answer and its cause.")
    parser.add_argument("--rejudge", action="store_true", help="Judge the rounds no judge has seen, instead of playing.")
    parser.add_argument("--log-level", default="WARNING", help="Logging level, written to stderr (default: %(default)s).")
    args = parser.parse_args()
    logging.basicConfig(level=args.log_level, stream=sys.stderr)
    if args.scoreboard:
        scoreboard(args.answers)
        return
    try:
        asyncio.run(rejudge(args) if args.rejudge else play(args))
    except (GremlinError, ChatError) as error:
        sys.exit(str(error))


if __name__ == "__main__":
    main()

"""
Play the gremlin's game with a model. The gremlin breaks one thing, the model hears
the users' complaint and investigates with NetGPT's tools, and its answer is judged
against what the gremlin did. Every round goes on a scoreboard, so models, and
versions of NetGPT, can be compared.

The model runs here, through netgpt-chat's tool loop, with a fresh conversation each
round and no map to draw on. Run it from the repository root with the lab deployed:

    python lab/game.py --model gemma4:26b-mlx --ollama http://HOST:11434    # each fault once, in random order
    python lab/game.py --model ... --faults loop blackhole --rounds 3
    python lab/game.py --lab wan --model ...                                # OSPF faults in the WAN lab
    python lab/game.py --lab wan --pairs --model ...                        # two faults at once
    python lab/game.py --lab wan --anywhere --model ...                     # each fault in a random one of its places
    python lab/game.py --scoreboard                                         # add --answers to read them
    python lab/game.py --rejudge --model ... --ollama ...                   # judge every round again

A judge model reads each answer beside what the gremlin broke, and decides whether
the root cause the answer settles on is the right one: found, partly, or missed.
The judge is the model that played unless --judge names another. A keyword rubric
(gremlin.Fault.clues) scores each answer too, and is kept beside the verdict. The
rubric can't tell which device an answer blames, and the judge can be wrong, so read
the answers where the two disagree.

Rounds are appended to lab/.scoreboard.jsonl, each marked with its lab and the NetGPT
commit it was played against, plus a fingerprint of any uncommitted changes to src/.
The scoreboard has a table for each lab, and keeps rounds played with faults in their
usual places apart from rounds played --anywhere. Each answered round's whole conversation (the
instructions, the tools as offered, the model's thinking and calls, and every tool
result) goes to lab/.transcripts/, named in the round's "transcript".
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
from gremlin import Fault, GremlinError, Lab
from netgpt.chat import (
    DEFAULT_CONTEXT,
    DEFAULT_MODEL,
    DEFAULT_OLLAMA,
    Chat,
    OUT_OF_TOOLS,
    ChatError,
    TerminalObserver,
    for_ollama,
    ollama_post,
    supports_thinking,
)
from netgpt.engine import Engine
from netgpt.inventory import Inventory
from netgpt.server import build_server

SCOREBOARD = Path("lab/.scoreboard.jsonl")
TRANSCRIPTS = Path("lab/.transcripts")

JUDGE_PROMPT = """\
Someone broke a lab network on purpose. What they broke, on {device}:

    {cause}

Users complained, and a troubleshooter investigated the network and wrote this diagnosis:

---
{answer}
---

Did the diagnosis find what was broken? Go by the root cause it settles on, not everything it mentions on the way.
- blamed_device: the device the diagnosis names as the root cause. Write "none" if it names no device.
- blamed_problem: what the diagnosis says is wrong on that device.
- also_blamed: any other devices the diagnosis says are misconfigured or need changing, or [] if none. A device
  it only mentions on the way, as working correctly or as part of the path, is not blamed.
- hedges: true if the diagnosis offers alternative causes ("either A or B", "check both") instead of settling
  on one.
- reason: how the blamed device and problem compare with what was broken.
- verdict: "found" if the blamed device is {device} and the blamed problem is what was broken, in any words.
  "partly" if the blamed device is {device} but the problem is wrong, or too vague to fix. "missed" otherwise.
"""

# When two things are broken at once, each is judged on its own, told about the other, so that blaming the
# other break's device for its problem isn't counted against the answer.
JUDGE_PAIR_PROMPT = """\
Someone broke a lab network on purpose, in two places at once. One break, on {device}:

    {cause}

The other, on {other_device}:

    {other_cause}

Users complained, and a troubleshooter investigated the network and wrote this diagnosis:

---
{answer}
---

Judge only whether the diagnosis found the break on {device}. Go by the causes it settles on, not everything it
mentions on the way.
- blamed_device: the device the diagnosis blames for the problem the break on {device} causes. Write "none" if it
  blames no device for it.
- blamed_problem: what the diagnosis says is wrong on that device.
- also_blamed: any other devices the diagnosis says are misconfigured or need changing, apart from {other_device},
  or [] if none. A device it only mentions on the way, as working correctly or as part of the path, is not blamed.
- hedges: true if the diagnosis offers alternative causes for that problem ("either A or B", "check both") instead
  of settling on one.
- reason: how the blamed device and problem compare with the break on {device}.
- verdict: "found" if the blamed device is {device} and the blamed problem is what was broken there, in any words.
  "partly" if the blamed device is {device} but the problem is wrong, or too vague to fix. "missed" otherwise.
"""

# Ollama holds the judge's reply to this schema. Naming what the diagnosis blames is easier than judging it,
# so the judge does that first, and the verdict follows. One string for the blame ran together "A and B" (both
# wrong) with "A or B" (a hedge), so other devices and hedging have fields of their own.
JUDGEMENT = {
    "type": "object",
    "properties": {
        "blamed_device": {"type": "string"},
        "blamed_problem": {"type": "string"},
        "also_blamed": {"type": "array", "items": {"type": "string"}},
        "hedges": {"type": "boolean"},
        "reason": {"type": "string"},
        "verdict": {"type": "string", "enum": ["found", "partly", "missed"]},
    },
    "required": ["blamed_device", "blamed_problem", "also_blamed", "hedges", "reason", "verdict"],
}

# Every device a fault can be on: the names looked for in what the judge says is blamed.
DEVICES = {fault.device for fault in [*gremlin.FAULTS.values(), *gremlin.VARIANTS.values()]}


class Watcher(TerminalObserver):
    """Shows each tool call as it happens, and keeps them, with their arguments, for the scoreboard."""

    def __init__(self) -> None:
        super().__init__(verbose=False)
        self.calls: list[dict[str, Any]] = []
        self.errors = 0

    def tool_call(
        self, name: str, arguments: dict[str, Any], result: str, is_error: bool, structured: dict[str, Any] | None = None
    ) -> None:
        super().tool_call(name, arguments, result, is_error, structured)
        self.calls.append({"name": name, "arguments": arguments})
        self.errors += is_error


def question(lab: Lab, faults: list[Fault]) -> str:
    """The complaint as users would bring it, with the hosts' addresses, since NetGPT knows hosts only by address."""
    hosts = ", ".join(f"{name} is {address}" for name, address in lab.hosts.items())
    reported = "a problem" if len(faults) == 1 else "two problems"
    return f"Users report {reported}: {gremlin.complaint(faults)} ({hosts}.) What's wrong?"


# Hyphens models write in place of "-", such as the non-breaking one in "hq‑core".
HYPHENS = str.maketrans({character: "-" for character in "\u2010\u2011\u2012\u2013\u2014\u2212"})


def rubric(faults: list[Fault], answer: str | None) -> str:
    """Whether an answer names each fault's device and a clue from each of its groups: found or missed."""
    if answer is None:
        return "no answer"
    text = answer.lower().translate(HYPHENS)
    named = all(
        fault.device in text and all(any(clue in text for clue in group) for group in fault.clues) for fault in faults
    )
    return "found" if named else "missed"


async def judge(http: httpx.AsyncClient, model: str, fault: Fault, answer: str, other: Fault | None = None) -> tuple[str, str]:
    """
    Ask a model whether an answer found the fault, one of a pair if other is the
    rest of it. Returns its verdict and its reason.
    """
    if other is None:
        prompt = JUDGE_PROMPT.format(device=fault.device, cause=fault.answer, answer=answer.strip())
    else:
        prompt = JUDGE_PAIR_PROMPT.format(
            device=fault.device, cause=fault.answer, other_device=other.device, other_cause=other.answer, answer=answer.strip()
        )
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
        blamed = " ".join([judgement["blamed_device"], *judgement["also_blamed"]]).lower()
        hedges, verdict = bool(judgement["hedges"]), judgement["verdict"]
        also = f" and {', '.join(judgement['also_blamed'])}" if judgement["also_blamed"] else ""
        reason = (
            f"blames {judgement['blamed_device']}{also}{', hedging' if hedges else ''}: "
            f"{judgement['blamed_problem']} {judgement['reason']}"
        )
    except (KeyError, TypeError, ValueError, AttributeError) as error:
        raise ChatError(f"The judge, {model}, gave no verdict: {reply.get('message')!r}") from error
    # A diagnosis that hedges, or doesn't blame the fault's device, missed, whatever the judge's verdict says. One
    # that blames another device as well is at best partly right: following it would change something that isn't broken.
    named = {device for device in DEVICES if device in blamed} - ({other.device} if other else set())
    if hedges or fault.device not in named:
        verdict = "missed"
    elif len(named) > 1 and verdict == "found":
        verdict = "partly"
    return verdict, reason


async def judge_round(http: httpx.AsyncClient, model: str, record: dict[str, Any]) -> None:
    """Add the judge's verdict and reason to a round; a round without an answer needs no judge."""
    if record["answer"] is None:
        record.update(verdict="no answer", reason=None, judge=None)
        return
    faults = faults_of(record)
    if len(faults) == 1:
        verdict, reason = await judge(http, model, faults[0], record["answer"])
        record.update(verdict=verdict, reason=reason, judge=model)
        return
    # A pair is found when both breaks are, and partly when either is found or partly.
    first, second = faults
    verdicts = {}
    reasons = []
    for fault, other in ((first, second), (second, first)):
        verdicts[fault.name], said = await judge(http, model, fault, record["answer"], other)
        reasons.append(f"{fault.name}: {verdicts[fault.name]}, {said}")
    found = list(verdicts.values())
    verdict = "found" if found == ["found", "found"] else "partly" if any(v in ("found", "partly") for v in found) else "missed"
    record.update(verdict=verdict, verdicts=verdicts, reason=" | ".join(reasons), judge=model)


def faults_of(record: dict[str, Any]) -> list[Fault]:
    """
    The faults a round broke: one, or a pair, where they were. Rounds from before
    variants were in their usual places, and rounds from before pairs name one.
    """
    return [gremlin.fault_named(name) for name in record.get("variants") or record.get("faults") or [record["fault"]]]


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


def keep_transcript(chat: Chat, when: datetime, lab: Lab, names: list[str]) -> str:
    """
    Write the round's conversation as the model saw it, with the tools it was offered,
    and return the file's name. A good round's transcript is an example to learn from.
    """
    model = chat.model.replace(":", "-").replace("/", "-")
    name = f"{when:%Y%m%dT%H%M%S}-{lab.name}-{'+'.join(names)}-{model}.json"
    TRANSCRIPTS.mkdir(exist_ok=True)
    transcript = {"model": chat.model, "tools": chat.tools, "messages": [for_ollama(message) for message in chat.messages]}
    (TRANSCRIPTS / name).write_text(json.dumps(transcript, indent=1))
    return name


async def play_round(chat: Chat, inventory: Inventory, lab: Lab, faults: list[Fault], anywhere: bool) -> dict[str, Any]:
    """Break the faults, let the model investigate, and put the lab back. The round is judged afterwards."""
    watcher = Watcher()
    names = [fault.name for fault in faults]
    gremlin.inflict(inventory, lab, faults)
    started = time.monotonic()
    answer, error = None, None
    try:
        chat.reset()
        answer = await chat.ask(question(lab, faults), watcher)
    except ChatError as failure:
        error = str(failure)
    finally:
        seconds = time.monotonic() - started
        gremlin.mend(inventory, lab, faults)
    when = datetime.now()
    # A round that failed has no conversation left: Chat drops the question with it.
    transcript = keep_transcript(chat, when, lab, names) if answer is not None else None
    wrong = gremlin.settle(inventory, lab)
    if wrong:
        raise GremlinError(f"The lab didn't come back after repairing {' and '.join(names)}: {', '.join(wrong)}.")
    return {
        "when": when.isoformat(timespec="seconds"),
        "lab": lab.name,
        # A pair goes on the scoreboard as one column, cost+auth.
        "fault": "+".join(names),
        "faults": names,
        # Where each was, and whether it could have been anywhere or was in its usual place.
        "variants": [fault.key for fault in faults],
        "anywhere": anywhere,
        "rubric": rubric(faults, answer),
        "seconds": round(seconds, 1),
        "replies": sum(message["role"] == "assistant" for message in chat.messages) if answer is not None else None,
        # Whether the answer came from the reply with no tools offered, after every other reply called tools.
        "last_word": answer is not None and any(message["content"] == OUT_OF_TOOLS for message in chat.messages),
        # Names and arguments. Rounds played before the arguments were kept list only the names.
        "tool_calls": watcher.calls,
        "tool_errors": watcher.errors,
        "answer": answer,
        "error": error,
        "transcript": transcript,
    }


def show_round(record: dict[str, Any]) -> None:
    faults = faults_of(record)
    disagreement = f"; the rubric said {record['rubric']}" if record.get("rubric", record["verdict"]) != record["verdict"] else ""
    last_word = ", answered when out of tool calls" if record.get("last_word") else ""
    where = " + ".join(record["variants"]) if record.get("anywhere") else record["fault"]
    print(
        f"{where}: {record['verdict'].upper()} in {record['seconds']:.0f} s, "
        f"{len(record['tool_calls'])} tool calls ({record['tool_errors']} errors){last_word}{disagreement}"
    )
    print(f"  answer: {' '.join((record['answer'] or record['error']).split())}")
    if record.get("reason"):
        print(f"  judge:  {record['reason']}")
    for fault in faults:
        print(f"  cause:  {fault.answer}", flush=True)


async def play(args: argparse.Namespace) -> None:
    lab = gremlin.LABS[args.lab]
    strays = [name for name in args.faults or [] if name not in lab.faults]
    if strays:
        raise GremlinError(f"Not faults in the {lab.name} lab: {', '.join(strays)}. Its faults are {', '.join(lab.faults)}.")
    inventory = Inventory.load(args.inventory or lab.inventory)
    if args.anywhere and not lab.variants:
        raise GremlinError(f"The {lab.name} lab's faults each have one place.")
    if args.pairs:
        breaks = [list(pair) for pair in lab.pairs] * args.rounds
    else:
        breaks = [[name] for name in args.faults or lab.faults] * args.rounds
    random.shuffle(breaks)
    version = netgpt_version()
    async with (
        Client(build_server(Engine(inventory))) as client,
        httpx.AsyncClient(base_url=args.ollama, timeout=httpx.Timeout(args.timeout, connect=10.0)) as http,
    ):
        think = args.think if args.think is not None else await supports_thinking(http, args.model)
        chat = Chat(client, http, args.model, {"num_ctx": args.context}, think=think, max_rounds=args.max_rounds)
        await chat.start()
        for number, names in enumerate(breaks, 1):
            faults = gremlin.place(lab, names, args.anywhere)
            print(f"\nRound {number} of {len(breaks)}: {gremlin.complaint(faults)}", flush=True)
            record = await play_round(chat, inventory, lab, faults, args.anywhere)
            await judge_round(http, args.judge or args.model, record)
            record.update(model=args.model, think=think, max_rounds=args.max_rounds, version=version)
            with SCOREBOARD.open("a") as log:
                log.write(json.dumps(record) + "\n")
            show_round(record)


def load_scoreboard() -> list[dict[str, Any]]:
    return [json.loads(line) for line in SCOREBOARD.read_text().splitlines()] if SCOREBOARD.exists() else []


async def rejudge(args: argparse.Namespace) -> None:
    """
    Judge every round on the scoreboard again, so that after the judge changes, every
    round is scored by the same judge. Prints the rounds whose verdict changed. Rounds
    from before there was a judge carry the rubric's verdict, which is kept as their
    rubric. Don't run it while a game is playing: it rewrites the scoreboard.
    """
    records = load_scoreboard()
    async with httpx.AsyncClient(base_url=args.ollama, timeout=httpx.Timeout(args.timeout, connect=10.0)) as http:
        for record in records:
            before = record["verdict"]
            record.setdefault("rubric", record["verdict"])
            await judge_round(http, args.judge or args.model, record)
            if record["verdict"] != before:
                print(f"\n{record['when']} {record['model']} {record['version']}, was {before}")
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
    for lab in gremlin.LABS.values():
        # Rounds from before there were two labs were all played in the fabric.
        played = [record for record in records if record.get("lab", "fabric") == lab.name]
        if played:
            print(f"\nThe {lab.name} lab:")
            table(lab, played)
    if answers:
        for record in records:
            print(f"\n{record['when']} {record['model']} @ {record['version']}")
            show_round(record)


def table(lab: Lab, records: list[dict[str, Any]]) -> None:
    """One lab's rounds: found/played per fault, for each model, NetGPT version, and limit on replies."""
    groups: dict[tuple[str, str, int, bool], list[dict[str, Any]]] = {}
    for record in records:
        key = (record["model"], record["version"], record["max_rounds"], bool(record.get("anywhere")))
        groups.setdefault(key, []).append(record)
    # Pairs the lab plays now, and any it played before.
    pairs = dict.fromkeys(["+".join(pair) for pair in lab.pairs] + [r["fault"] for r in records if "+" in r["fault"]])
    names = list(lab.faults) + [pair for pair in pairs if any(record["fault"] == pair for record in records)]
    widths = [max(12, len(name) + 2) for name in names]
    print(
        f"{'model':<30} {'netgpt':<16}{'replies':>8}{'places':>10}"
        + "".join(f"{name:>{width}}" for name, width in zip(names, widths))
        + f"{'found':>8}{'partly':>8}{'no answer':>11}{'last word':>11}{'median s':>10}"
    )
    for (model, version, limit, anywhere), rounds in groups.items():
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
            f"{model:<30} {version:<16}{f'≤{limit}':>8}{'anywhere' if anywhere else 'usual':>10}"
            + "".join(f"{cell:>{width}}" for cell, width in zip(cells, widths))
            + f"{f'{found}/{len(rounds)}':>8}{partly:>8}{unanswered:>11}{last_word:>11}{median:>10.0f}"
        )


def main() -> None:
    parser = argparse.ArgumentParser(description="Play the gremlin's game: break the lab, ask a model why, keep score.")
    parser.add_argument("--lab", choices=list(gremlin.LABS), default="fabric", help="The lab to play in (default: %(default)s).")
    parser.add_argument("--faults", nargs="+", choices=list(gremlin.FAULTS), help="Faults to play (default: all the lab's).")
    parser.add_argument("--pairs", action="store_true", help="Play the lab's pairs of faults, two broken at once.")
    parser.add_argument(
        "--anywhere", action="store_true", help="Break each fault in one of its places at random, not its usual one (WAN)."
    )
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
    parser.add_argument("--inventory", type=Path, help="The lab's inventory (default: the lab's own).")
    parser.add_argument("--scoreboard", action="store_true", help="Print the scoreboard instead of playing.")
    parser.add_argument("--answers", action="store_true", help="With --scoreboard, print every answer and its cause.")
    parser.add_argument("--rejudge", action="store_true", help="Judge every round again, instead of playing.")
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

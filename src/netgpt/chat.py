"""
netgpt-chat talks to the network through a local model served by Ollama.

It builds the NetGPT MCP server in-process and drives it through an MCP client,
so the model sees exactly the tools and instructions any other MCP client sees,
and device output never leaves the machine. The chat runs the tool-calling loop
itself: each model reply either calls tools, whose results go back to the
model, or answers the question.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import re
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any

import httpx
from mcp import Client
from mcp_types import CallToolResult, Tool
from prompt_toolkit import PromptSession

from netgpt.inventory import Inventory
from netgpt.server import build_server

DEFAULT_MODEL = "gemma4:e4b"
DEFAULT_OLLAMA = "http://127.0.0.1:11434"

logger = logging.getLogger("netgpt.chat")

# ANSI colors for progress lines on a terminal.
GRAY, YELLOW, RED = "90", "33", "31"

# Ollama clamps the context to what the model supports, so asking for 128K is safe.
DEFAULT_CONTEXT = 131072

# Small models sometimes answer a tool result with nothing, or with a bare control
# token such as <|tool_response>. Chat.ask avoids the common cause; for the rest, a
# fresh sample of the same turn is usually a real answer.
BLANK_REPLY = re.compile(r"^\s*(<\|?[a-z_]+\|?>\s*)*$")
BLANK_REPLY_RETRIES = 2

ToolCallObserver = Callable[[str, dict[str, Any], str, bool], None]


class ChatError(RuntimeError):
    """The model or Ollama failed to produce an answer."""


def ollama_tools(tools: list[Tool]) -> list[dict[str, Any]]:
    """Describe MCP tools in Ollama's function-calling format."""
    return [
        {
            "type": "function",
            "function": {"name": tool.name, "description": tool.description or "", "parameters": tool.input_schema},
        }
        for tool in tools
    ]


def result_text(result: CallToolResult) -> str:
    """Render a tool result for the model: structured content as JSON, otherwise its text."""
    if result.structured_content is not None:
        return json.dumps(result.structured_content, separators=(",", ":"))
    return "\n".join(getattr(block, "text", "") for block in result.content)


def ollama_error(response: httpx.Response) -> str:
    """Pull the error message out of an Ollama error response."""
    try:
        return str(response.json()["error"])
    except (ValueError, KeyError, TypeError):
        return response.text.strip()[:300]


async def ollama_post(http: httpx.AsyncClient, path: str, body: dict[str, Any]) -> dict[str, Any]:
    """POST to the Ollama API, turning HTTP and connection failures into ChatError."""
    try:
        response = await http.post(path, json=body)
        response.raise_for_status()
    except httpx.HTTPStatusError as error:
        raise ChatError(f"Ollama: {ollama_error(error.response)}") from error
    except httpx.HTTPError as error:
        raise ChatError(f"Cannot reach Ollama at {http.base_url}: {error!r}") from error
    return response.json()


async def supports_thinking(http: httpx.AsyncClient, model: str) -> bool:
    """Ask Ollama whether a model can reason before answering."""
    details = await ollama_post(http, "/api/show", {"model": model})
    return "thinking" in details.get("capabilities", [])


class Chat:
    """
    One conversation between a local model and NetGPT's tools. The history carries
    over from question to question until reset() clears it.
    """

    def __init__(
        self,
        client: Client,
        http: httpx.AsyncClient,
        model: str,
        options: dict[str, Any],
        think: bool = False,
        max_rounds: int = 10,
        on_tool_call: ToolCallObserver | None = None,
    ) -> None:
        """client must already be connected; http's base URL is the Ollama server."""
        self.model = model
        self.think = think
        self._client = client
        self._http = http
        self._options = options
        self._max_rounds = max_rounds
        self._on_tool_call = on_tool_call
        self._tools: list[dict[str, Any]] = []
        self._instructions = ""
        self.messages: list[dict[str, Any]] = []

    async def start(self) -> None:
        """Fetch the server's tools and instructions and begin an empty conversation."""
        self._tools = ollama_tools((await self._client.list_tools()).tools)
        self._instructions = self._client.instructions or ""
        self.reset()

    def reset(self) -> None:
        self.messages = [{"role": "system", "content": self._instructions}]

    async def ask(self, question: str) -> str:
        """Answer one question, calling tools as the model asks for them."""
        checkpoint = len(self.messages)
        self.messages.append({"role": "user", "content": question})
        try:
            for _ in range(self._max_rounds):
                message = await self._reply()
                calls = message.get("tool_calls") or []
                if not calls:
                    self.messages.append(message)
                    return message.get("content", "").strip()
                # Keep the calls but drop any narration that came with them ("First I'll
                # list the devices..."). With it left in, gemma4 under Ollama answers the
                # tool results with an empty turn every time.
                self.messages.append({**message, "content": ""})
                for call in calls:
                    self.messages.append(await self._run_tool(call))
            raise ChatError(f"{self.model} was still calling tools after {self._max_rounds} turns.")
        except ChatError:
            # Drop the unanswered question so the conversation can carry on.
            del self.messages[checkpoint:]
            raise

    async def _reply(self) -> dict[str, Any]:
        """Ask the model for its next message, resampling a blank reply."""
        request = {
            "model": self.model,
            "messages": self.messages,
            "tools": self._tools,
            "stream": False,
            "think": self.think,
            "options": self._options,
        }
        for _ in range(BLANK_REPLY_RETRIES + 1):
            message = (await ollama_post(self._http, "/api/chat", request))["message"]
            if message.get("tool_calls") or not BLANK_REPLY.match(message.get("content", "")):
                return message
            logger.warning("Resampling a blank reply from %s: %r", self.model, message)
        raise ChatError(f"{self.model} gave a blank reply {BLANK_REPLY_RETRIES + 1} times in a row.")

    async def _run_tool(self, call: dict[str, Any]) -> dict[str, Any]:
        """Run one tool call and return the tool message that carries its result."""
        name = call["function"]["name"]
        arguments = call["function"].get("arguments") or {}
        # Unknown tools and invalid arguments come back as error results for the model to read.
        result = await self._client.call_tool(name, arguments)
        text = result_text(result)
        if self._on_tool_call is not None:
            self._on_tool_call(name, arguments, text, result.is_error)
        message = {"role": "tool", "tool_name": name, "content": text}
        if "id" in call:
            message["tool_call_id"] = call["id"]
        return message


def progress(text: str, color: str) -> None:
    """
    Print a progress line to stderr, in an ANSI color when stderr is a terminal.
    Answers go to stdout, so redirecting stdout captures only the answers.
    """
    if sys.stderr.isatty():
        text = f"\033[{color}m{text}\033[0m"
    print(text, file=sys.stderr, flush=True)


def tool_call_printer(verbose: bool) -> ToolCallObserver:
    """Print each tool call as it happens. Errors are always shown; results only when verbose."""

    def show(name: str, arguments: dict[str, Any], text: str, is_error: bool) -> None:
        rendered = ", ".join(f"{key}={value!r}" for key, value in arguments.items())
        line = f"  · {name}({rendered})"
        if is_error:
            line += f"  ✗ {' '.join(text.split())[:300]}"
        elif verbose:
            line += f"  → {text[:500]}"
        progress(line, YELLOW if is_error else GRAY)

    return show


async def interactive(chat: Chat) -> None:
    """Read questions from the terminal until Ctrl-D or /quit."""
    session: PromptSession[str] = PromptSession()
    thinking = "thinking" if chat.think else "not thinking"
    print(f"NetGPT chat with {chat.model} ({thinking}). /reset clears the conversation; Ctrl-D quits.")
    while True:
        try:
            line = (await session.prompt_async("netgpt> ")).strip()
        except EOFError:
            return
        except KeyboardInterrupt:
            continue
        if line in ("/quit", "/exit"):
            return
        if line == "/reset":
            chat.reset()
            print("Conversation cleared.", flush=True)
        elif line:
            try:
                print(f"\n{await chat.ask(line)}\n", flush=True)
            except ChatError as error:
                progress(str(error), RED)


async def run(args: argparse.Namespace) -> int:
    """Answer the questions on the command line, or chat interactively. Returns the exit status."""
    inventory = Inventory.load(Path(args.inventory))
    options: dict[str, Any] = {"num_ctx": args.context}
    if args.threads:
        options["num_thread"] = args.threads
    timeout = httpx.Timeout(args.timeout, connect=10.0)
    async with (
        Client(build_server(inventory)) as client,
        httpx.AsyncClient(base_url=args.ollama, timeout=timeout) as http,
    ):
        try:
            think = args.think if args.think is not None else await supports_thinking(http, args.model)
        except ChatError as error:
            progress(str(error), RED)
            return 1
        chat = Chat(
            client,
            http,
            args.model,
            options,
            think=think,
            max_rounds=args.max_rounds,
            on_tool_call=tool_call_printer(args.verbose),
        )
        await chat.start()
        if not args.questions:
            await interactive(chat)
            return 0
        status = 0
        for question in args.questions:
            chat.reset()
            if len(args.questions) > 1:
                print(f"\n> {question}", flush=True)
            try:
                print(await chat.ask(question), flush=True)
            except ChatError as error:
                progress(str(error), RED)
                status = 1
        return status


def main() -> None:
    """Chat with the network through a local Ollama model."""
    parser = argparse.ArgumentParser(
        description="Chat with the network through a local Ollama model and NetGPT's read-only tools."
    )
    parser.add_argument(
        "questions", nargs="*", help="Questions to answer, each in a fresh conversation. Omit them to chat."
    )
    parser.add_argument(
        "--inventory",
        type=Path,
        default=os.environ.get("NETGPT_INVENTORY"),
        help="Path to the inventory YAML file (default: $NETGPT_INVENTORY).",
    )
    parser.add_argument("--model", default=DEFAULT_MODEL, help=f"Ollama model (default: {DEFAULT_MODEL}).")
    parser.add_argument("--ollama", default=DEFAULT_OLLAMA, help=f"Ollama server URL (default: {DEFAULT_OLLAMA}).")
    parser.add_argument(
        "--context",
        type=int,
        default=DEFAULT_CONTEXT,
        help="Context window in tokens; Ollama caps it at the model's maximum (default: %(default)s).",
    )
    parser.add_argument("--threads", type=int, default=None, help="CPU threads for inference (default: Ollama's choice).")
    parser.add_argument(
        "--think",
        action=argparse.BooleanOptionalAction,
        default=None,
        help="Let the model reason before each reply: much slower, but far better at multi-step questions "
        "(default: on when the model supports it).",
    )
    parser.add_argument("--max-rounds", type=int, default=10, help="Model replies allowed per question.")
    parser.add_argument("--timeout", type=float, default=900.0, help="Seconds to wait for one model reply.")
    parser.add_argument("--verbose", action="store_true", help="Show tool results as well as tool calls.")
    parser.add_argument("--log-level", default="ERROR", help="Logging level, written to stderr.")
    args = parser.parse_args()
    if args.inventory is None:
        parser.error("--inventory is required (or set NETGPT_INVENTORY).")

    logging.basicConfig(level=args.log_level, stream=sys.stderr)
    sys.exit(asyncio.run(run(args)))

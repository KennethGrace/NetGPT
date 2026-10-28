"""
netgpt-chat talks to the network through a local model served by Ollama.

It builds the NetGPT MCP server in-process and drives it through an MCP client,
so the model sees exactly the tools and instructions any other MCP client sees,
and device output never leaves the machine. The chat runs the tool-calling loop
itself: each model reply either calls tools, whose results go back to the
model, or answers the question. Replies stream from Ollama, and a ChatObserver
sees the model's thinking, text, and tool calls as they arrive, which is how the
terminal and the web page (web.py) show progress.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import re
import secrets
import sys
from pathlib import Path
from typing import Any

import httpx
from mcp import Client
from mcp_types import CallToolResult, Tool
from prompt_toolkit import PromptSession

from netgpt.inventory import Inventory
from netgpt.server import build_server
from netgpt.statistics import DEFAULT_INTERVAL, Poller

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

# When the model has spent every reply on tool calls, it gets one more with no tools offered, and this note,
# so what it found becomes an answer instead of an error.
OUT_OF_TOOLS = (
    "That was your last tool call for this question. Answer it now from what you have found, "
    "and say what you could not check."
)

# Added to the server's instructions. Small models make one tool call per reply, a reply each time, unless
# told that one reply can make several; and the budget is counted in replies.
SEVERAL_CALLS = (
    "Each question allows {replies} replies that call tools. One reply can call several tools: when you need "
    "several things that don't depend on each other (tables, paths, pings), ask for them all in the same reply."
)


class ChatError(RuntimeError):
    """The model or Ollama failed to produce an answer."""


class ChatObserver:
    """Receives a question's progress as it happens. This base class ignores all of it."""

    def thinking(self, text: str) -> None:
        """The next piece of the model's reasoning."""

    def content(self, text: str) -> None:
        """The next piece of the model's reply text."""

    def tool_call(
        self, name: str, arguments: dict[str, Any], result: str, is_error: bool, structured: dict[str, Any] | None = None
    ) -> None:
        """A tool call the model made, the result the model read, and the result's structured content."""


def plain_schema(schema: Any, definitions: dict[str, Any] | None = None) -> Any:
    """
    Write a JSON schema as plainly as possible for a small model. Ollama keeps no
    $ref, so referenced definitions are written in place; and "X or null" becomes
    X, since leaving an optional parameter out already says it may be empty.
    """
    if isinstance(schema, list):
        return [plain_schema(item, definitions) for item in schema]
    if not isinstance(schema, dict):
        return schema
    definitions = schema.get("$defs", definitions) or {}
    rest = {key: value for key, value in schema.items() if key not in ("$defs", "$ref", "anyOf")}
    if "$ref" in schema:
        return plain_schema({**definitions[schema["$ref"].rsplit("/", 1)[-1]], **rest}, definitions)
    options = [option for option in schema.get("anyOf", []) if option != {"type": "null"}]
    if len(options) == 1:
        return plain_schema({**options[0], **rest}, definitions)
    if options:
        rest["anyOf"] = options
    return {key: plain_schema(value, definitions) for key, value in rest.items()}


def ollama_tools(tools: list[Tool]) -> list[dict[str, Any]]:
    """Describe MCP tools in Ollama's function-calling format."""
    return [
        {
            "type": "function",
            "function": {
                "name": tool.name,
                "description": tool.description or "",
                "parameters": plain_schema(tool.input_schema),
            },
        }
        for tool in tools
    ]


def result_text(result: CallToolResult) -> str:
    """
    Render a tool result for the model: its content, as any MCP client would show
    it, with JSON compacted to save tokens. Structured content is for programs; a
    tool such as display gives the model a summary there and the page the drawing.
    """
    texts = []
    for block in result.content:
        text = getattr(block, "text", "")
        try:
            text = json.dumps(json.loads(text), separators=(",", ":"), ensure_ascii=False)
        except ValueError:
            pass
        texts.append(text)
    return "\n".join(texts)


def for_ollama(message: dict[str, Any]) -> dict[str, Any]:
    """A history message without the keys Chat keeps for itself, which start with an underscore."""
    return {key: value for key, value in message.items() if not key.startswith("_")}


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
    ) -> None:
        """
        client must already be connected; http's base URL is the Ollama server.
        max_rounds is how many replies to a question may call tools. If they all
        do, one more reply, with no tools offered, answers from what they found.
        """
        self.model = model
        self.think = think
        self._client = client
        self._http = http
        self._options = options
        self._max_rounds = max_rounds
        self._tools: list[dict[str, Any]] = []
        self._instructions = ""
        self.messages: list[dict[str, Any]] = []

    async def start(self) -> None:
        """Fetch the server's tools and instructions and begin an empty conversation."""
        self._tools = ollama_tools((await self._client.list_tools()).tools)
        self._instructions = f"{self._client.instructions or ''}\n{SEVERAL_CALLS.format(replies=self._max_rounds)}"
        self.reset()

    def reset(self) -> None:
        self.messages = [{"role": "system", "content": self._instructions}]

    async def ask(self, question: str, observer: ChatObserver | None = None) -> str:
        """Answer one question, calling tools as the model asks for them."""
        observer = observer or ChatObserver()
        checkpoint = len(self.messages)
        self.messages.append({"role": "user", "content": question})
        try:
            for _ in range(self._max_rounds):
                message = await self._reply(observer)
                calls = message.get("tool_calls") or []
                if not calls:
                    self.messages.append(message)
                    return message["content"].strip()
                # Keep the calls but drop any narration that came with them ("First I'll
                # list the devices..."). With it left in, gemma4 under Ollama answers the
                # tool results with an empty turn every time.
                self.messages.append({**message, "content": ""})
                for call in calls:
                    self.messages.append(await self._run_tool(call, observer))
            return await self._last_word(observer)
        except ChatError:
            # Drop the unanswered question so the conversation can carry on.
            del self.messages[checkpoint:]
            raise

    async def _last_word(self, observer: ChatObserver) -> str:
        """Ask for an answer with no tools offered, once every reply has gone on tool calls."""
        self.messages.append({"role": "user", "content": OUT_OF_TOOLS})
        message = await self._reply(observer, tools=False)
        if not message["content"].strip():
            raise ChatError(f"{self.model} was still calling tools after {self._max_rounds} turns.")
        self.messages.append({key: value for key, value in message.items() if key != "tool_calls"})
        return message["content"].strip()

    async def _reply(self, observer: ChatObserver, tools: bool = True) -> dict[str, Any]:
        """Get the model's next message, resampling a blank reply."""
        for _ in range(BLANK_REPLY_RETRIES + 1):
            message = await self._stream_reply(observer, tools)
            if message.get("tool_calls") or not BLANK_REPLY.match(message["content"]):
                return message
            logger.warning("Resampling a blank reply from %s: %r", self.model, message)
        raise ChatError(f"{self.model} gave a blank reply {BLANK_REPLY_RETRIES + 1} times in a row.")

    async def _stream_reply(self, observer: ChatObserver, tools: bool = True) -> dict[str, Any]:
        """Stream one reply from Ollama, passing its thinking and text to the observer as they arrive."""
        request = {
            "model": self.model,
            "messages": [for_ollama(message) for message in self.messages],
            "tools": self._tools if tools else [],
            "stream": True,
            "think": self.think,
            "options": self._options,
        }
        thinking, content, tool_calls = "", "", []
        try:
            async with self._http.stream("POST", "/api/chat", json=request) as response:
                if response.is_error:
                    await response.aread()
                    raise ChatError(f"Ollama: {ollama_error(response)}")
                async for line in response.aiter_lines():
                    if not line.strip():
                        continue
                    chunk = json.loads(line)
                    if "error" in chunk:
                        raise ChatError(f"Ollama: {chunk['error']}")
                    delta = chunk.get("message", {})
                    if delta.get("thinking"):
                        thinking += delta["thinking"]
                        observer.thinking(delta["thinking"])
                    if delta.get("content"):
                        content += delta["content"]
                        observer.content(delta["content"])
                    tool_calls.extend(delta.get("tool_calls") or [])
        except httpx.HTTPError as error:
            raise ChatError(f"Cannot reach Ollama at {self._http.base_url}: {error!r}") from error
        message: dict[str, Any] = {"role": "assistant", "content": content}
        if thinking:
            message["thinking"] = thinking
        if tool_calls:
            message["tool_calls"] = tool_calls
        return message

    async def _run_tool(self, call: dict[str, Any], observer: ChatObserver) -> dict[str, Any]:
        """Run one tool call and return the tool message that carries its result."""
        name = call["function"]["name"]
        arguments = call["function"].get("arguments") or {}
        # Unknown tools and invalid arguments come back as error results for the model to read.
        result = await self._client.call_tool(name, arguments)
        text = result_text(result)
        observer.tool_call(name, arguments, text, result.is_error, result.structured_content)
        message = {"role": "tool", "tool_name": name, "content": text}
        if "id" in call:
            message["tool_call_id"] = call["id"]
        if result.structured_content is not None:
            # Kept so a page can redraw what a tool showed; never sent to the model.
            message["_structured"] = result.structured_content
        return message


def progress(text: str, color: str) -> None:
    """
    Print a progress line to stderr, in an ANSI color when stderr is a terminal.
    Answers go to stdout, so redirecting stdout captures only the answers.
    """
    if sys.stderr.isatty():
        text = f"\033[{color}m{text}\033[0m"
    print(text, file=sys.stderr, flush=True)


class TerminalObserver(ChatObserver):
    """Prints each tool call as it happens. Errors are always shown; results only when verbose."""

    def __init__(self, verbose: bool) -> None:
        self._verbose = verbose

    def tool_call(
        self, name: str, arguments: dict[str, Any], result: str, is_error: bool, structured: dict[str, Any] | None = None
    ) -> None:
        rendered = ", ".join(f"{key}={value!r}" for key, value in arguments.items())
        line = f"  · {name}({rendered})"
        if is_error:
            line += f"  ✗ {' '.join(result.split())[:300]}"
        elif self._verbose:
            line += f"  → {result[:500]}"
        progress(line, YELLOW if is_error else GRAY)


async def interactive(chat: Chat, observer: ChatObserver) -> None:
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
                print(f"\n{await chat.ask(line, observer)}\n", flush=True)
            except ChatError as error:
                progress(str(error), RED)


def web_address(value: str) -> tuple[str, int]:
    """Parse [HOST:]PORT for --web; the host defaults to loopback."""
    host, _, port = value.rpartition(":")
    if not port.isdigit() or not 0 < int(port) < 65536:
        raise argparse.ArgumentTypeError(f"'{value}' is not [HOST:]PORT.")
    return host.strip("[]") or "127.0.0.1", int(port)


async def run(args: argparse.Namespace) -> int:
    """Serve the web page, answer the questions given, or chat in the terminal. Returns the exit status."""
    inventory = Inventory.load(Path(args.inventory))
    options: dict[str, Any] = {"num_ctx": args.context}
    if args.threads:
        options["num_thread"] = args.threads
    timeout = httpx.Timeout(args.timeout, connect=10.0)
    statistics = Poller(inventory, interval=args.poll_interval) if args.poll_interval > 0 else None
    # Only the web page can show the map, so only it gets the tool that draws one.
    async with (
        Client(build_server(inventory, display=args.web is not None, statistics=statistics)) as client,
        httpx.AsyncClient(base_url=args.ollama, timeout=timeout) as http,
    ):
        try:
            think = args.think if args.think is not None else await supports_thinking(http, args.model)
        except ChatError as error:
            progress(str(error), RED)
            return 1
        chat = Chat(client, http, args.model, options, think=think, max_rounds=args.max_rounds)
        await chat.start()
        if args.web:
            # Imported here because the web page builds on this module.
            from netgpt.web import serve

            host, port = args.web
            token = os.environ.get("NETGPT_WEB_TOKEN") or secrets.token_urlsafe(24)
            progress(f"NetGPT chat with {chat.model} at http://{host}:{port}/?token={token}", GRAY)
            await serve(chat, host, port, token, statistics)
            return 0
        observer = TerminalObserver(args.verbose)
        if not args.questions:
            await interactive(chat, observer)
            return 0
        status = 0
        for question in args.questions:
            chat.reset()
            if len(args.questions) > 1:
                print(f"\n> {question}", flush=True)
            try:
                print(await chat.ask(question, observer), flush=True)
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
    parser.add_argument(
        "--max-rounds",
        type=int,
        default=10,
        help="Replies per question that may call tools. If they all do, one more answers without tools "
        "(default: %(default)s).",
    )
    parser.add_argument(
        "--timeout", type=float, default=900.0, help="Seconds to wait for Ollama to send anything (default: %(default)s)."
    )
    parser.add_argument("--verbose", action="store_true", help="Show tool results as well as tool calls.")
    parser.add_argument(
        "--poll-interval",
        type=float,
        default=DEFAULT_INTERVAL,
        help="Seconds between polls of every device's interface counters, which interface_traffic reads. "
        "0 turns polling, and the tool, off (default: %(default)g).",
    )
    parser.add_argument(
        "--web",
        type=web_address,
        metavar="[HOST:]PORT",
        help="Serve the chat as a web page instead of the terminal. HOST defaults to 127.0.0.1. "
        "Sign-in uses $NETGPT_WEB_TOKEN, or a random token printed at startup.",
    )
    parser.add_argument("--log-level", default="ERROR", help="Logging level, written to stderr.")
    args = parser.parse_args()
    if args.inventory is None:
        parser.error("--inventory is required (or set NETGPT_INVENTORY).")
    if args.poll_interval < 0:
        parser.error("--poll-interval can't be negative; 0 turns polling off.")
    if args.web and args.questions:
        parser.error("--web serves a page for asking questions; leave the questions off the command line.")

    logging.basicConfig(level=args.log_level, stream=sys.stderr)
    sys.exit(asyncio.run(run(args)))

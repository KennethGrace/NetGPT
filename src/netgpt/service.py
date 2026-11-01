"""
The service module runs the engine on its own, with no model behind it:
netgpt-engine. It serves the engine's API (netgpt.api) at /api/v1 over HTTP, for
frontends in other repositories and for scripts, and polls the interface counters
for as long as it runs, not only while a client is connected.

It serves plain HTTP to whoever holds the token, so bind it to an address only
trusted machines can reach. The token comes from $NETGPT_ENGINE_TOKEN, or is made
up at startup and printed once.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import logging
import os
import secrets
import sys
from collections.abc import AsyncIterator
from pathlib import Path

import uvicorn
from starlette.applications import Starlette
from starlette.routing import Mount

from netgpt.api import build_api
from netgpt.engine import Engine
from netgpt.inventory import Inventory
from netgpt.statistics import DEFAULT_INTERVAL, Poller
from netgpt.store import STORE_HELP, StoreError, check_store, open_store

TOKEN_ENV = "NETGPT_ENGINE_TOKEN"
DEFAULT_LISTEN = "127.0.0.1:8081"


def listen_address(value: str) -> tuple[str, int]:
    """Parse [HOST:]PORT for an address to listen on; the host defaults to loopback."""
    host, _, port = value.rpartition(":")
    if not port.isdigit() or not 0 < int(port) < 65536:
        raise argparse.ArgumentTypeError(f"'{value}' is not [HOST:]PORT.")
    return host.strip("[]") or "127.0.0.1", int(port)


def build_service(engine: Engine, token: str) -> Starlette:
    """The engine's API at /api/v1, with the engine running (and polling, if it polls) as long as the service runs."""

    @contextlib.asynccontextmanager
    async def running(_: Starlette) -> AsyncIterator[None]:
        async with engine.running():
            yield

    return Starlette(routes=[Mount("/api/v1", app=build_api(engine, token))], lifespan=running)


def main() -> None:
    """Serve the engine's API over HTTP until interrupted."""
    parser = argparse.ArgumentParser(description="Serve NetGPT's engine over HTTP, with no model behind it: the API at /api/v1.")
    parser.add_argument(
        "--inventory",
        type=Path,
        default=os.environ.get("NETGPT_INVENTORY"),
        help="Path to the inventory YAML file (default: $NETGPT_INVENTORY).",
    )
    parser.add_argument(
        "--listen",
        type=listen_address,
        default=DEFAULT_LISTEN,
        metavar="[HOST:]PORT",
        help="The address to serve on. HOST defaults to 127.0.0.1 (default: %(default)s).",
    )
    parser.add_argument(
        "--poll-interval",
        type=float,
        default=DEFAULT_INTERVAL,
        help="Seconds between polls of every device's interface counters, which the traffic and chart routes read. "
        "0 turns polling, and those routes, off (default: %(default)g).",
    )
    parser.add_argument("--store", metavar="memory|URL", help=STORE_HELP)
    parser.add_argument("--log-level", default="INFO", help="Logging level, written to stderr.")
    args = parser.parse_args()
    if args.inventory is None:
        parser.error("--inventory is required (or set NETGPT_INVENTORY).")
    if args.poll_interval < 0:
        parser.error("--poll-interval can't be negative; 0 turns polling off.")

    logging.basicConfig(level=args.log_level, stream=sys.stderr)
    inventory = Inventory.load(Path(args.inventory))
    statistics = None
    if args.poll_interval > 0:
        try:
            store = open_store(args.store)
            # Find out now if the store can't be used, rather than when the service starts polling.
            asyncio.run(check_store(store))
        except StoreError as error:
            parser.error(str(error))
        statistics = Poller(inventory, interval=args.poll_interval, store=store)
    host, port = args.listen
    token = os.environ.get(TOKEN_ENV)
    if token:
        print(f"NetGPT engine at http://{host}:{port}/api/v1/", file=sys.stderr)
    else:
        token = secrets.token_urlsafe(24)
        print(f"NetGPT engine at http://{host}:{port}/api/v1/ with the token {token}", file=sys.stderr)
    service = build_service(Engine(inventory, statistics=statistics), token)
    uvicorn.run(service, host=host, port=port, log_level=args.log_level.lower())

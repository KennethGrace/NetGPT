"""
Record the lab's answers as test fixtures: every device's JSON-RPC responses for
the tables the topology and the forwarding walk read, captured through the real
SR Linux driver so the tests replay exactly what it asks for.

Run it from the repository root with the lab deployed and the tables filled (sweep
the host networks from their leaves first):

    python lab/record_fixtures.py

It writes tests/fixtures/srlinux/lab/<device>/<table>.json. Check the diff before
committing: a redeploy changes MAC addresses, and the tests that name them.
"""

from __future__ import annotations

import argparse
import asyncio
import json
from collections.abc import Awaitable, Callable
from pathlib import Path

import httpx

from netgpt.inventory import Inventory
from netgpt.platforms.srlinux import SRLinuxPlatform

TABLES: dict[str, Callable[[SRLinuxPlatform], Awaitable[list]]] = {
    "lldp": lambda platform: platform.lldp_neighbors(),
    "arp": lambda platform: platform.arp_table(),
    "mac_table": lambda platform: platform.mac_table(),
    "interfaces": lambda platform: platform.interfaces(),
    "route_table": lambda platform: platform.routes("default"),
}


class Recording(httpx.AsyncBaseTransport):
    """Passes each request to the device and keeps the response body."""

    def __init__(self, verify: str | bool) -> None:
        self.inner = httpx.AsyncHTTPTransport(verify=verify)
        self.body = b""

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        response = await self.inner.handle_async_request(request)
        self.body = await response.aread()
        # The body is decoded now, so it no longer matches the encoding headers.
        headers = [(key, value) for key, value in response.headers.items() if key.lower() not in ("content-encoding", "content-length")]
        return httpx.Response(response.status_code, headers=headers, content=self.body)

    async def aclose(self) -> None:
        await self.inner.aclose()


async def record(inventory: Inventory, out: Path) -> None:
    """Query every device for every table and write each response to its own file."""
    for device in inventory.devices:
        folder = out / device.name
        folder.mkdir(parents=True, exist_ok=True)
        for table, query in TABLES.items():
            transport = Recording(str(device.tls_ca) if device.tls_ca else True)
            async with SRLinuxPlatform(device, inventory.credentials_for(device), transport=transport) as platform:
                items = await query(platform)
            (folder / f"{table}.json").write_text(json.dumps(json.loads(transport.body), indent=1) + "\n")
            print(f"{device.name} {table}: {len(items)} records")


def main() -> None:
    parser = argparse.ArgumentParser(description="Record the lab's JSON-RPC responses as test fixtures.")
    parser.add_argument("--inventory", type=Path, default=Path("lab/inventory.yml"), help="The lab inventory.")
    parser.add_argument(
        "--out", type=Path, default=Path("tests/fixtures/srlinux/lab"), help="Where to write the fixtures."
    )
    args = parser.parse_args()
    asyncio.run(record(Inventory.load(args.inventory), args.out))


if __name__ == "__main__":
    main()

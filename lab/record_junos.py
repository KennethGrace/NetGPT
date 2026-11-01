"""
Record Junos devices' NETCONF replies as test fixtures, through the real Junos
driver, so the tests replay exactly what it asks for.

The devices are real equipment and the repository is public, so every reply is
renamed on its way to disk: a YAML file kept outside the repository maps each
real name and address to a stand-in, longest first, and serial numbers are
blanked. Read the fixtures before committing them.

    python lab/record_junos.py --inventory .local/juniper/inventory.yml --rename .local/juniper/rename.yml

It writes tests/fixtures/junos/<device's stand-in name>/: one file per reply, and
replies.json, which says which RPC each one answers.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import re
from pathlib import Path

import yaml

from netgpt.inventory import Inventory
from netgpt.platforms.base import PlatformError
from netgpt.platforms.junos import JunosPlatform, NetconfOverSsh

# Commands recorded for the CLI tests: one Junos has, one it answers with nothing, and one it rejects.
COMMANDS = ["show bgp summary", "show ip route", "show bogus"]
SERIAL = re.compile(r"(<[\w:-]*serial[\w-]*>)[^<]*(</)")


class Renamer:
    """Replaces real names and addresses with stand-ins, and blanks serial numbers."""

    def __init__(self, names: dict[str, str]) -> None:
        self.names = sorted(names.items(), key=lambda item: len(item[0]), reverse=True)

    def __call__(self, text: str) -> str:
        for real, stand_in in self.names:
            text = text.replace(real, stand_in)
        return SERIAL.sub(r"\1SERIAL\2", text)


class Recording:
    """A NETCONF session that passes each RPC to the device and keeps the renamed reply."""

    def __init__(self, inner: NetconfOverSsh, folder: Path, rename: Renamer) -> None:
        self.inner = inner
        self.folder = folder
        self.rename = rename
        self.replies: dict[str, str] = {}

    async def rpc(self, body: str) -> str:
        reply = await self.inner.rpc(body)
        if self.rename(body) in self.replies:
            return reply  # Asked again by another query; the first reply serves both.
        name = f"{len(self.replies) + 1:02d}-{re.sub(r'[^a-z0-9]+', '-', re.sub(r'</[^>]*>', '', body.lower())).strip('-')[:80]}.xml"
        (self.folder / name).write_text(self.rename(reply).strip() + "\n")
        self.replies[self.rename(body)] = name
        return reply

    async def close(self) -> None:
        await self.inner.close()


async def record(inventory: Inventory, out: Path, rename: Renamer) -> None:
    """Ask every device each query the driver has, and write the replies it took."""
    for device in inventory.devices:
        folder = out / rename(device.name)
        folder.mkdir(parents=True, exist_ok=True)
        for old in folder.glob("*.xml"):
            old.unlink()
        credentials = inventory.credentials_for(device)
        session = Recording(NetconfOverSsh(device, credentials), folder, rename)
        async with JunosPlatform(device, credentials, session=session) as platform:
            await platform.lldp_neighbors()
            await platform.interface_counters()
            await platform.mac_table()
            await platform.arp_table()
            await platform.bgp_sessions()
            instances = {interface.network_instance for interface in await platform.interfaces() if interface.mode == "routed"}
            for instance in sorted(instances - {None, "mgmt_junos"}):
                await platform.routes(instance)
            for command in COMMANDS:
                try:
                    await platform.show(command)
                except PlatformError:
                    pass  # The failures are what the tests want.
        (folder / "replies.json").write_text(json.dumps(session.replies, indent=1) + "\n")
        print(f"{rename(device.name)}: {len(session.replies)} replies")


def main() -> None:
    parser = argparse.ArgumentParser(description="Record Junos devices' NETCONF replies as renamed test fixtures.")
    parser.add_argument("--inventory", type=Path, required=True, help="The inventory of Junos devices to record.")
    parser.add_argument("--rename", type=Path, required=True, help="YAML mapping each real name or address to a stand-in.")
    parser.add_argument("--out", type=Path, default=Path("tests/fixtures/junos"), help="Where to write the fixtures.")
    args = parser.parse_args()
    names = yaml.safe_load(args.rename.read_text())
    asyncio.run(record(Inventory.load(args.inventory), args.out, Renamer(names)))


if __name__ == "__main__":
    main()

"""
Record NX-OS devices' replies as test fixtures, through the real NX-OS driver, so
the tests replay exactly what it asks for.

The devices are real equipment on a network other people use, and the repository
is public, so every reply is made anonymous on its way to disk:

- Each table keeps only a few rows of each kind (each MAC entry type, each route
  owner, each LLDP port subtype), which is what the parser tests need.
- A YAML file kept outside the repository maps the inventory's names (and other
  known names, such as the domain) to stand-ins, longest first. Every other system
  name a neighbor sends becomes neighbor-N, and descriptions are blanked.
- Every IPv4 address moves to 198.18.0.0/15, a /24 at a time, so addresses on one
  network stay on one; 0.x.x.x (OSPF areas) and masks stay as they are.
- Every MAC address becomes a locally administered one, except the protocols' own
  (VRRP's, HSRP's, multicast), which say nothing about the network.

Read the fixtures before committing them.

    python lab/record_nxos.py --inventory .local/cisco/inventory.yml --rename .local/cisco/rename.yml

It writes tests/fixtures/nxos/<device's stand-in name>/: one file per reply, and
replies.json, which says which command each one answers and its exit status.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import re
import xml.etree.ElementTree as ElementTree
from pathlib import Path
from typing import Any

import yaml

from netgpt.inventory import Inventory
from netgpt.platforms.base import PlatformError
from netgpt.platforms.nxos import ExecOverSsh, NxosPlatform, Reply, as_dict

# Commands recorded for the CLI tests: one NX-OS has, with a pipe, and one it rejects.
COMMANDS = ["show vrf | include Up", "show bogus"]
# How many rows of each kind a table keeps, and how many in all.
PER_KIND, MOST_ROWS = 2, 24
# The fields that say what kind of row it is, and the fields that name an interface (by its kind: Eth, Vlan, ...).
KIND_FIELDS = {
    "disp_type", "disp_is_static", "port_type", "chassis_type", "portmode", "state", "clientname", "state_str",
    "type_str", "flags", "passive", "auth_type", "eth_mode", "proto", "addrf", "vrf-name-out", "cname", "ptag",
}
NAME_FIELDS = {"interface", "intf-name", "intf-out", "disp_port", "l_port_id", "ifname", "intf"}
ADDRESS = re.compile(r"(?<![\d.])(\d{1,3})\.(\d{1,3})\.(\d{1,3})\.(\d{1,3})(?![\d.])")
MAC = re.compile(r"\b[0-9a-f]{4}\.[0-9a-f]{4}\.[0-9a-f]{4}\b|\b(?:[0-9a-f]{2}:){5}[0-9a-f]{2}\b", re.IGNORECASE)
PROTOCOL_MACS = ("0000.0c07.ac", "0000.0c9f.f", "0000.5e00.01", "0000.5e00.02", "0100.5e", "0180.c2", "ffff.ffff.ffff", "3333.")
PORT_LIKE = re.compile(r"^[A-Za-z][\w/.:-]*\d$")
STAND_IN_NETWORKS = [f"198.{18 + index // 256}.{index % 256}" for index in range(512)]


class Anonymizer:
    """Stand-ins for names, addresses, and MACs, the same ones for every device in a recording."""

    def __init__(self, names: dict[str, str]) -> None:
        self.known = dict(names)
        self.names = sorted(names.items(), key=lambda item: len(item[0]), reverse=True)
        self.systems: dict[str, str] = {}
        self.networks: dict[str, str] = {}
        self.macs: dict[str, str] = {}

    def system(self, name: str) -> str:
        """
        A system name: its stand-in from the YAML file, or the next neighbor-N. Only a
        whole name in the file keeps one: renaming the domain alone would leave the host.
        """
        if name in self.known:
            return self.known[name]
        return self.systems.setdefault(name, f"neighbor-{len(self.systems) + 1}")

    def _address(self, match: re.Match[str]) -> str:
        octets = [int(octet) for octet in match.groups()]
        if octets[0] in (0, 255) or octets[0] >= 224 or any(octet > 255 for octet in octets):
            return match.group(0)
        network = ".".join(match.groups()[:3])
        if network not in self.networks:
            self.networks[network] = STAND_IN_NETWORKS[len(self.networks)]
        return f"{self.networks[network]}.{octets[3]}"

    def _mac(self, match: re.Match[str]) -> str:
        mac = match.group(0).lower()
        dotted = re.sub(r"[^0-9a-f]", "", mac)
        dotted = f"{dotted[0:4]}.{dotted[4:8]}.{dotted[8:12]}"
        if dotted.startswith(PROTOCOL_MACS):
            return match.group(0)
        if dotted not in self.macs:
            number = len(self.macs) + 1
            self.macs[dotted] = f"0200.{number >> 16:04x}.{number & 0xFFFF:04x}"
        stand_in = self.macs[dotted]
        if ":" in mac:
            digits = stand_in.replace(".", "")
            return ":".join(digits[index : index + 2] for index in range(0, 12, 2))
        return stand_in

    def text(self, text: str) -> str:
        for real, stand_in in self.names:
            text = text.replace(real, stand_in)
        return MAC.sub(self._mac, ADDRESS.sub(self._address, text))

    def field(self, key: str, value: str) -> str:
        """One field's value, with the free text in it (names, descriptions) replaced."""
        if key in ("sys_name", "hostname"):
            return self.system(value) if value not in ("", "null") else value
        if key in ("sys_desc", "desc"):
            return "description" if value not in ("", "null") else value
        if key == "port_desc" and value not in ("", "null", "not advertised") and not PORT_LIKE.match(value):
            return "a port description"
        return self.text(value)


def _kinds(node: Any, depth: int = 0) -> tuple[Any, ...]:
    """What makes a row one kind or another: its fields, the kind fields' values, and which kinds of interface it names."""
    if not isinstance(node, dict) or depth > 3:
        return ()
    found: list[Any] = [tuple(sorted(node)) if depth == 0 else ()]
    for key, value in sorted(node.items()):
        if isinstance(value, (dict, list)):
            for child in value if isinstance(value, list) else [value]:
                found.extend(_kinds(child, depth + 1))
            if isinstance(value, list) and key.startswith("ROW_"):
                found.append(("several", key))
        elif key in KIND_FIELDS:
            found.append((key, value))
        elif key in NAME_FIELDS:
            found.append((key, re.sub(r"[\d/.:]+", "", str(value))))
    return tuple(found)


def _pick(count: int, kind_of: Any) -> list[int]:
    """Which of a table's rows to keep: the first PER_KIND of each kind, up to MOST_ROWS."""
    seen: dict[Any, int] = {}
    kept = []
    for index in range(count):
        kind = kind_of(index)
        if seen.get(kind, 0) < PER_KIND and len(kept) < MOST_ROWS:
            seen[kind] = seen.get(kind, 0) + 1
            kept.append(index)
    return kept


def trim(node: Any) -> Any:
    """A JSON reply with only a few rows of each kind in each table."""
    if isinstance(node, list):
        return [trim(child) for child in node]
    if not isinstance(node, dict):
        return node
    vrfs, interfaces = node.get("TABLE_vrf"), node.get("TABLE_intf")
    if isinstance(vrfs, list) and isinstance(interfaces, list) and len(vrfs) == len(interfaces):
        # show ip interface's two tables are in step, the nth VRF the nth interface's: keep them so.
        kept = _pick(len(interfaces), lambda index: _kinds(interfaces[index]) + _kinds(vrfs[index]))
        node = {**node, "TABLE_vrf": [vrfs[index] for index in kept], "TABLE_intf": [interfaces[index] for index in kept]}
    return {
        key: [trim(value[index]) for index in _pick(len(value), lambda index: _kinds(value[index]))]
        if key.startswith("ROW_") and isinstance(value, list)
        else trim(value)
        for key, value in node.items()
    }


def anonymized(node: Any, anonymize: Anonymizer, key: str = "") -> Any:
    if isinstance(node, dict):
        return {name: anonymized(value, anonymize, name) for name, value in node.items()}
    if isinstance(node, list):
        return [anonymized(value, anonymize, key) for value in node]
    if isinstance(node, str):
        return anonymize.field(key, node)
    return node


def trim_xml(element: ElementTree.Element, anonymize: Anonymizer) -> None:
    """Trim and anonymize an XML reply in place, by the same rules as a JSON one."""
    groups: dict[str, list[ElementTree.Element]] = {}
    for child in list(element):
        tag = child.tag.rsplit("}", 1)[-1]
        if tag.startswith("ROW_"):
            groups.setdefault(tag, []).append(child)
    for rows in groups.values():
        if len(rows) > 1:
            kept = set(_pick(len(rows), lambda index: _kinds(as_dict(rows[index]))))
            for index, row in enumerate(rows):
                if index not in kept:
                    element.remove(row)
    for child in element:
        trim_xml(child, anonymize)
    if element.text and element.text.strip():
        element.text = anonymize.field(element.tag.rsplit("}", 1)[-1], element.text.strip())


def recorded(command: str, output: str, anonymize: Anonymizer) -> str:
    """What to keep of one reply."""
    body = output.strip()
    if command.endswith("| json") and body.startswith("{"):
        return json.dumps(anonymized(trim(json.loads(body)), anonymize), indent=1) + "\n"
    if command.endswith("| xml") and body.startswith("<?xml"):
        root = ElementTree.fromstring(body.replace("]]>]]>", "").strip())
        trim_xml(root, anonymize)
        return ElementTree.tostring(root, encoding="unicode") + "\n]]>]]>\n"
    # Anything else: text, an error, or the tail of XML that NX-OS sent for JSON. Its start is enough.
    return anonymize.text(body[:2000]) + "\n"


class Recording:
    """Runs each command on the device and keeps the anonymous reply."""

    def __init__(self, inner: ExecOverSsh, folder: Path, anonymize: Anonymizer) -> None:
        self.inner = inner
        self.folder = folder
        self.anonymize = anonymize
        self.replies: dict[str, dict[str, Any]] = {}

    async def run(self, command: str) -> Reply:
        reply = await self.inner.run(command)
        key = self.anonymize.text(command)
        if key not in self.replies:
            name = f"{len(self.replies) + 1:02d}-{re.sub(r'[^a-z0-9]+', '-', key.lower()).strip('-')[:70]}.txt"
            (self.folder / name).write_text(recorded(command, reply.output, self.anonymize))
            self.replies[key] = {"file": name, "status": reply.status}
        return reply

    async def close(self) -> None:
        await self.inner.close()


async def record(inventory: Inventory, devices: list[str], out: Path, anonymize: Anonymizer) -> None:
    """Ask each device every query the driver has, and write the replies it took."""
    for name in devices:
        device = inventory.get(name)
        folder = out / anonymize.system(device.name)
        folder.mkdir(parents=True, exist_ok=True)
        for old in folder.glob("*.txt"):
            old.unlink()
        credentials = inventory.credentials_for(device)
        session = Recording(ExecOverSsh(device, credentials), folder, anonymize)
        async with NxosPlatform(device, credentials, session=session) as platform:
            await platform.lldp_neighbors()
            await platform.interface_counters()
            await platform.mac_table()
            await platform.arp_table()
            await platform.ospf_interfaces()
            await platform.ospf_neighbors()
            instances = {interface.network_instance for interface in await platform.interfaces() if interface.mode == "routed"}
            for instance in sorted(instances - {None}):
                await platform.routes(instance)
            await platform.routes("nosuchvrf")
            for command in COMMANDS:
                try:
                    await platform.show(command)
                except PlatformError:
                    pass  # The failures are what the tests want.
        (folder / "replies.json").write_text(json.dumps(session.replies, indent=1) + "\n")
        print(f"{anonymize.system(device.name)}: {len(session.replies)} replies")


def main() -> None:
    parser = argparse.ArgumentParser(description="Record NX-OS devices' replies as anonymous test fixtures.")
    parser.add_argument("--inventory", type=Path, required=True, help="The inventory the devices are in.")
    parser.add_argument("--rename", type=Path, required=True, help="YAML mapping each real name to a stand-in.")
    parser.add_argument("--device", action="append", help="A device to record (default: every cisco_nxos device).")
    parser.add_argument("--out", type=Path, default=Path("tests/fixtures/nxos"), help="Where to write the fixtures.")
    args = parser.parse_args()
    inventory = Inventory.load(args.inventory)
    devices = args.device or [device.name for device in inventory.devices if device.platform == "cisco_nxos"]
    anonymize = Anonymizer(yaml.safe_load(args.rename.read_text()))
    asyncio.run(record(inventory, devices, args.out, anonymize))


if __name__ == "__main__":
    main()

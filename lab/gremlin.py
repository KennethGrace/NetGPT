"""
Let a gremlin loose in the lab. It breaks one thing on purpose, keeps what it broke
to itself, and puts it back when asked. Meanwhile, ask NetGPT why the network is
misbehaving, and see whether it finds the fault.

The gremlin writes configuration over SR Linux's JSON-RPC API with the lab's login.
It is a lab tool, apart from NetGPT, which stays read-only: the model never sees the
gremlin or what it did.

Run it from the repository root with the lab deployed:

    python lab/gremlin.py break             # break something at random and print the complaint
    python lab/gremlin.py break --fault loop
    python lab/gremlin.py reveal            # say what it broke
    python lab/gremlin.py repair            # put it back
    python lab/gremlin.py check             # dry-run every fault; nothing is committed

What it broke is kept in lab/.gremlin.json until it is repaired.
"""

from __future__ import annotations

import argparse
import json
import random
import sys
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

import httpx

from netgpt.inventory import Inventory

STATE = Path("lab/.gremlin.json")
VALID = "All changes are valid."
DISCARDED = "All changes have been discarded. Leaving candidate mode."


class GremlinError(Exception):
    pass


@dataclass(frozen=True)
class Fault:
    """
    One way to break the lab. The complaint is what a user would report: symptoms
    only, and several faults share one. The answer is the cause, for checking a
    diagnosis against.
    """

    name: str
    device: str
    breaks: tuple[str, ...]
    repairs: tuple[str, ...]
    complaint: str
    answer: str


FAULTS = {
    fault.name: fault
    for fault in [
        Fault(
            name="port-down",
            device="leaf2",
            breaks=("set / interface ethernet-1/1 admin-state disable",),
            repairs=("set / interface ethernet-1/1 admin-state enable",),
            complaint="host2 and host3 can't reach host1, though they can still reach each other.",
            answer="leaf2's ethernet-1/1, its port toward switch1, is administratively down.",
        ),
        Fault(
            name="renumbered",
            device="leaf2",
            breaks=(
                "delete / interface ethernet-1/1 subinterface 0 ipv4 address 192.168.12.1/24",
                "set / interface ethernet-1/1 subinterface 0 ipv4 address 192.168.21.1/24",
            ),
            repairs=(
                "delete / interface ethernet-1/1 subinterface 0 ipv4 address 192.168.21.1/24",
                "set / interface ethernet-1/1 subinterface 0 ipv4 address 192.168.12.1/24",
            ),
            complaint="host2 and host3 can't reach host1, though they can still reach each other.",
            answer=(
                "leaf2's ethernet-1/1.0 has 192.168.21.1/24 where 192.168.12.1/24 belongs (12 and 21 swapped), "
                "so host2 and host3 have lost their gateway."
            ),
        ),
        Fault(
            name="unplugged",
            device="switch1",
            breaks=("delete / network-instance lan interface ethernet-1/2.0",),
            repairs=("set / network-instance lan interface ethernet-1/2.0",),
            complaint="host2 can't reach anything, not even host3. host3 is fine.",
            answer="switch1's ethernet-1/2.0, host2's port, is no longer in the lan mac-vrf, so nothing bridges host2's traffic.",
        ),
        Fault(
            name="blackhole",
            device="spine1",
            breaks=(
                "set / network-instance default next-hop-groups group null admin-state enable",
                "set / network-instance default next-hop-groups group null blackhole generate-icmp false",
                "set / network-instance default static-routes route 192.168.12.0/24 next-hop-group null",
            ),
            repairs=(
                "set / network-instance default static-routes route 192.168.12.0/24 next-hop-group leaf2",
                "delete / network-instance default next-hop-groups group null",
            ),
            complaint="host1 can't reach host2 or host3, and they can't reach host1.",
            answer="spine1 discards traffic for 192.168.12.0/24: its static route points at next-hop group null, a blackhole.",
        ),
        Fault(
            name="loop",
            device="spine1",
            breaks=("set / network-instance default static-routes route 192.168.12.0/24 next-hop-group leaf1",),
            repairs=("set / network-instance default static-routes route 192.168.12.0/24 next-hop-group leaf2",),
            complaint="host1 can't reach host2 or host3, and they can't reach host1.",
            answer=(
                "spine1 sends 192.168.12.0/24 to leaf1 instead of leaf2, and leaf1's route for 192.168.0.0/16 "
                "sends it straight back: a routing loop."
            ),
        ),
        Fault(
            name="no-way-back",
            device="leaf1",
            breaks=("delete / network-instance default static-routes route 192.168.0.0/16",),
            repairs=(
                "set / network-instance default static-routes route 192.168.0.0/16 admin-state enable",
                "set / network-instance default static-routes route 192.168.0.0/16 next-hop-group spine",
            ),
            complaint="host2 and host3 can't reach host1.",
            answer="leaf1 has lost its static route for 192.168.0.0/16, so traffic reaches host1 but host1's replies die at leaf1.",
        ),
    ]
}


def cli(inventory: Inventory, name: str, commands: list[str]) -> str:
    """Run commands in one CLI session on a lab device and return their output."""
    device = inventory.get(name)
    credentials = inventory.credentials_for(device)
    verify: str | bool = str(device.tls_ca) if device.tls_ca else True
    payload = {"jsonrpc": "2.0", "id": 1, "method": "cli", "params": {"commands": commands, "output-format": "text"}}
    try:
        with httpx.Client(
            base_url=f"https://{device.host}",
            auth=(credentials.username, credentials.password()),
            verify=verify,
            timeout=60.0,
        ) as client:
            response = client.post("/jsonrpc", json=payload)
            response.raise_for_status()
    except httpx.HTTPError as error:
        raise GremlinError(f"{name}: {type(error).__name__}: {error}") from error
    body = response.json()
    if "error" in body:
        raise GremlinError(f"{name}: {body['error'].get('message', body['error'])}")
    return "".join(str(item) for item in body["result"])


def commit(inventory: Inventory, name: str, commands: tuple[str, ...]) -> None:
    """Apply commands to a device's running configuration in one commit."""
    cli(inventory, name, ["enter candidate private", *commands, "commit now"])


def dry_run(inventory: Inventory, fault: Fault) -> str | None:
    """
    Check a fault against the running configuration without committing anything:
    its break must be valid and change something, and its repair must undo the
    break exactly. Returns what is wrong, or None.
    """
    broken = cli(inventory, fault.device, ["enter candidate private", *fault.breaks, "commit validate", "diff", "discard now"])
    if not broken.replace(VALID, "").replace(DISCARDED, "").strip():
        return "breaking it changes nothing; is it already broken?"
    left = cli(inventory, fault.device, ["enter candidate private", *fault.breaks, *fault.repairs, "diff", "discard now"])
    left = left.replace(DISCARDED, "").strip()
    if left:
        return f"repairing it leaves changes behind:\n{left}"
    return None


def broken() -> tuple[Fault, str] | None:
    """The fault the gremlin committed, and when, if it hasn't been repaired."""
    if not STATE.exists():
        return None
    state = json.loads(STATE.read_text())
    return FAULTS[state["fault"]], state["broken"]


def break_something(inventory: Inventory, name: str | None) -> None:
    if broken() is not None:
        raise GremlinError("The gremlin has already broken something. Put it back first: python lab/gremlin.py repair")
    fault = FAULTS[name] if name else random.choice(list(FAULTS.values()))
    commit(inventory, fault.device, fault.breaks)
    STATE.write_text(json.dumps({"fault": fault.name, "broken": datetime.now().isoformat(timespec="seconds")}) + "\n")
    print(f"The gremlin broke something. The complaint:\n\n    {fault.complaint}\n")
    print("Ask NetGPT why. `python lab/gremlin.py reveal` says what it did, and `repair` puts it back.")


def reveal() -> None:
    found = broken()
    if found is None:
        print("Nothing is broken, as far as the gremlin knows.")
        return
    fault, when = found
    print(f"{fault.name}, on {fault.device}, since {when}:\n\n    {fault.answer}\n\nThe gremlin ran:")
    for command in fault.breaks:
        print(f"    {command}")


def repair(inventory: Inventory, name: str | None) -> None:
    """Put back the fault the gremlin committed, or a named one (if its record is lost)."""
    found = broken()
    if name is None and found is None:
        print("Nothing is broken, as far as the gremlin knows.")
        return
    fault = FAULTS[name] if name else found[0]
    commit(inventory, fault.device, fault.repairs)
    if found is not None and found[0] == fault:
        STATE.unlink()
    print(f"Repaired {fault.name} on {fault.device}.")


def check(inventory: Inventory) -> bool:
    if broken() is not None:
        raise GremlinError("The checks compare against the running configuration. Repair the lab first.")
    passed = True
    for fault in FAULTS.values():
        problem = dry_run(inventory, fault)
        passed = passed and problem is None
        print(f"{fault.name} ({fault.device}): {problem or 'ok'}")
    return passed


def main() -> None:
    parser = argparse.ArgumentParser(description="Break one thing in the lab on purpose, and put it back.")
    parser.add_argument("action", choices=["break", "reveal", "repair", "check"], help="What the gremlin should do.")
    parser.add_argument("--fault", choices=list(FAULTS), help="The fault to break or repair (break: at random).")
    parser.add_argument("--inventory", type=Path, default=Path("lab/inventory.yml"), help="The lab inventory.")
    args = parser.parse_args()
    inventory = Inventory.load(args.inventory)
    try:
        if args.action == "break":
            break_something(inventory, args.fault)
        elif args.action == "reveal":
            reveal()
        elif args.action == "repair":
            repair(inventory, args.fault)
        elif not check(inventory):
            sys.exit(1)
    except GremlinError as error:
        sys.exit(str(error))


if __name__ == "__main__":
    main()

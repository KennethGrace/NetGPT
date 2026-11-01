"""
Let a gremlin loose in the lab. It breaks one thing on purpose, keeps what it broke
to itself, and puts it back when asked. Meanwhile, ask NetGPT why the network is
misbehaving, and see whether it finds the fault. (lab/game.py plays this with a
model and keeps score.)

The gremlin writes configuration over SR Linux's JSON-RPC API with the lab's login.
It is a lab tool, apart from NetGPT, which stays read-only: the model never sees the
gremlin or what it did.

It knows two labs: the fabric (lab/netgpt.clab.yml, static routes) and the WAN
(lab/wan/wan.clab.yml, OSPF across three sites), each with faults of its own. Run it
from the repository root with the lab deployed:

    python lab/gremlin.py break             # break something at random and print the complaint
    python lab/gremlin.py break --fault loop
    python lab/gremlin.py reveal            # say what it broke
    python lab/gremlin.py repair            # put it back
    python lab/gremlin.py check             # dry-run every fault; nothing is committed
    python lab/gremlin.py --lab wan break   # the same, in the WAN lab
    python lab/gremlin.py --lab wan break --pair             # two faults at once, from the lab's pairs
    python lab/gremlin.py --lab wan break --fault mtu hello  # or two named ones
    python lab/gremlin.py --lab wan break --anywhere         # a fault wherever it can go, not its usual place
    python lab/gremlin.py --lab wan break --fault passive@hq-dc:ethernet-1/1.0

Each WAN fault has a usual place, and variants: the same mistake on another
interface or router, with the complaint its symptoms would bring. A fault always
in one place can be remembered rather than found.

What it broke is kept in the lab's .gremlin.json until it is repaired.
"""

from __future__ import annotations

import argparse
import json
import random
import re
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

import httpx

from netgpt.inventory import Inventory

VALID = "All changes are valid."
DISCARDED = "All changes have been discarded. Leaving candidate mode."
OSPF = "/ network-instance default protocols ospf instance main"


class GremlinError(Exception):
    pass


@dataclass(frozen=True)
class Fault:
    """
    One way to break the lab. The complaint is what a user would report: symptoms
    only, and several faults share one. The answer is the cause. A diagnosis finds
    the fault when it names the device and, from each group of clues, at least one.
    """

    name: str
    device: str
    breaks: tuple[str, ...]
    repairs: tuple[str, ...]
    complaint: str
    answer: str
    clues: tuple[tuple[str, ...], ...]
    # Seconds the break takes to show everywhere, the slowest of its variants when it was broken live. A routing
    # protocol notices some faults only when a timer runs out, and an adjacency one end gives up at once can stay
    # full at the other until its dead interval (40 seconds) runs out.
    takes: float = 0.0
    # Where in the lab it is, for a fault with variants: a device and an interface, area, or prefix.
    where: str = ""
    # What its symptoms touch, for pairing variants: "wan" changes the paths between sites and leaves everything
    # reachable; "dc" cuts a datacenter off. Faults with neither don't pair.
    effect: str = ""

    @property
    def key(self) -> str:
        """The name of this variant: the fault's name and where it is, such as passive@hq-dc:ethernet-1/1.0."""
        return f"{self.name}@{self.where}" if self.where else self.name


@dataclass(frozen=True)
class Lab:
    """
    A lab the gremlin knows: its inventory, the hosts complaints name (they aren't in
    the inventory, so NetGPT knows them only by address), the probes that all answer
    when it is whole, its faults, and where it keeps what it broke.
    """

    name: str
    inventory: Path
    hosts: dict[str, str]
    # Pings (device, destination, source address or None) that all answer when the lab is whole. They also
    # refill ARP and MAC tables a repair emptied.
    probes: tuple[tuple[str, str, str | None], ...]
    faults: dict[str, Fault]
    state: Path
    # How many full OSPF neighbors each router has when the lab is whole. OSPF routes around a broken
    # adjacency, so pings alone can't tell it is back.
    adjacencies: dict[str, int] = field(default_factory=dict)
    # Pings per probe: a lab whose datapath drops the odd packet gets a few tries.
    tries: int = 1
    # Faults to break together, on different devices, whose complaints don't contradict each other. Broken
    # anywhere, the first is a variant that changes the paths between sites and the second one that cuts a
    # datacenter off, which leave each other's symptoms alone.
    pairs: tuple[tuple[str, str], ...] = ()
    # Every place each fault can go, by name, its usual place first.
    variants: dict[str, tuple[Fault, ...]] = field(default_factory=dict)


FABRIC_HOSTS = {"host1": "192.168.11.2", "host2": "192.168.12.2", "host3": "192.168.12.3"}

FABRIC_FAULTS = {
    fault.name: fault
    for fault in [
        Fault(
            name="port-down",
            device="leaf2",
            breaks=("set / interface ethernet-1/1 admin-state disable",),
            repairs=("set / interface ethernet-1/1 admin-state enable",),
            complaint="host2 and host3 can't reach host1, though they can still reach each other.",
            answer="leaf2's ethernet-1/1, its port toward switch1, is administratively down.",
            clues=(("ethernet-1/1", "e1-1"), ("down", "disabled", "shut")),
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
            clues=(("192.168.21",),),
        ),
        Fault(
            name="unplugged",
            device="switch1",
            breaks=("delete / network-instance lan interface ethernet-1/2.0",),
            repairs=("set / network-instance lan interface ethernet-1/2.0",),
            complaint="host2 can't reach anything, not even host3. host3 is fine.",
            answer="switch1's ethernet-1/2.0, host2's port, is no longer in the lan mac-vrf, so nothing bridges host2's traffic.",
            clues=(("ethernet-1/2", "e1-2"),),
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
            clues=(("blackhole", "black hole", "null", "discard", "drop"),),
        ),
        Fault(
            name="loop",
            device="spine1",
            breaks=("set / network-instance default static-routes route 192.168.12.0/24 next-hop-group leaf1",),
            repairs=("set / network-instance default static-routes route 192.168.12.0/24 next-hop-group leaf2",),
            complaint="host1 can't reach host2 or host3, and they can't reach host1.",
            answer=(
                "spine1's static route for 192.168.12.0/24 points at leaf1 instead of leaf2. leaf1's route for "
                "192.168.0.0/16 is as it should be, and sends the traffic back to spine1: a routing loop."
            ),
            clues=(("loop",),),
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
            clues=(("192.168.0.0/16", "no route", "missing", "route back", "return", "default route"),),
        ),
    ]
}

FABRIC = Lab(
    name="fabric",
    inventory=Path("lab/inventory.yml"),
    hosts=FABRIC_HOSTS,
    # Each host from its own leaf, and each host network from across the fabric, from the gateway address.
    probes=(
        ("leaf1", FABRIC_HOSTS["host1"], None),
        ("leaf2", FABRIC_HOSTS["host2"], None),
        ("leaf2", FABRIC_HOSTS["host3"], None),
        ("leaf1", FABRIC_HOSTS["host2"], "192.168.11.1"),
        ("leaf1", FABRIC_HOSTS["host3"], "192.168.11.1"),
        ("leaf2", FABRIC_HOSTS["host1"], "192.168.12.1"),
    ),
    faults=FABRIC_FAULTS,
    state=Path("lab/.gremlin.json"),
)

WAN_HOSTS = {
    "hq-user1": "10.10.10.11",
    "hq-user2": "10.10.10.12",
    "hq-srv1": "10.10.100.10",
    "ops-user1": "10.20.10.11",
    "ops-srv1": "10.20.100.10",
    "br-user1": "10.30.10.11",
}

BACKUP_IN_USE = "The 1G link between Ops and the branch, a backup, is carrying traffic, though the branch's 10G link to HQ is up."
LONG_WAY = "Traffic between HQ and Ops is going the long way, through the branch, though the 25G link between them is up."
HQ_SERVER_GONE = "Nobody can reach HQ's server, hq-srv1, from anywhere, not even from HQ's own users."
OPS_SERVER_GONE = "Nobody can reach Ops's server, ops-srv1, from anywhere, not even from Ops's own user."
HQ_SERVER_FROM_AFAR = "The branch and Ops can't reach HQ's server, hq-srv1, though HQ's own users can."
OPS_SERVER_FROM_AFAR = "HQ and the branch can't reach Ops's server, ops-srv1, though Ops's own user can."

# Two pairs share a complaint, so the model has to look rather than guess. Each cause says the other end of
# the link is as it should be, since a mismatch could be fixed at either end: the gremlin changed one, and
# it is the one that differs from every other interface in the lab.
WAN_FAULTS = {
    fault.name: fault
    for fault in [
        Fault(
            name="cost",
            device="hq-core",
            breaks=(f"set {OSPF} area 0.0.0.0 interface ethernet-1/20.0 metric 1000",),
            repairs=(f"delete {OSPF} area 0.0.0.0 interface ethernet-1/20.0 metric",),
            complaint=BACKUP_IN_USE,
            answer=(
                "hq-core's OSPF cost on ethernet-1/20.0, its 10G link to the branch, is 1000 where it should be 10, so "
                "HQ sends its traffic for the branch through Ops and the 1G link. The branch's costs are as they should be."
            ),
            clues=(("cost", "metric"), ("1000", "ethernet-1/20")),
            takes=5.0,
            where="hq-core:ethernet-1/20.0",
            effect="wan",
        ),
        Fault(
            name="area",
            device="branch",
            breaks=(
                f"delete {OSPF} area 0.0.0.0 interface ethernet-1/1.0",
                f"set {OSPF} area 0.0.0.3 interface ethernet-1/1.0 interface-type point-to-point",
            ),
            repairs=(
                f"delete {OSPF} area 0.0.0.3",
                f"set {OSPF} area 0.0.0.0 interface ethernet-1/1.0 interface-type point-to-point",
            ),
            complaint=BACKUP_IN_USE,
            answer=(
                "The branch's ethernet-1/1.0, its 10G link to HQ, is in OSPF area 0.0.0.3, and hq-core's end is in area "
                "0.0.0.0, so they aren't neighbors, and the branch's traffic to and from HQ goes through Ops and the 1G "
                "link. hq-core's end is as it should be."
            ),
            clues=(("area",),),
            takes=50.0,
            where="branch:ethernet-1/1.0",
            effect="wan",
        ),
        Fault(
            name="mtu",
            device="ops-core",
            breaks=("set / interface ethernet-1/1 subinterface 0 ip-mtu 1400",),
            repairs=("delete / interface ethernet-1/1 subinterface 0 ip-mtu",),
            complaint=LONG_WAY,
            answer=(
                "ops-core's ethernet-1/1.0, its 25G link to HQ, has an IP MTU of 1400, and hq-core's end 1500, so their "
                "OSPF adjacency sticks in exstart, and traffic between HQ and Ops goes around through the branch. "
                "hq-core's end is as it should be."
            ),
            clues=(("mtu",),),
            takes=20.0,
            where="ops-core:ethernet-1/1.0",
            effect="wan",
        ),
        Fault(
            name="passive",
            device="hq-core",
            breaks=(f"set {OSPF} area 0.0.0.1 interface ethernet-1/3.0 passive true",),
            repairs=(f"delete {OSPF} area 0.0.0.1 interface ethernet-1/3.0 passive",),
            complaint=HQ_SERVER_GONE,
            answer=(
                "hq-core's ethernet-1/3.0, its link to hq-dc, is passive in OSPF, so it sends no hellos and the two "
                "aren't neighbors: HQ's datacenter (area 0.0.0.1) is cut off, and nothing else has a route to "
                "10.10.100.0/24. hq-dc is as it should be."
            ),
            clues=(("passive",),),
            takes=50.0,
            where="hq-core:ethernet-1/3.0",
            effect="dc",
        ),
        Fault(
            name="hello",
            device="hq-dc",
            breaks=(f"set {OSPF} area 0.0.0.1 interface ethernet-1/1.0 hello-interval 5",),
            repairs=(f"delete {OSPF} area 0.0.0.1 interface ethernet-1/1.0 hello-interval",),
            complaint=HQ_SERVER_GONE,
            answer=(
                "hq-dc's ethernet-1/1.0, its link to hq-core, sends OSPF hellos every 5 seconds, and hq-core's end every "
                "10, so they aren't neighbors: HQ's datacenter (area 0.0.0.1) is cut off, and nothing else has a route "
                "to 10.10.100.0/24. hq-core is as it should be."
            ),
            clues=(("hello",),),
            # Each end drops the other when its dead interval (40 seconds) runs out with no matching hello. Live,
            # hq-dc had dropped hq-core by 45 seconds and hq-core hq-dc by 74.
            takes=75.0,
            where="hq-dc:ethernet-1/1.0",
            effect="dc",
        ),
        Fault(
            name="static",
            device="ops-core",
            breaks=(
                "set / network-instance default next-hop-groups group null admin-state enable",
                "set / network-instance default next-hop-groups group null blackhole generate-icmp false",
                "set / network-instance default static-routes route 10.30.10.0/24 admin-state enable",
                "set / network-instance default static-routes route 10.30.10.0/24 next-hop-group null",
            ),
            repairs=(
                "delete / network-instance default static-routes",
                "delete / network-instance default next-hop-groups",
            ),
            complaint="The branch's user can't reach anything at Ops, though everything at HQ works.",
            answer=(
                "ops-core has a static route for 10.30.10.0/24, the branch's LAN, that points at next-hop group null, a "
                "blackhole. A static route beats OSPF's, so ops-core discards everything for the branch's user, "
                "including Ops's replies."
            ),
            clues=(("static",), ("blackhole", "black hole", "null", "discard", "drop")),
            takes=10.0,
            where="ops-core:10.30.10.0/24",
        ),
        # Harder: every adjacency stays full, and only the routes say anything is wrong.
        Fault(
            name="summary",
            device="hq-core",
            breaks=(f"set {OSPF} area 0.0.0.1 area-range 10.10.0.0/16 advertise false",),
            repairs=(f"delete {OSPF} area 0.0.0.1 area-range 10.10.0.0/16",),
            complaint=HQ_SERVER_FROM_AFAR,
            answer=(
                "hq-core, the border router for HQ's datacenter area (0.0.0.1), has an area range 10.10.0.0/16 set not to "
                "advertise, so it keeps the datacenter's networks out of area 0, and the rest of the WAN has no route to "
                "10.10.100.0/24. Every adjacency is full, and hq-dc is as it should be."
            ),
            clues=(("range", "summar", "advertise"),),
            takes=15.0,
            where="hq-core:10.10.0.0/16",
        ),
        # Harder: an adjacency is missing, and the typed tools show no setting that differs; the cause is in the
        # configuration.
        Fault(
            name="stub",
            device="ops-dc",
            breaks=(f"set {OSPF} area 0.0.0.2 stub summaries true",),
            repairs=(f"delete {OSPF} area 0.0.0.2 stub",),
            complaint=OPS_SERVER_GONE,
            answer=(
                "ops-dc treats area 0.0.0.2 as a stub area and ops-core doesn't, so their hellos disagree (the E bit) and "
                "they aren't neighbors: Ops's datacenter is cut off, and nothing else has a route to 10.20.100.0/24. "
                "ops-core's area settings are as they should be."
            ),
            clues=(("stub",),),
            takes=75.0,
            where="ops-dc:0.0.0.2",
            effect="dc",
        ),
        Fault(
            name="auth",
            device="ops-core",
            breaks=(
                "set / system authentication keychain ospf-dc admin-state enable",
                "set / system authentication keychain ospf-dc type ospf",
                "set / system authentication keychain ospf-dc key 1 algorithm md5",
                'set / system authentication keychain ospf-dc key 1 authentication-key "lab-key-1"',
                f"set {OSPF} area 0.0.0.2 interface ethernet-1/3.0 authentication keychain ospf-dc",
            ),
            repairs=(
                f"delete {OSPF} area 0.0.0.2 interface ethernet-1/3.0 authentication",
                "delete / system authentication keychain ospf-dc",
            ),
            complaint=OPS_SERVER_GONE,
            answer=(
                "ops-core's ethernet-1/3.0, its link to ops-dc, uses OSPF authentication (MD5, keychain ospf-dc) and "
                "ops-dc's end uses none, so they aren't neighbors: Ops's datacenter is cut off, and nothing else has a "
                "route to 10.20.100.0/24. ops-dc is as it should be."
            ),
            clues=(("authenticat", "md5", "keychain"),),
            takes=75.0,
            where="ops-core:ethernet-1/3.0",
            effect="dc",
        ),
    ]
}

# Variants: each WAN fault anywhere it fits. A fault that kills an adjacency can go on either end of any of the
# five, and its complaint is whatever losing that adjacency looks like to users.


@dataclass(frozen=True)
class LinkEnd:
    """One end of an OSPF adjacency: the router, the subinterface OSPF runs on, and what the link leads to."""

    device: str
    interface: str
    to: str


@dataclass(frozen=True)
class Adjacency:
    """One of the WAN's OSPF adjacencies, and what users notice when it is gone."""

    ends: tuple[LinkEnd, LinkEnd]
    area: str
    # An area no router has, for the area fault: the backbone's links go to 0.0.0.3, and a datacenter's to the other
    # datacenter's area, as if its configuration had been copied.
    wrong_area: str
    speed: str
    complaint: str
    # What follows from losing it, after "so they aren't neighbors: ".
    consequence: str
    effect: str


FAILOVER = (
    "In last night's failover test, with its 10G link to HQ shut, the branch lost touch with everything, though its 1G "
    "backup link to Ops was up. The 10G link is back, and everything works again."
)

WAN_ADJACENCIES = (
    Adjacency(
        ends=(LinkEnd("hq-core", "ethernet-1/1.0", "Ops"), LinkEnd("ops-core", "ethernet-1/1.0", "HQ")),
        area="0.0.0.0", wrong_area="0.0.0.3", speed="25G", complaint=LONG_WAY,
        consequence="traffic between HQ and Ops goes around through the branch", effect="wan",
    ),
    Adjacency(
        ends=(LinkEnd("hq-core", "ethernet-1/20.0", "the branch"), LinkEnd("branch", "ethernet-1/1.0", "HQ")),
        area="0.0.0.0", wrong_area="0.0.0.3", speed="10G", complaint=BACKUP_IN_USE,
        consequence="the branch's traffic to and from HQ goes through Ops and the 1G link", effect="wan",
    ),
    Adjacency(
        ends=(LinkEnd("ops-core", "ethernet-1/20.0", "the branch"), LinkEnd("branch", "ethernet-1/2.0", "Ops")),
        area="0.0.0.0", wrong_area="0.0.0.3", speed="1G", complaint=FAILOVER,
        consequence="the 1G backup link carries nothing, so with its 10G link to HQ down, the branch is cut off",
        effect="wan",
    ),
    Adjacency(
        ends=(LinkEnd("hq-core", "ethernet-1/3.0", "hq-dc"), LinkEnd("hq-dc", "ethernet-1/1.0", "hq-core")),
        area="0.0.0.1", wrong_area="0.0.0.2", speed="", complaint=HQ_SERVER_GONE,
        consequence="HQ's datacenter (area 0.0.0.1) is cut off, and nothing else has a route to 10.10.100.0/24",
        effect="dc",
    ),
    Adjacency(
        ends=(LinkEnd("ops-core", "ethernet-1/3.0", "ops-dc"), LinkEnd("ops-dc", "ethernet-1/1.0", "ops-core")),
        area="0.0.0.2", wrong_area="0.0.0.1", speed="", complaint=OPS_SERVER_GONE,
        consequence="Ops's datacenter (area 0.0.0.2) is cut off, and nothing else has a route to 10.20.100.0/24",
        effect="dc",
    ),
)

KEYCHAIN = (
    "set / system authentication keychain {chain} admin-state enable",
    "set / system authentication keychain {chain} type ospf",
    "set / system authentication keychain {chain} key 1 algorithm md5",
    'set / system authentication keychain {chain} key 1 authentication-key "lab-key-1"',
)


def adjacency_faults(adjacency: Adjacency, end: LinkEnd, peer: LinkEnd) -> list[Fault]:
    """The faults that keep two routers from being neighbors, made at one end of their adjacency."""
    area, interface = adjacency.area, end.interface
    port, subinterface = interface.rsplit(".", 1)
    link = f"its {adjacency.speed} link to {end.to}" if adjacency.speed else f"its link to {end.to}"
    at = f"{end.device}'s {interface}, {link},"
    lost = adjacency.consequence
    chain = "ospf-wan" if adjacency.effect == "wan" else "ospf-dc"
    common = dict(device=end.device, complaint=adjacency.complaint, where=f"{end.device}:{interface}", effect=adjacency.effect)
    return [
        Fault(
            name="passive",
            breaks=(f"set {OSPF} area {area} interface {interface} passive true",),
            repairs=(f"delete {OSPF} area {area} interface {interface} passive",),
            answer=(
                f"{at} is passive in OSPF, so it sends no hellos and the two aren't neighbors: {lost}. "
                f"{peer.device} is as it should be."
            ),
            clues=(("passive",),),
            takes=50.0,
            **common,
        ),
        Fault(
            name="hello",
            breaks=(f"set {OSPF} area {area} interface {interface} hello-interval 5",),
            repairs=(f"delete {OSPF} area {area} interface {interface} hello-interval",),
            answer=(
                f"{at} sends OSPF hellos every 5 seconds, and {peer.device}'s end every 10, so they aren't neighbors: "
                f"{lost}. {peer.device} is as it should be."
            ),
            clues=(("hello",),),
            takes=75.0,
            **common,
        ),
        Fault(
            name="area",
            breaks=(
                f"delete {OSPF} area {area} interface {interface}",
                f"set {OSPF} area {adjacency.wrong_area} interface {interface} interface-type point-to-point",
            ),
            repairs=(
                f"delete {OSPF} area {adjacency.wrong_area}",
                f"set {OSPF} area {area} interface {interface} interface-type point-to-point",
            ),
            answer=(
                f"{at} is in OSPF area {adjacency.wrong_area}, and {peer.device}'s end is in area {area}, so they aren't "
                f"neighbors: {lost}. {peer.device}'s end is as it should be."
            ),
            clues=(("area",),),
            takes=50.0,
            **common,
        ),
        Fault(
            name="mtu",
            breaks=(f"set / interface {port} subinterface {subinterface} ip-mtu 1400",),
            repairs=(f"delete / interface {port} subinterface {subinterface} ip-mtu",),
            answer=(
                f"{at} has an IP MTU of 1400, and {peer.device}'s end 1500, so their OSPF adjacency sticks in exstart: "
                f"{lost}. {peer.device}'s end is as it should be."
            ),
            clues=(("mtu",),),
            takes=20.0,
            **common,
        ),
        Fault(
            name="auth",
            breaks=(
                *(command.format(chain=chain) for command in KEYCHAIN),
                f"set {OSPF} area {area} interface {interface} authentication keychain {chain}",
            ),
            repairs=(
                f"delete {OSPF} area {area} interface {interface} authentication",
                f"delete / system authentication keychain {chain}",
            ),
            answer=(
                f"{at} uses OSPF authentication (MD5, keychain {chain}) and {peer.device}'s end uses none, so they aren't "
                f"neighbors: {lost}. {peer.device} is as it should be."
            ),
            clues=(("authenticat", "md5", "keychain"),),
            takes=75.0,
            **common,
        ),
    ]


def stub_fault(adjacency: Adjacency, end: LinkEnd, peer: LinkEnd) -> Fault:
    """A datacenter's area made a stub at one router only."""
    area = adjacency.area
    return Fault(
        name="stub",
        device=end.device,
        breaks=(f"set {OSPF} area {area} stub summaries true",),
        repairs=(f"delete {OSPF} area {area} stub",),
        complaint=adjacency.complaint,
        answer=(
            f"{end.device} treats area {area} as a stub area and {peer.device} doesn't, so their hellos disagree (the E "
            f"bit) and they aren't neighbors: {adjacency.consequence}. {peer.device}'s area settings are as they should be."
        ),
        clues=(("stub",),),
        takes=75.0,
        where=f"{end.device}:{area}",
        effect="dc",
    )


def cost_fault(device: str, interface: str, metric: int, usual: int, link: str, complaint: str, result: str, peer: str) -> Fault:
    """An OSPF cost that moves traffic: raised on a link it should take, or lowered on the backup."""
    port = interface.rsplit(".", 1)[0]
    return Fault(
        name="cost",
        device=device,
        breaks=(f"set {OSPF} area 0.0.0.0 interface {interface} metric {metric}",),
        repairs=(f"delete {OSPF} area 0.0.0.0 interface {interface} metric",),
        complaint=complaint,
        answer=(
            f"{device}'s OSPF cost on {interface}, {link}, is {metric} where it should be {usual}, so {result}. "
            f"{peer}'s costs are as they should be."
        ),
        clues=(("cost", "metric"), (port,)),
        takes=5.0,
        where=f"{device}:{interface}",
        effect="wan",
    )


def static_fault(device: str, prefix: str, network: str, complaint: str, discards: str) -> Fault:
    """A static route to a blackhole, for a network OSPF should reach."""
    return Fault(
        name="static",
        device=device,
        breaks=(
            "set / network-instance default next-hop-groups group null admin-state enable",
            "set / network-instance default next-hop-groups group null blackhole generate-icmp false",
            f"set / network-instance default static-routes route {prefix} admin-state enable",
            f"set / network-instance default static-routes route {prefix} next-hop-group null",
        ),
        repairs=("delete / network-instance default static-routes", "delete / network-instance default next-hop-groups"),
        complaint=complaint,
        answer=(
            f"{device} has a static route for {prefix}, {network}, that points at next-hop group null, a blackhole. A "
            f"static route beats OSPF's, so {device} discards {discards}."
        ),
        clues=(("static",), ("blackhole", "black hole", "null", "discard", "drop")),
        takes=10.0,
        where=f"{device}:{prefix}",
    )


def summary_fault(device: str, area: str, prefix: str, complaint: str, kept: str, server: str, peer: str) -> Fault:
    """A datacenter's networks kept out of the backbone by its border router's area range."""
    return Fault(
        name="summary",
        device=device,
        breaks=(f"set {OSPF} area {area} area-range {prefix} advertise false",),
        repairs=(f"delete {OSPF} area {area} area-range {prefix}",),
        complaint=complaint,
        answer=(
            f"{device}, the border router for {kept} area ({area}), has an area range {prefix} set not to advertise, so "
            f"it keeps {server} out of area 0, and the rest of the WAN has no route to it. Every adjacency is full, and "
            f"{peer} is as it should be."
        ),
        clues=(("range", "summar", "advertise"),),
        takes=15.0,
        where=f"{device}:{prefix}",
    )


SERVERS_APART = "The two servers, hq-srv1 and ops-srv1, can't reach each other, though everyone else reaches both."


def wan_variants() -> dict[str, tuple[Fault, ...]]:
    """Every place each WAN fault can go, its usual place (WAN_FAULTS) first."""
    found: dict[str, list[Fault]] = {}
    for adjacency in WAN_ADJACENCIES:
        for end, peer in (adjacency.ends, adjacency.ends[::-1]):
            for fault in adjacency_faults(adjacency, end, peer):
                found.setdefault(fault.name, []).append(fault)
            if adjacency.effect == "dc":
                found.setdefault("stub", []).append(stub_fault(adjacency, end, peer))
    found["cost"] = [
        cost_fault("branch", "ethernet-1/1.0", 1000, 10, "its 10G link to HQ", BACKUP_IN_USE,
                   "the branch sends its traffic for HQ, and for Ops, over the 1G link", "hq-core"),
        cost_fault("hq-core", "ethernet-1/1.0", 1000, 4, "its 25G link to Ops",
                   "Traffic from HQ to Ops is going the long way, through the branch, though the 25G link between them is up.",
                   "HQ sends its traffic for Ops through the branch", "ops-core"),
        cost_fault("ops-core", "ethernet-1/1.0", 1000, 4, "its 25G link to HQ",
                   "Traffic from Ops to HQ is going the long way, through the branch, though the 25G link between them is up.",
                   "Ops sends its traffic for HQ through the branch", "hq-core"),
        cost_fault("ops-core", "ethernet-1/20.0", 1, 100, "its 1G link to the branch", BACKUP_IN_USE,
                   "Ops sends its traffic for the branch over the 1G link instead of through HQ", "branch"),
        cost_fault("branch", "ethernet-1/2.0", 1, 100, "its 1G link to Ops", BACKUP_IN_USE,
                   "the branch sends its traffic for Ops, and for HQ, over the 1G link", "ops-core"),
    ]
    found["static"] = [
        static_fault("hq-core", "10.30.10.0/24", "the branch's LAN", "The branch's user can't reach anything at HQ or Ops.",
                     "everything for the branch's user, including the replies from HQ, and from Ops, which go through HQ"),
        static_fault("hq-core", "10.20.100.0/24", "Ops's datacenter LAN", OPS_SERVER_FROM_AFAR,
                     "everything for Ops's server from HQ, and from the branch, whose traffic for Ops goes through HQ"),
        static_fault("ops-core", "10.10.100.0/24", "HQ's datacenter LAN",
                     "Nobody at Ops can reach HQ's server, hq-srv1, though HQ's users and the branch can.",
                     "everything Ops sends HQ's server"),
        static_fault("branch", "10.10.100.0/24", "HQ's datacenter LAN",
                     "The branch's user can't reach HQ's server, hq-srv1, though everything else answers.",
                     "everything the branch's user sends HQ's server"),
        static_fault("branch", "10.20.100.0/24", "Ops's datacenter LAN",
                     "The branch's user can't reach Ops's server, ops-srv1, though everything else answers.",
                     "everything the branch's user sends Ops's server"),
        static_fault("hq-dc", "10.20.100.0/24", "Ops's datacenter LAN", SERVERS_APART,
                     "everything HQ's server sends Ops's server, replies included"),
        static_fault("ops-dc", "10.10.100.0/24", "HQ's datacenter LAN", SERVERS_APART,
                     "everything Ops's server sends HQ's server, replies included"),
    ]
    found["summary"] = [
        summary_fault("hq-core", "0.0.0.1", "10.10.100.0/24", HQ_SERVER_FROM_AFAR, "HQ's datacenter",
                      "HQ's server network, 10.10.100.0/24,", "hq-dc"),
        summary_fault("ops-core", "0.0.0.2", "10.20.0.0/16", OPS_SERVER_FROM_AFAR, "Ops's datacenter",
                      "the datacenter's networks, 10.20.100.0/24 among them,", "ops-dc"),
        summary_fault("ops-core", "0.0.0.2", "10.20.100.0/24", OPS_SERVER_FROM_AFAR, "Ops's datacenter",
                      "Ops's server network, 10.20.100.0/24,", "ops-dc"),
    ]
    variants = {}
    for name, usual in WAN_FAULTS.items():
        others = [fault for fault in found[name] if fault.key != usual.key]
        variants[name] = (usual, *others)
    return variants



WAN = Lab(
    name="wan",
    inventory=Path("lab/wan/inventory.yml"),
    hosts=WAN_HOSTS,
    # Each host from its own router, and the servers and the branch from across the WAN, from the gateway address.
    probes=(
        ("hq-core", WAN_HOSTS["hq-user1"], None),
        ("hq-core", WAN_HOSTS["hq-user2"], None),
        ("hq-dc", WAN_HOSTS["hq-srv1"], None),
        ("ops-core", WAN_HOSTS["ops-user1"], None),
        ("ops-dc", WAN_HOSTS["ops-srv1"], None),
        ("branch", WAN_HOSTS["br-user1"], None),
        ("branch", WAN_HOSTS["hq-srv1"], "10.30.10.1"),
        ("branch", WAN_HOSTS["ops-srv1"], "10.30.10.1"),
        ("ops-core", WAN_HOSTS["hq-srv1"], "10.20.10.1"),
        ("hq-dc", WAN_HOSTS["ops-srv1"], "10.10.100.1"),
        ("ops-dc", WAN_HOSTS["br-user1"], "10.20.100.1"),
    ),
    faults=WAN_FAULTS,
    state=Path("lab/wan/.gremlin.json"),
    adjacencies={"hq-core": 3, "hq-dc": 1, "branch": 2, "ops-core": 3, "ops-dc": 1},
    tries=3,
    # Each pair touches separate parts of the WAN, so both sets of symptoms show and neither complaint denies
    # the other: in its usual places, and anywhere (see Lab.pairs).
    pairs=(("mtu", "hello"), ("area", "passive"), ("cost", "auth"), ("hello", "stub")),
    variants=wan_variants(),
)

LABS = {lab.name: lab for lab in (FABRIC, WAN)}
# Every fault, by name, whichever lab it is in. The names don't repeat across labs.
FAULTS = {name: fault for lab in LABS.values() for name, fault in lab.faults.items()}
LAB_OF = {name: lab for lab in LABS.values() for name in lab.faults}
# Every variant, by key (passive@hq-dc:ethernet-1/1.0), the usual places included.
VARIANTS = {fault.key: fault for lab in LABS.values() for faults in lab.variants.values() for fault in faults}


def fault_named(name: str) -> Fault:
    """A fault by name, in its usual place, or a variant by key."""
    if name in VARIANTS:
        return VARIANTS[name]
    if name in FAULTS:
        return FAULTS[name]
    raise GremlinError(f"No fault is called {name}. The faults are {', '.join(FAULTS)}, and a variant is fault@place.")


def label(fault: Fault) -> str:
    return fault.key if fault.where else f"{fault.name} on {fault.device}"


def anywhere_pair(lab: Lab, names: list[str]) -> list[Fault]:
    """
    Two faults in random places that leave each other's symptoms alone: the first
    where it changes the paths between sites, and the second where it cuts a
    datacenter off, on another device.
    """
    if len(names) != 2:
        raise GremlinError(f"Anywhere, the gremlin breaks one fault or a pair, not {len(names)}.")
    first, second = (lab.variants.get(name, ()) for name in names)
    choices = [
        [one, other]
        for one in first if one.effect == "wan"
        for other in second if other.effect == "dc" and other.device != one.device
    ]
    if not choices:
        raise GremlinError(
            f"{names[0]} can't go anywhere it changes the paths between sites with {names[1]} somewhere it cuts a "
            f"datacenter off, on another device."
        )
    return random.choice(choices)


def place(lab: Lab, names: list[str], anywhere: bool) -> list[Fault]:
    """
    The faults to break: each named one in its usual place, or anywhere, one of its
    variants at random (a pair's, chosen together). A variant's key is that variant.
    """
    if not anywhere or any("@" in name for name in names):
        return [fault_named(name) for name in names]
    if len(names) == 1:
        return [random.choice(lab.variants.get(names[0]) or (lab.faults[names[0]],))]
    return anywhere_pair(lab, names)


def cli(inventory: Inventory, name: str, commands: list[str]) -> str:
    """Run commands in one CLI session on a lab device and return their output."""
    device = inventory.get(name)
    credentials = inventory.credentials_for(device)
    verify: str | bool = str(device.tls_ca) if device.tls_ca else True
    payload = {"jsonrpc": "2.0", "id": 1, "method": "cli", "params": {"commands": commands, "output-format": "text"}}
    for attempt in range(2):
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
        if "error" not in body:
            return "".join(str(item) for item in body["result"])
        message = str(body["error"].get("message", body["error"]))
        # Now and then SR Linux refuses a request with "Invalid session id" before running any of it, so a
        # second try is safe. One ended a run of the gremlin's checks after 59 faults.
        if "Invalid session id" not in message or attempt:
            raise GremlinError(f"{name}: {message}")
    raise AssertionError("unreachable")


def commit(inventory: Inventory, name: str, commands: tuple[str, ...]) -> None:
    """Apply commands to a device's running configuration in one commit."""
    cli(inventory, name, ["enter candidate private", *commands, "commit now"])


def answers(inventory: Inventory, device: str, destination: str, source: str | None, tries: int = 1) -> bool:
    """Whether a ping from a device, optionally from one of its addresses, gets a reply to any of its tries."""
    command = f"ping {destination} network-instance default -c {tries} -i 0.2 -W 1" + (f" -I {source}" if source else "")
    try:
        output = cli(inventory, device, [command])
    except GremlinError as error:
        # SR Linux reports an unanswered ping as an error, with ping's output inside it.
        output = str(error)
    received = re.search(r"(\d+) received", output)
    return received is not None and received.group(1) != "0"


def full_neighbors(inventory: Inventory, device: str) -> int:
    """How many OSPF neighbors a router has in the full state."""
    output = cli(inventory, device, ["show network-instance default protocols ospf neighbor"])
    return len(re.findall(r"\|\s+\S+\s+\S+\s+full\s", output))


def settle(inventory: Inventory, lab: Lab, timeout: float = 90.0) -> list[str]:
    """
    Wait for the lab to be whole: every probe answering, and every OSPF adjacency
    full. A repaired interface takes a moment to come back, and an adjacency longer.
    Returns what is still wrong when time runs out.
    """
    deadline = time.monotonic() + timeout
    while True:
        wrong = [
            f"{device} to {destination}" + (f" from {source}" if source else "")
            for device, destination, source in lab.probes
            if not answers(inventory, device, destination, source, lab.tries)
        ]
        for device, expected in lab.adjacencies.items():
            found = full_neighbors(inventory, device)
            if found != expected:
                wrong.append(f"{device} has {found} full OSPF neighbors, not {expected}")
        if not wrong or time.monotonic() > deadline:
            return wrong
        time.sleep(2)


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


def complaint(faults: list[Fault]) -> str:
    """What users report: one fault's complaint, or each of several, numbered."""
    if len(faults) == 1:
        return faults[0].complaint
    return " ".join(f"({number}) {fault.complaint}" for number, fault in enumerate(faults, 1))


def broken(lab: Lab) -> tuple[list[Fault], str] | None:
    """The faults the gremlin committed in a lab, and when, if they haven't been repaired."""
    if not lab.state.exists():
        return None
    state = json.loads(lab.state.read_text())
    return [fault_named(name) for name in state.get("faults") or [state["fault"]]], state["broken"]


def inflict(inventory: Inventory, lab: Lab, faults: list[Fault]) -> None:
    """
    Commit a fault, or a pair, and remember them. Only on a whole lab, so the
    symptoms are all theirs.
    """
    if broken(lab) is not None:
        raise GremlinError(
            f"The gremlin has already broken something. Put it back first: python lab/gremlin.py --lab {lab.name} repair"
        )
    wrong = settle(inventory, lab)
    if wrong:
        raise GremlinError(f"The lab isn't whole, so a fault's symptoms would be muddled: {', '.join(wrong)}.")
    for fault in faults:
        commit(inventory, fault.device, fault.breaks)
    keys = [fault.key for fault in faults]
    lab.state.write_text(json.dumps({"faults": keys, "broken": datetime.now().isoformat(timespec="seconds")}) + "\n")
    time.sleep(max(fault.takes for fault in faults))  # After the record, so a break interrupted here can still be repaired.


def mend(inventory: Inventory, lab: Lab, faults: list[Fault]) -> None:
    """Commit faults' repairs, and forget them if they are what the gremlin committed."""
    found = broken(lab)
    for fault in reversed(faults):
        commit(inventory, fault.device, fault.repairs)
    if found is not None and found[0] == faults:
        lab.state.unlink()


def break_something(inventory: Inventory, lab: Lab, names: list[str] | None, pair: bool, anywhere: bool) -> None:
    if not names:
        names = list(random.choice(lab.pairs)) if pair else [random.choice(list(lab.faults))]
    faults = place(lab, names, anywhere)
    inflict(inventory, lab, faults)
    print(f"The gremlin broke something. The complaint:\n\n    {complaint(faults)}\n")
    print(f"Ask NetGPT why. `python lab/gremlin.py --lab {lab.name} reveal` says what it did, and `repair` puts it back.")


def reveal(lab: Lab) -> None:
    found = broken(lab)
    if found is None:
        print("Nothing is broken, as far as the gremlin knows.")
        return
    faults, when = found
    for fault in faults:
        print(f"{label(fault)}, since {when}:\n\n    {fault.answer}\n\nThe gremlin ran:")
        for command in fault.breaks:
            print(f"    {command}")
        print()


def repair(inventory: Inventory, lab: Lab, names: list[str] | None) -> None:
    """Put back the faults the gremlin committed, or named ones (if its record is lost)."""
    found = broken(lab)
    if not names and found is None:
        print("Nothing is broken, as far as the gremlin knows.")
        return
    faults = [fault_named(name) for name in names] if names else found[0]
    mend(inventory, lab, faults)
    wrong = settle(inventory, lab)
    repaired = ", ".join(label(fault) for fault in faults)
    print(f"Repaired {repaired}.", f"Still wrong: {', '.join(wrong)}." if wrong else "The lab is whole.")


def check(inventory: Inventory, lab: Lab) -> bool:
    if broken(lab) is not None:
        raise GremlinError("The checks compare against the running configuration. Repair the lab first.")
    passed = True
    faults = [fault for variants in lab.variants.values() for fault in variants] or list(lab.faults.values())
    for fault in faults:
        problem = dry_run(inventory, fault)
        passed = passed and problem is None
        print(f"{label(fault)}: {problem or 'ok'}")
    for pair in lab.pairs:
        devices = {lab.faults[name].device for name in pair}
        if len(devices) < len(pair):
            passed = False
            print(f"{' + '.join(pair)}: both on {devices.pop()}; a pair's faults belong on different devices")
        if lab.variants:
            try:
                anywhere_pair(lab, list(pair))
            except GremlinError as error:
                passed = False
                print(f"{' + '.join(pair)}: {error}")
    return passed


def main() -> None:
    parser = argparse.ArgumentParser(description="Break one thing in the lab on purpose, and put it back.")
    parser.add_argument("action", choices=["break", "reveal", "repair", "check"], help="What the gremlin should do.")
    parser.add_argument("--lab", choices=list(LABS), default="fabric", help="The lab (default: %(default)s).")
    parser.add_argument(
        "--fault", nargs="+", help="The faults to break or repair, by name or variant (fault@place). Break: one at random."
    )
    parser.add_argument("--pair", action="store_true", help="Break two faults at once, a pair of the lab's at random.")
    parser.add_argument(
        "--anywhere", action="store_true", help="Break each fault in one of its places at random, not its usual one."
    )
    parser.add_argument("--inventory", type=Path, help="The lab's inventory (default: the lab's own).")
    args = parser.parse_args()
    lab = LABS[args.lab]
    for name in args.fault or []:
        fault = name.split("@", 1)[0]
        if fault not in FAULTS:
            sys.exit(f"No fault is called {fault}. The {lab.name} lab's are {', '.join(lab.faults)}.")
        if fault not in lab.faults:
            sys.exit(f"{fault} is a fault in the {LAB_OF[fault].name} lab. Pass --lab {LAB_OF[fault].name}.")
        if "@" in name and name not in VARIANTS:
            places = ", ".join(variant.key for variant in lab.variants.get(fault, ()))
            sys.exit(f"{fault} doesn't go at {name.split('@', 1)[1]}. It goes at {places or 'its usual place only'}.")
    if args.anywhere and not lab.variants:
        sys.exit(f"The {lab.name} lab's faults each have one place.")
    inventory = Inventory.load(args.inventory or lab.inventory)
    try:
        if args.action == "break":
            break_something(inventory, lab, args.fault, args.pair, args.anywhere)
        elif args.action == "reveal":
            reveal(lab)
        elif args.action == "repair":
            repair(inventory, lab, args.fault)
        elif not check(inventory, lab):
            sys.exit(1)
    except GremlinError as error:
        sys.exit(str(error))


if __name__ == "__main__":
    main()

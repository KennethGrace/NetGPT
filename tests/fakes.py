"""
Fakes shared by the tests: a platform that answers from canned data, a transport
that replays recorded SR Linux responses, and an Ollama server that streams
scripted replies.
"""

from __future__ import annotations

import asyncio
import json
import re
from pathlib import Path
from typing import Any, NamedTuple

import httpx

from netgpt.guard import NOKIA_SRLINUX
from netgpt.platforms import Platform, PlatformError
from netgpt.inventory import Credentials, Device
from netgpt.platforms.srlinux import SRLinuxPlatform
from netgpt.primitives import (
    ArpEntry,
    DeviceError,
    DeviceInfo,
    Interface,
    InterfaceCounters,
    LldpNeighbor,
    MacEntry,
    NextHop,
    OspfInterface,
    OspfNeighbor,
    PingResult,
    Route,
)
from netgpt.topology import Topology, build_ospf, build_topology

FIXTURES = Path(__file__).parent / "fixtures" / "srlinux"


def replay(fixtures: dict[str, str]) -> tuple[httpx.MockTransport, list[str]]:
    """
    Answer each SR Linux JSON-RPC request with the fixture whose key prefixes its
    CLI command or YANG path. Returns the transport and the list of requests made.
    """
    requests: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        command = json.loads(request.content)["params"]["commands"][0]
        key = command["path"] if isinstance(command, dict) else command
        requests.append(key)
        name = next(fixture for prefix, fixture in fixtures.items() if key.startswith(prefix))
        return httpx.Response(200, json=json.loads((FIXTURES / name).read_text()))

    return httpx.MockTransport(handler), requests


# The containerlab lab, and the responses recorded from each of its devices (by the
# path each query asks for).
LAB = [
    DeviceInfo(name="spine1", platform="nokia_srlinux", host="172.20.20.11", role="spine"),
    DeviceInfo(name="leaf1", platform="nokia_srlinux", host="172.20.20.21", role="leaf"),
    DeviceInfo(name="leaf2", platform="nokia_srlinux", host="172.20.20.22", role="leaf"),
    DeviceInfo(name="switch1", platform="nokia_srlinux", host="172.20.20.41", role="access"),
]
TABLES = {
    "/system/lldp": "lldp.json",
    "/interface[name=*]/subinterface[index=*]/ipv4/arp": "arp.json",
    "/interface[name=*]/subinterface[index=*]/type": "interfaces.json",
    "/network-instance[name=*]/bridge-table": "mac_table.json",
    "/network-instance[name=default]/route-table": "route_table.json",
}
RECORDED = {device.name: {path: f"lab/{device.name}/{name}" for path, name in TABLES.items()} for device in LAB}


class RecordedLab(NamedTuple):
    """Every lab device's tables, as the SR Linux driver parses them."""

    neighbors: list[LldpNeighbor]
    arp: list[ArpEntry]
    macs: list[MacEntry]
    interfaces: list[Interface]
    routes: dict[str, list[Route]]

    def topology(self, is_management: Any, devices: list[DeviceInfo] = LAB) -> Topology:
        return build_topology(devices, self.neighbors, self.arp, [], is_management, self.macs, self.interfaces)


async def recorded_lab() -> RecordedLab:
    """
    Every lab device's LLDP, ARP, MAC, and routing tables and interfaces, parsed by
    the real SR Linux driver from recorded responses. Needs NETGPT_TEST_PASSWORD set
    to anything.
    """
    credentials = Credentials(username="admin", password_env="NETGPT_TEST_PASSWORD")
    lab = RecordedLab([], [], [], [], {})
    for info in LAB:
        transport, _ = replay(RECORDED[info.name])
        device = Device(name=info.name, host=info.host, platform=info.platform, credentials="lab")
        async with SRLinuxPlatform(device, credentials, transport=transport) as platform:
            lab.neighbors.extend(await platform.lldp_neighbors())
            lab.arp.extend(await platform.arp_table())
            lab.macs.extend(await platform.mac_table())
            lab.interfaces.extend(await platform.interfaces())
            lab.routes[info.name] = await platform.routes("default")
    return lab


# The WAN lab (lab/wan/): three sites joined by OSPF, and the responses recorded from each of its devices.
WAN = [
    DeviceInfo(name="hq-core", platform="nokia_srlinux", host="172.20.21.11", role="core"),
    DeviceInfo(name="hq-dc", platform="nokia_srlinux", host="172.20.21.12", role="datacenter"),
    DeviceInfo(name="hq-sw1", platform="nokia_srlinux", host="172.20.21.13", role="access"),
    DeviceInfo(name="branch", platform="nokia_srlinux", host="172.20.21.31", role="branch"),
    DeviceInfo(name="ops-core", platform="nokia_srlinux", host="172.20.21.21", role="core"),
    DeviceInfo(name="ops-dc", platform="nokia_srlinux", host="172.20.21.22", role="datacenter"),
]
WAN_TABLES = TABLES | {"/network-instance[name=*]/protocols/ospf": "ospf.json"}
RECORDED_WAN = {device.name: {path: f"wan/{device.name}/{name}" for path, name in WAN_TABLES.items()} for device in WAN}


class RecordedWan(NamedTuple):
    """Every WAN lab device's interfaces, OSPF, and routes, as the SR Linux driver parses them."""

    interfaces: list[Interface]
    ospf_interfaces: list[OspfInterface]
    ospf_neighbors: list[OspfNeighbor]
    routes: dict[str, list[Route]]

    def ospf(self, errors: list[DeviceError] | None = None) -> Topology:
        return build_ospf(WAN, self.interfaces, self.ospf_interfaces, self.ospf_neighbors, errors or [])


async def recorded_wan() -> RecordedWan:
    """
    The WAN lab's tables, parsed by the real SR Linux driver from recorded responses.
    Needs NETGPT_TEST_PASSWORD set to anything.
    """
    credentials = Credentials(username="admin", password_env="NETGPT_TEST_PASSWORD")
    wan = RecordedWan([], [], [], {})
    for info in WAN:
        transport, _ = replay(RECORDED_WAN[info.name])
        device = Device(name=info.name, host=info.host, platform=info.platform, credentials="lab")
        async with SRLinuxPlatform(device, credentials, transport=transport) as platform:
            wan.interfaces.extend(await platform.interfaces())
            wan.ospf_interfaces.extend(await platform.ospf_interfaces())
            wan.ospf_neighbors.extend(await platform.ospf_neighbors())
            wan.routes[info.name] = await platform.routes("default")
    return wan


class SwitchedNetwork:
    """
    A small routed network with a layer 2 switch in it, as the devices would report it:

        r0 (e1/1) ── (e1/49) r1 (e1/1) ── (e1/1) sw1 (e1/2) ── 10.1.0.2
                                                     (e1/3) ── 10.1.0.3

    r0 and r1 share 10.0.0.0/31; r1 routes 10.1.0.0/24, which sw1 bridges (in 'lan')
    out to the two hosts. Hosts don't speak LLDP, so only ARP on r1 and sw1's MAC
    table say where they are.
    """

    HOST2, HOST3 = "10.1.0.2", "10.1.0.3"
    MAC2, MAC3, R1_MAC = "AA:00:00:00:00:02", "AA:00:00:00:00:03", "1A:00:00:00:01:01"

    def __init__(self) -> None:
        self.devices = [
            DeviceInfo(name="r0", platform="nokia_srlinux", host="172.20.20.10", role="spine"),
            DeviceInfo(name="r1", platform="nokia_srlinux", host="172.20.20.20", role="leaf"),
            DeviceInfo(name="sw1", platform="nokia_srlinux", host="172.20.20.40", role="access"),
        ]
        self.neighbors = [
            LldpNeighbor(device="r0", interface="ethernet-1/1", neighbor="r1", neighbor_interface="ethernet-1/49"),
            LldpNeighbor(device="r1", interface="ethernet-1/49", neighbor="r0", neighbor_interface="ethernet-1/1"),
            LldpNeighbor(device="r1", interface="ethernet-1/1", neighbor="sw1", neighbor_interface="ethernet-1/1"),
            LldpNeighbor(device="sw1", interface="ethernet-1/1", neighbor="r1", neighbor_interface="ethernet-1/1"),
        ]
        self.arp = [
            ArpEntry(device="r0", interface="ethernet-1/1.0", ip_address="10.0.0.1", mac="1A:00:00:00:01:49"),
            ArpEntry(device="r1", interface="ethernet-1/49.0", ip_address="10.0.0.0", mac="1A:00:00:00:00:01"),
            ArpEntry(device="r1", interface="ethernet-1/1.0", ip_address=self.HOST2, mac=self.MAC2),
            ArpEntry(device="r1", interface="ethernet-1/1.0", ip_address=self.HOST3, mac=self.MAC3),
        ]
        self.macs = [
            MacEntry(device="sw1", network_instance="lan", mac=self.R1_MAC, interface="ethernet-1/1.0", entry_type="learnt"),
            MacEntry(device="sw1", network_instance="lan", mac=self.MAC2, interface="ethernet-1/2.0", entry_type="learnt"),
            MacEntry(device="sw1", network_instance="lan", mac=self.MAC3, interface="ethernet-1/3.0", entry_type="learnt"),
        ]
        self.interfaces = [
            Interface(device="r0", name="ethernet-1/1.0", network_instance="default", mode="routed", addresses=["10.0.0.0/31"]),
            Interface(device="r1", name="ethernet-1/49.0", network_instance="default", mode="routed", addresses=["10.0.0.1/31"]),
            Interface(device="r1", name="ethernet-1/1.0", network_instance="default", mode="routed", addresses=["10.1.0.1/24"]),
            *(
                Interface(device="sw1", name=f"ethernet-1/{port}.0", network_instance="lan", mode="bridged")
                for port in (1, 2, 3)
            ),
        ]

        def route(device: str, prefix: str, protocol: str, hop: NextHop) -> Route:
            return Route(device=device, network_instance="default", prefix=prefix, protocol=protocol, next_hops=[hop])

        self.routes = {
            "r0": [
                route("r0", "10.0.0.0/31", "local", NextHop(interface="ethernet-1/1.0")),
                route("r0", "10.0.0.0/32", "host", NextHop(action="receive")),
                route("r0", "10.1.0.0/24", "static", NextHop(address="10.0.0.1")),
            ],
            "r1": [
                route("r1", "10.0.0.0/31", "local", NextHop(interface="ethernet-1/49.0")),
                route("r1", "10.0.0.1/32", "host", NextHop(action="receive")),
                route("r1", "10.1.0.0/24", "local", NextHop(interface="ethernet-1/1.0")),
                route("r1", "10.1.0.1/32", "host", NextHop(action="receive")),
                route("r1", "0.0.0.0/0", "static", NextHop(address="10.0.0.0")),
            ],
        }

    def topology(self, is_management: Any) -> Topology:
        return build_topology(self.devices, self.neighbors, self.arp, [], is_management, self.macs, self.interfaces)


class FakePlatform(Platform):
    """
    Answers from canned data: each device is cabled to spine1 (outside the
    inventory) on ethernet-1/49, with one host behind ethernet-1/1 and a default
    route toward spine1. A device named 'broken' fails like an unreachable device.
    """

    policy = NOKIA_SRLINUX
    management_interfaces = re.compile(r"^mgmt\d+$")
    commands_run: list[str] = []
    pings: list[tuple[str, str, int, str]] = []
    counter_reads: dict[str, int] = {}

    async def _run_checked_cli(self, command: str) -> str:
        FakePlatform.commands_run.append(command)
        return f"{self.device.name}: ran {command}"

    async def lldp_neighbors(self) -> list[LldpNeighbor]:
        if self.device.name == "broken":
            raise PlatformError("broken: connection refused")
        return [LldpNeighbor(device=self.device.name, interface="ethernet-1/49", neighbor="spine1")]

    async def arp_table(self) -> list[ArpEntry]:
        if self.device.name == "broken":
            raise PlatformError("broken: connection refused")
        host = {"leaf1": "198.51.100.11", "leaf2": "198.51.100.12"}[self.device.name]
        return [ArpEntry(device=self.device.name, interface="ethernet-1/1.0", ip_address=host, mac="02:00:00:00:00:01")]

    async def interface_counters(self) -> list[InterfaceCounters]:
        """
        Each read finds 37,500,000 more octets in and 3,750,000 more out on ethernet-1/1, a
        1G port, and two more errors in: 10 and 1 Mbit/s when the reads are 30 seconds apart.
        """
        if self.device.name == "broken":
            raise PlatformError("broken: connection refused")
        reads = FakePlatform.counter_reads[self.device.name] = FakePlatform.counter_reads.get(self.device.name, 0) + 1
        return [
            InterfaceCounters(
                device=self.device.name,
                interface="ethernet-1/1",
                oper_state="up",
                speed_bps=10**9,
                in_octets=reads * 37_500_000,
                out_octets=reads * 3_750_000,
                in_packets=reads * 25_000,
                out_packets=reads * 2_500,
                in_errors=reads * 2,
                out_errors=0,
                in_discards=0,
                out_discards=0,
                carrier_transitions=1,
            )
        ]

    async def ping(self, destination: str, count: int, network_instance: str) -> PingResult:
        """Every network's .1 and .2 answer; nothing else does."""
        if self.device.name == "broken":
            raise PlatformError("broken: connection refused")
        FakePlatform.pings.append((self.device.name, destination, count, network_instance))
        received = count if destination.rsplit(".", 1)[-1] in ("1", "2") else 0
        return PingResult(source=self.device.name, destination=destination, sent=count, received=received)

    async def routes(self, network_instance: str) -> list[Route]:
        if self.device.name == "broken":
            raise PlatformError("broken: connection refused")
        hosts = {"leaf1": "198.51.100.8/30", "leaf2": "198.51.100.12/30"}[self.device.name]
        table = [
            (hosts, "local", NextHop(interface="ethernet-1/1.0")),
            ("10.0.0.0/31", "local", NextHop(interface="ethernet-1/49.0")),
            ("0.0.0.0/0", "static", NextHop(address="10.0.0.1")),
        ]
        return [
            Route(device=self.device.name, network_instance=network_instance, prefix=prefix, protocol=protocol, next_hops=[hop])
            for prefix, protocol, hop in table
        ]


def calls(name: str, arguments: dict[str, Any], call_id: str = "call_1", content: str = "") -> dict[str, Any]:
    """A model reply that calls one tool."""
    tool_calls = [{"id": call_id, "function": {"name": name, "arguments": arguments}}]
    return {"role": "assistant", "content": content, "tool_calls": tool_calls}


def says(text: str) -> dict[str, Any]:
    """A model reply that answers."""
    return {"role": "assistant", "content": text}


class ScriptedOllama:
    """
    Streams the scripted replies to /api/chat in order, and keeps every request.
    A reply given as a list is streamed as those chunks. With gated=True, each
    reply waits for gate to be set, so a test can hold an answer in flight.
    """

    def __init__(self, *replies: dict[str, Any] | list[dict[str, Any]], gated: bool = False) -> None:
        self.replies = list(replies)
        self.requests: list[dict[str, Any]] = []
        self.entered = asyncio.Event()
        self.gate = asyncio.Event()
        if not gated:
            self.gate.set()

    async def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(json.loads(request.content))
        self.entered.set()
        await self.gate.wait()
        if not self.replies:
            return httpx.Response(404, json={"error": "model 'missing' not found"})
        reply = self.replies.pop(0)
        chunks = reply if isinstance(reply, list) else [reply]
        lines = [json.dumps({"message": chunk, "done": False}) for chunk in chunks]
        lines.append(json.dumps({"message": {"role": "assistant", "content": ""}, "done": True}))
        return httpx.Response(200, text="\n".join(lines) + "\n", headers={"Content-Type": "application/x-ndjson"})

    def client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(base_url="http://ollama.test", transport=httpx.MockTransport(self.handler))

    def messages(self, request: int) -> list[dict[str, Any]]:
        return self.requests[request]["messages"]

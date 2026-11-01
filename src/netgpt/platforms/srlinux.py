"""
The SR Linux platform module talks to Nokia SR Linux over its JSON-RPC API.

State comes from the YANG `get` method, so most queries need no screen scraping.
CLI commands go through the JSON-RPC `cli` method after the read-only check.
"""

from __future__ import annotations

import ipaddress
import re
from typing import Any, NamedTuple

import httpx

from netgpt.guard import NOKIA_SRLINUX
from netgpt.inventory import Credentials, Device
from netgpt.platforms.base import Platform, PlatformError
from netgpt.primitives import (
    ArpEntry,
    CommandOutput,
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

NETWORK_INSTANCE_NAME = re.compile(r"^[A-Za-z0-9_.-]{1,64}$")
PING_COUNTS = re.compile(r"(\d+) packets transmitted, (\d+) received")
PING_RTT = re.compile(r"rtt min/avg/max/mdev = [\d.]+/([\d.]+)/")

# Each subinterface's type, state, and addresses, and which network instance (and kind of instance) it is in.
INTERFACE_PATHS = [
    "/interface[name=*]/subinterface[index=*]/type",
    "/interface[name=*]/subinterface[index=*]/oper-state",
    "/interface[name=*]/subinterface[index=*]/ipv4/address[ip-prefix=*]/ip-prefix",
    "/network-instance[name=*]/type",
    "/network-instance[name=*]/interface[name=*]/name",
]

# Each port's state, speed, and counters. Every port is listed, but one with no configuration has no counters.
COUNTER_PATHS = [
    "/interface[name=*]/oper-state",
    "/interface[name=*]/ethernet/port-speed",
    "/interface[name=*]/statistics",
]
# Every OSPF instance's interfaces, with their neighbors, and its router ID. Narrow paths, so the link-state
# database stays on the device.
OSPF_PATHS = [
    "/network-instance[name=*]/protocols/ospf/instance[name=*]/area[area-id=*]/interface[interface-name=*]",
    "/network-instance[name=*]/protocols/ospf/instance[name=*]/router-id",
    # An area is normal unless it has one of these.
    "/network-instance[name=*]/protocols/ospf/instance[name=*]/area[area-id=*]/stub",
    "/network-instance[name=*]/protocols/ospf/instance[name=*]/area[area-id=*]/nssa",
    # Each keychain's algorithm, and nothing else: its keys are never read.
    "/system/authentication/keychain[name=*]/key[index=*]/algorithm",
]
# An interface that names no keychain sends OSPF packets unauthenticated.
NO_AUTHENTICATION = "none"
# SR Linux's OSPF names no router with 0.0.0.0, for example the DR of a point-to-point link.
NO_ROUTER = "0.0.0.0"

PORT_SPEED = re.compile(r"^(\d+)([MGT])$")
SPEED_UNITS = {"M": 10**6, "G": 10**9, "T": 10**12}

# Added to the error when SR Linux can't parse a command. Models reach for Cisco's commands, or for SR OS's
# (show router ospf), which SR Linux doesn't have, and then try variations of them; working examples and the
# typed tools stop the guessing.
CLI_HINT = (
    "This device runs Nokia SR Linux, whose commands differ from Cisco's and from Nokia SR OS's. For example: "
    "show interface brief, show network-instance default route-table, "
    "show network-instance <name> bridge-table mac-table all, show arpnd arp-entries, "
    "show network-instance default protocols ospf neighbor, info from running network-instance default static-routes, "
    "info from running network-instance default protocols ospf (OSPF's configuration: areas, costs, timers, "
    "authentication). For interfaces, ARP, MAC tables, routes, LLDP, and OSPF, the typed tools (interfaces, "
    "arp_table, mac_table, routes, lldp_neighbors, ospf_neighbors, ospf_interfaces) need no commands."
)

# What SR Linux's next-hop types do with a packet; any other type forwards it.
NEXT_HOP_ACTIONS = {"extract": "receive", "broadcast": "receive", "blackhole": "drop", "drop": "drop", "reject": "drop"}


def strip_module_prefixes(node: Any) -> Any:
    """Remove YANG module prefixes from keys, so 'srl_nokia-interfaces:interface' becomes 'interface'."""
    if isinstance(node, dict):
        return {key.split(":", 1)[-1]: strip_module_prefixes(value) for key, value in node.items()}
    if isinstance(node, list):
        return [strip_module_prefixes(item) for item in node]
    return node


def as_list(node: Any) -> list[Any]:
    """Return a YANG list as a Python list; absent lists become empty."""
    if node is None:
        return []
    return node if isinstance(node, list) else [node]


def ospf_instances(found: dict[str, Any]) -> list[tuple[str, dict[str, Any]]]:
    """Every OSPF instance in a get's result, as (network instance, the OSPF instance)."""
    return [
        (instance["name"], ospf)
        for instance in as_list(found.get("network-instance"))
        for ospf in as_list(instance.get("protocols", {}).get("ospf", {}).get("instance"))
    ]


class OspfPort(NamedTuple):
    """One interface OSPF runs on, as SR Linux's state has it, with what its router, area, and keychain say."""

    network_instance: str
    router_id: str | None
    area: str
    area_type: str
    interface: dict[str, Any]
    authentication: str
    keychain: str | None


def next_hop(hop: dict[str, Any], protocol: str) -> NextHop:
    """
    Read one next hop. SR Linux gives a connected (local) route the router's own
    address as its next hop; for those the interface is the whole answer. An
    indirect next hop has an address and no interface: the route to that address
    says which way it goes.
    """
    action = NEXT_HOP_ACTIONS.get(str(hop.get("type", "")).split(":")[-1], "forward")
    if action != "forward":
        return NextHop(action=action)
    address = None if protocol == "local" else hop.get("ip-address")
    return NextHop(address=address, interface=hop.get("subinterface"))


def router_or_none(value: Any) -> str | None:
    """Read a router ID, where 0.0.0.0 means none."""
    return None if value in (None, NO_ROUTER) else str(value)


def port_speed(value: Any) -> int | None:
    """Read a port speed such as 25G or 100M as bits per second."""
    match = PORT_SPEED.match(str(value or ""))
    return int(match.group(1)) * SPEED_UNITS[match.group(2)] if match else None


def parse_ping_output(device: str, destination: str, output: str) -> PingResult:
    """Parse the summary of Linux ping output, which SR Linux's ping command prints."""
    counts = PING_COUNTS.search(output)
    if counts is None:
        raise PlatformError(f"{device}: unexpected ping output: {output.strip()[:200]}")
    rtt = PING_RTT.search(output)
    return PingResult(
        source=device,
        destination=destination,
        sent=int(counts.group(1)),
        received=int(counts.group(2)),
        rtt_avg_ms=float(rtt.group(1)) if rtt else None,
    )


class SRLinuxPlatform(Platform):
    policy = NOKIA_SRLINUX
    management_interfaces = re.compile(r"^mgmt\d+$")
    # Each ping is a CLI session on the device; past about 16 at once, a sweep gets no faster.
    sweep_width = 16

    def __init__(
        self,
        device: Device,
        credentials: Credentials,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        """The transport is only for tests, which replay recorded responses through it."""
        super().__init__(device, credentials)
        verify: str | bool = str(device.tls_ca) if device.tls_ca else True
        self._client = httpx.AsyncClient(
            base_url=f"https://{device.host}",
            auth=(credentials.username, credentials.password()),
            verify=verify,
            timeout=30.0,
            transport=transport,
        )
        self._request_id = 0

    async def close(self) -> None:
        await self._client.aclose()

    async def _rpc(self, method: str, params: dict[str, Any]) -> list[Any]:
        """Make one JSON-RPC call and return its result list."""
        self._request_id += 1
        payload = {"jsonrpc": "2.0", "id": self._request_id, "method": method, "params": params}
        try:
            response = await self._client.post("/jsonrpc", json=payload)
            response.raise_for_status()
        except httpx.HTTPStatusError as error:
            raise PlatformError(f"{self.device.name}: HTTP {error.response.status_code} from JSON-RPC") from error
        except httpx.HTTPError as error:
            raise PlatformError(f"{self.device.name}: {type(error).__name__}: {error}") from error
        body = response.json()
        if "error" in body:
            raise PlatformError(f"{self.device.name}: {body['error'].get('message', body['error'])}")
        return body["result"]

    async def _get_state(self, path: str) -> dict[str, Any]:
        """Fetch one path from the state datastore with module prefixes removed."""
        result = await self._rpc("get", {"commands": [{"path": path, "datastore": "state"}]})
        return strip_module_prefixes(result[0]) if result else {}

    async def _get_states(self, paths: list[str]) -> list[dict[str, Any]]:
        """Fetch several paths from the state datastore in one call, with module prefixes removed."""
        result = await self._rpc("get", {"commands": [{"path": path, "datastore": "state"} for path in paths]})
        return [strip_module_prefixes(item) for item in result]

    async def show(self, command: str, max_chars: int = 50_000) -> CommandOutput:
        # Only for show: ping's commands are built here, so its parse errors are about its arguments, not the CLI.
        try:
            return await super().show(command, max_chars)
        except PlatformError as error:
            if "Parsing error" in str(error):
                raise PlatformError(f"{error.args[0].rstrip()} {CLI_HINT}") from error
            raise

    async def _run_checked_cli(self, command: str) -> str:
        result = await self._rpc("cli", {"commands": [command], "output-format": "text"})
        return "".join(str(item) for item in result)

    async def lldp_neighbors(self) -> list[LldpNeighbor]:
        lldp = await self._get_state("/system/lldp")
        return [
            LldpNeighbor(
                device=self.device.name,
                interface=interface["name"],
                neighbor=neighbor.get("system-name"),
                neighbor_interface=neighbor.get("port-id"),
                neighbor_chassis_id=neighbor.get("chassis-id"),
            )
            for interface in as_list(lldp.get("interface"))
            for neighbor in as_list(interface.get("neighbor"))
        ]

    async def arp_table(self) -> list[ArpEntry]:
        state = await self._get_state("/interface[name=*]/subinterface[index=*]/ipv4/arp/neighbor")
        return [
            ArpEntry(
                device=self.device.name,
                interface=f"{interface['name']}.{subinterface['index']}",
                ip_address=neighbor["ipv4-address"],
                mac=neighbor.get("link-layer-address"),
                origin=neighbor.get("origin"),
            )
            for interface in as_list(state.get("interface"))
            for subinterface in as_list(interface.get("subinterface"))
            for neighbor in as_list(subinterface.get("ipv4", {}).get("arp", {}).get("neighbor"))
        ]

    async def interfaces(self) -> list[Interface]:
        # Narrow paths rather than whole subinterfaces, which carry ARP tables and counters.
        types, states, addresses, instance_types, members = await self._get_states(INTERFACE_PATHS)

        def by_subinterface(state: dict[str, Any]) -> dict[str, dict[str, Any]]:
            return {
                f"{interface['name']}.{subinterface['index']}": subinterface
                for interface in as_list(state.get("interface"))
                for subinterface in as_list(interface.get("subinterface"))
            }

        kinds = {
            instance["name"]: str(instance.get("type", "")).split(":")[-1]
            for instance in as_list(instance_types.get("network-instance"))
        }
        instance_of = {
            member["name"]: instance["name"]
            for instance in as_list(members.get("network-instance"))
            for member in as_list(instance.get("interface"))
        }
        modes, operational, prefixes = by_subinterface(types), by_subinterface(states), by_subinterface(addresses)
        found = []
        for name in modes | operational | prefixes:
            instance = instance_of.get(name)
            declared = str(modes.get(name, {}).get("type", "")).split(":")[-1]
            bridged = declared == "bridged" or (not declared and kinds.get(instance or "") == "mac-vrf")
            found.append(
                Interface(
                    device=self.device.name,
                    name=name,
                    network_instance=instance,
                    mode="bridged" if bridged else "routed",
                    addresses=[
                        address["ip-prefix"] for address in as_list(prefixes.get(name, {}).get("ipv4", {}).get("address"))
                    ],
                    oper_state=operational.get(name, {}).get("oper-state"),
                )
            )
        return found

    async def interface_counters(self) -> list[InterfaceCounters]:
        states, speeds, statistics = await self._get_states(COUNTER_PATHS)

        def by_port(state: dict[str, Any]) -> dict[str, dict[str, Any]]:
            return {interface["name"]: interface for interface in as_list(state.get("interface"))}

        oper, speed = by_port(states), by_port(speeds)
        found = []
        for name, port in by_port(statistics).items():
            counts = port.get("statistics") or {}
            if not counts:
                continue

            def count(key: str) -> int:
                return int(counts.get(key, 0))

            found.append(
                InterfaceCounters(
                    device=self.device.name,
                    interface=name,
                    oper_state=oper.get(name, {}).get("oper-state"),
                    speed_bps=port_speed(speed.get(name, {}).get("ethernet", {}).get("port-speed")),
                    in_octets=count("in-octets"),
                    out_octets=count("out-octets"),
                    in_packets=count("in-packets"),
                    out_packets=count("out-packets"),
                    in_errors=count("in-error-packets"),
                    out_errors=count("out-error-packets"),
                    in_discards=count("in-discarded-packets"),
                    out_discards=count("out-discarded-packets"),
                    carrier_transitions=int(counts["carrier-transitions"]) if "carrier-transitions" in counts else None,
                )
            )
        return found

    async def mac_table(self) -> list[MacEntry]:
        state = await self._get_state("/network-instance[name=*]/bridge-table/mac-table/mac")
        return [
            MacEntry(
                device=self.device.name,
                network_instance=instance["name"],
                mac=mac["address"],
                interface=mac.get("destination", ""),
                entry_type=mac.get("type"),
            )
            for instance in as_list(state.get("network-instance"))
            for mac in as_list(instance.get("bridge-table", {}).get("mac-table", {}).get("mac"))
        ]

    async def ping(self, destination: str, count: int, network_instance: str) -> PingResult:
        # The command is built from validated parts rather than taken from the model,
        # so it bypasses the read-only CLI policy without opening a hole in it.
        try:
            address = ipaddress.IPv4Address(destination)
        except ValueError:
            raise PlatformError(f"'{destination}' is not an IPv4 address.") from None
        if not NETWORK_INSTANCE_NAME.match(network_instance):
            raise PlatformError(f"'{network_instance}' is not a valid network instance name.")
        if not 1 <= count <= 10:
            raise PlatformError("count must be between 1 and 10.")
        command = f"ping {address} network-instance {network_instance} -c {count} -W 1"
        try:
            output = await self._run_checked_cli(command)
        except PlatformError as error:
            # ping exits non-zero when nothing answers, which JSON-RPC sometimes reports
            # as an error. The output inside it is still a valid (0 received) result.
            if PING_COUNTS.search(str(error)) is None:
                raise
            output = str(error)
        return parse_ping_output(self.device.name, str(address), output)

    async def _ospf(self) -> list[OspfPort]:
        """Every interface OSPF runs on, with what its router, area, and keychain say about it."""
        interfaces, router_ids, stubs, nssas, keychains = await self._get_states(OSPF_PATHS)
        ids = {(name, ospf["name"]): ospf.get("router-id") for name, ospf in ospf_instances(router_ids)}
        area_types = {
            (name, ospf["name"], area["area-id"]): kind
            for kind, found in (("stub", stubs), ("nssa", nssas))
            for name, ospf in ospf_instances(found)
            for area in as_list(ospf.get("area"))
            if kind in area
        }
        # The path has no wildcard until the keychain, so the result is rooted there.
        algorithms = {
            keychain["name"]: "/".join(sorted({key["algorithm"] for key in as_list(keychain.get("key")) if "algorithm" in key}))
            for keychain in as_list(keychains.get("keychain"))
        }
        ports = []
        for name, ospf in ospf_instances(interfaces):
            for area in as_list(ospf.get("area")):
                for interface in as_list(area.get("interface")):
                    keychain = interface.get("authentication", {}).get("keychain")
                    authentication = (algorithms.get(keychain) or "unknown") if keychain else NO_AUTHENTICATION
                    area_type = area_types.get((name, ospf["name"], area["area-id"]), "normal")
                    ports.append(OspfPort(name, ids.get((name, ospf["name"])), area["area-id"], area_type, interface,
                                          authentication, keychain))
        return ports

    async def ospf_interfaces(self) -> list[OspfInterface]:
        return [
            OspfInterface(
                device=self.device.name,
                network_instance=port.network_instance,
                router_id=port.router_id,
                area=port.area,
                area_type=port.area_type,
                interface=port.interface["interface-name"],
                address=port.interface.get("local-ip-address"),
                network_type=port.interface.get("interface-type"),
                state=port.interface.get("oper-state"),
                passive=bool(port.interface.get("passive", False)),
                authentication=port.authentication,
                keychain=port.keychain,
                cost=port.interface.get("metric"),
                hello_interval=port.interface.get("hello-interval"),
                dead_interval=port.interface.get("dead-interval"),
                mtu=port.interface.get("mtu"),
                neighbors=port.interface.get("neighbor-count"),
                designated_router=router_or_none(port.interface.get("dr-id")),
                backup_designated_router=router_or_none(port.interface.get("bdr-id")),
            )
            for port in await self._ospf()
        ]

    async def ospf_neighbors(self) -> list[OspfNeighbor]:
        # up-time is in hundredths of a second.
        return [
            OspfNeighbor(
                device=self.device.name,
                network_instance=port.network_instance,
                area=port.area,
                interface=port.interface["interface-name"],
                router_id=neighbor["router-id"],
                address=neighbor.get("address"),
                state=str(neighbor.get("adjacency-state", "unknown")).split(":")[-1],
                priority=neighbor.get("priority"),
                dead_seconds=neighbor.get("dead-time"),
                uptime_seconds=neighbor["up-time"] // 100 if isinstance(neighbor.get("up-time"), int) else None,
                state_changes=neighbor.get("state-changes"),
            )
            for port in await self._ospf()
            for neighbor in as_list(port.interface.get("neighbor"))
        ]

    async def routes(self, network_instance: str) -> list[Route]:
        # A path without wildcards comes back rooted at the route-table container itself.
        # Routes point at next-hop groups, whose members point at next hops, all by index.
        route_table = await self._get_state(f"/network-instance[name={network_instance}]/route-table")
        next_hops = {hop["index"]: hop for hop in as_list(route_table.get("next-hop"))}
        groups = {
            group["index"]: [next_hops.get(member["next-hop"], {}) for member in as_list(group.get("next-hop"))]
            for group in as_list(route_table.get("next-hop-group"))
        }
        routes = []
        for route in as_list(route_table.get("ipv4-unicast", {}).get("route")):
            protocol = str(route.get("route-type", "unknown")).split(":")[-1]
            hops = groups.get(route.get("next-hop-group"), [])
            routes.append(
                Route(
                    device=self.device.name,
                    network_instance=network_instance,
                    prefix=route["ipv4-prefix"],
                    protocol=protocol,
                    next_hops=[next_hop(hop, protocol) for hop in hops],
                    active=route.get("active"),
                    metric=route.get("metric"),
                    preference=route.get("preference"),
                )
            )
        return routes

"""
The SR Linux platform module talks to Nokia SR Linux over its JSON-RPC API.

State comes from the YANG `get` method, so most queries need no screen scraping.
CLI commands go through the JSON-RPC `cli` method after the read-only check.
"""

from __future__ import annotations

import ipaddress
import re
from typing import Any

import httpx

from netgpt.guard import NOKIA_SRLINUX
from netgpt.inventory import Credentials, Device
from netgpt.platforms.base import Platform, PlatformError
from netgpt.primitives import ArpEntry, LldpNeighbor, MacEntry, PingResult, Route

NETWORK_INSTANCE_NAME = re.compile(r"^[A-Za-z0-9_.-]{1,64}$")
PING_COUNTS = re.compile(r"(\d+) packets transmitted, (\d+) received")
PING_RTT = re.compile(r"rtt min/avg/max/mdev = [\d.]+/([\d.]+)/")


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


def describe_next_hop(hop: dict[str, Any], protocol: str) -> str:
    """
    Render one resolved next hop. SR Linux gives a connected (local) route the
    router's own address as its next hop, so for those the interface is the
    meaningful answer.
    """
    address, subinterface = hop.get("ip-address"), hop.get("subinterface")
    if protocol == "local" and subinterface:
        return subinterface
    if address and subinterface:
        return f"{address} via {subinterface}"
    return address or subinterface or str(hop.get("type", "unknown")).split(":")[-1]


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
                    next_hops=[describe_next_hop(hop, protocol) for hop in hops],
                    active=route.get("active"),
                    metric=route.get("metric"),
                    preference=route.get("preference"),
                )
            )
        return routes

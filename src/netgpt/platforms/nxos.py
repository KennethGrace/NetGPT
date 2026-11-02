"""
The nxos module talks to Cisco NX-OS over SSH.

NX-OS answers most show commands with structured output when `| json` follows
them, and runs each command on an exec channel of its own, several over one
connection, so no prompt is scraped and no text is parsed. Some releases give up
partway through turning a reply into JSON and send the tail of its XML instead
(7.0(3)I7 on a Nexus 9000 does it for OSPF), so the driver then asks for `| xml`,
which the JSON is made from, and reads it into the same shape.

NX-OS's JSON is a tree of TABLE_x and ROW_x: a table with one row holds it bare,
and one with several holds a list, so every read goes through rows(). Numbers
are strings on some releases and numbers on others.
"""

from __future__ import annotations

import asyncio
import json
import re
import xml.etree.ElementTree as ElementTree
from typing import Any, NamedTuple, Protocol

import asyncssh

from netgpt.guard import CISCO_NXOS
from netgpt.inventory import Credentials, Device
from netgpt.platforms.base import Platform, PlatformError
from netgpt.platforms.ssh import SshLogin
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
    Route,
)

NETWORK_INSTANCE_NAME = re.compile(r"^[A-Za-z0-9_.:-]{1,32}$")
# Seconds to wait for one command. A Nexus 9000's whole `show interface` takes about two; now and then
# a command never finishes, and is run again.
COMMAND_TIMEOUT = 30.0
# The ports whose counters are polled: Ethernet ports, breakouts included, and port-channels.
PORTS = re.compile(r"^(Ethernet\d+(/\d+)+|port-channel\d+)$")
# NX-OS shortens interface names in some tables (LLDP, routes): Eth1/1 is Ethernet1/1.
SHORT_NAMES = [(re.compile(r"^Eth(?=\d)", re.IGNORECASE), "Ethernet"), (re.compile(r"^Po(?=\d)", re.IGNORECASE), "port-channel"),
               (re.compile(r"^Lo(?=\d)", re.IGNORECASE), "loopback")]
# What NX-OS writes for a value a neighbor didn't send.
ABSENT = {"", "null", "not advertised", "n/a"}
# An ISO 8601 duration, as NX-OS reports ages and timers: P4M25DT9H20M51S.
DURATION = re.compile(
    r"^P(?:(?P<years>\d+)Y)?(?:(?P<months>\d+)M)?(?:(?P<weeks>\d+)W)?(?:(?P<days>\d+)D)?"
    r"(?:T(?:(?P<hours>\d+)H)?(?:(?P<minutes>\d+)M)?(?:(?P<seconds>\d+(?:\.\d+)?)S)?)?$"
)
DURATION_SECONDS = {"years": 365 * 86400, "months": 30 * 86400, "weeks": 7 * 86400, "days": 86400, "hours": 3600,
                    "minutes": 60, "seconds": 1}
# Route owners whose routes are the device's own addresses: its interfaces', and the FHRP addresses it shares.
RECEIVING_OWNERS = {"local", "hsrp", "vrrp"}
NETWORK_TYPES = {"broadcast": "broadcast", "p2p": "point-to-point", "loopback": "loopback"}
OSPF_STATES = {"dr": "designated-router", "bdr": "backup-designated-router", "drother": "dr-other", "p2p": "point-to-point",
               "down": "down", "waiting": "waiting", "loopback": "loopback"}
NEIGHBOR_STATES = {"2way": "2-way"}

# Added to the error when NX-OS can't parse a command. Models reach for IOS's or Junos's commands;
# working examples and the typed tools stop the guessing.
CLI_HINT = (
    "This device runs Cisco NX-OS. For example: show interface brief, show ip interface brief vrf all, "
    "show ip route vrf all, show ip arp vrf all, show mac address-table, show lldp neighbors, show vpc brief, "
    "show port-channel summary, show ip ospf neighbors vrf all, show running-config interface Ethernet1/1. "
    "The typed tools (interfaces, arp_table, mac_table, routes, lldp_neighbors, ospf_interfaces, ospf_neighbors) "
    "return the same records from every platform."
)


class Reply(NamedTuple):
    """What one command printed, and its exit status: 0 when it ran."""

    status: int
    output: str


class Commands(Protocol):
    """A way to run CLI commands on one device."""

    async def run(self, command: str) -> Reply: ...

    async def close(self) -> None: ...


class ExecOverSsh:
    """CLI commands over one SSH connection, each on an exec channel of its own."""

    def __init__(self, device: Device, credentials: Credentials) -> None:
        self._device = device
        self._login = SshLogin(device, credentials)

    async def run(self, command: str) -> Reply:
        for attempt in (1, 2):
            connection = await self._login.connection()
            try:
                result = await asyncio.wait_for(connection.run(command, check=False), COMMAND_TIMEOUT)
                break
            except (asyncssh.ChannelOpenError, asyncssh.ConnectionLost, TimeoutError) as error:
                # Now and then a switch closes the connection between commands, or never finishes one
                # that usually takes a second. Log in again, once: a show command is safe to run twice.
                await self.close()
                if attempt == 2:
                    raise PlatformError(f"{self._device.name}: {type(error).__name__}: {error}") from error
            except (OSError, asyncssh.Error) as error:
                await self.close()
                raise PlatformError(f"{self._device.name}: {type(error).__name__}: {error}") from error
        output = result.stdout if isinstance(result.stdout, str) else (result.stdout or b"").decode(errors="replace")
        return Reply(result.exit_status or 0, output)

    async def close(self) -> None:
        await self._login.close()


def rows(node: Any, *path: str) -> list[dict[str, Any]]:
    """The rows at a path of TABLE_x and ROW_x keys, as a list whether NX-OS sent one row or several."""
    for key in path:
        node = node.get(key) if isinstance(node, dict) else None
        if node is None:
            return []
    found = node if isinstance(node, list) else [node]
    return [row for row in found if isinstance(row, dict)]


def text(row: dict[str, Any], key: str) -> str | None:
    """A value as text, or None when it is absent or NX-OS's word for absent."""
    value = row.get(key)
    if value is None or isinstance(value, (dict, list)):
        return None
    value = str(value).strip()
    return None if value.lower() in ABSENT else value


def number(row: dict[str, Any], key: str) -> int | None:
    value = text(row, key)
    return int(value) if value is not None and value.isdigit() else None


def full_name(interface: str) -> str:
    """An interface named as NX-OS's interface tables name it: Eth1/1 is Ethernet1/1, Po10 is port-channel10."""
    for short, full in SHORT_NAMES:
        interface = short.sub(full, interface)
    return interface


def seconds_of(duration: str | None) -> int | None:
    """
    An ISO 8601 duration in seconds. NX-OS counts long ones in months, which are
    taken as 30 days, so an age of months is good to a few days.
    """
    match = DURATION.match(duration or "")
    if not match or not any(match.groupdict().values()):
        return None
    return int(sum(float(value) * DURATION_SECONDS[unit] for unit, value in match.groupdict().items() if value))


def as_dict(element: ElementTree.Element) -> Any:
    """An XML element as NX-OS's JSON would have it: text for a leaf, and repeated children as a list."""
    children = list(element)
    if not children:
        return (element.text or "").strip()
    found: dict[str, Any] = {}
    for child in children:
        tag, value = child.tag.rsplit("}", 1)[-1], as_dict(child)
        if tag not in found:
            found[tag] = value
        elif isinstance(found[tag], list):
            found[tag].append(value)
        else:
            found[tag] = [found[tag], value]
    return found


def from_xml(output: str, device: str) -> dict[str, Any]:
    """The data in an `| xml` reply (inside its __readonly__ element), shaped as the JSON would be."""
    body = output.replace("]]>]]>", "").strip()
    if not body:
        return {}
    try:
        root = ElementTree.fromstring(body)
    except ElementTree.ParseError as error:
        raise PlatformError(f"{device}: an unreadable reply: {error}") from error
    data = next((element for element in root.iter() if element.tag.rsplit("}", 1)[-1] == "__readonly__"), None)
    found = as_dict(data) if data is not None else {}
    return found if isinstance(found, dict) else {}


def remote_port(row: dict[str, Any]) -> str | None:
    """
    The neighbor's port. Switches send the port's name; a host often sends its MAC
    address instead, and names the port in the description. NX-OS spells the port
    ID's subtype out on some releases and numbers it on others (3 is a MAC address).
    """
    port = text(row, "port_id")
    if (text(row, "port_type") or "").lower() in ("mac address", "3"):
        return text(row, "port_desc") or port
    return port


def protocol_of(owner: str) -> str:
    """The protocol that owns a route: NX-OS names a routing process's routes after its tag (ospf-1, bgp-65001)."""
    owner = owner.lower()
    if owner == "vrrp_engine":
        return "vrrp"
    return re.sub(r"-[\w.]+$", "", owner) if re.match(r"^(ospf|ospfv3|bgp|eigrp|isis|rip)-", owner) else owner


def next_hop(path: dict[str, Any], protocol: str) -> NextHop:
    """Where one of a route's paths sends a packet."""
    interface = text(path, "ifname")
    if protocol in RECEIVING_OWNERS:
        return NextHop(action="receive")
    if interface is not None and interface.lower().startswith("null"):
        return NextHop(action="drop")
    address = None if protocol == "direct" else text(path, "ipnexthop")
    return NextHop(address=address, interface=full_name(interface) if interface else None)


class NxosPlatform(Platform):
    policy = CISCO_NXOS
    management_interfaces = re.compile(r"^mgmt\d+$")

    def __init__(self, device: Device, credentials: Credentials, session: Commands | None = None) -> None:
        """The session is only for tests, which replay recorded replies through it."""
        super().__init__(device, credentials)
        self._session = session or ExecOverSsh(device, credentials)

    async def close(self) -> None:
        await self._session.close()

    async def _cli(self, command: str) -> str:
        """Run one command, and fail with what NX-OS said if it didn't run."""
        reply = await self._session.run(command)
        if reply.status != 0:
            said = " ".join(line.strip() for line in reply.output.splitlines() if line.strip() not in ("", "Cmd exec error."))
            raise PlatformError(f"{self.device.name}: {said or f'the command failed with exit status {reply.status}'}")
        return reply.output

    async def _query(self, command: str) -> dict[str, Any]:
        """
        One show command's structured reply: its JSON, or the XML the JSON is made
        from when NX-OS sends something else. Empty when the device has nothing to
        say (routes for a VRF it doesn't have).
        """
        output = (await self._cli(f"{command} | json")).strip()
        if not output:
            return {}
        try:
            reply = json.loads(output)
        except json.JSONDecodeError:
            return from_xml(await self._cli(f"{command} | xml"), self.device.name)
        return reply if isinstance(reply, dict) else {}

    async def show(self, command: str, max_chars: int = 50_000) -> CommandOutput:
        try:
            return await super().show(command, max_chars)
        except PlatformError as error:
            if "Syntax error" in str(error) or "Invalid command" in str(error):
                raise PlatformError(f"{error.args[0].rstrip()}. {CLI_HINT}") from error
            raise

    async def _run_checked_cli(self, command: str) -> str:
        return await self._cli(command)

    async def lldp_neighbors(self) -> list[LldpNeighbor]:
        reply = await self._query("show lldp neighbors detail")
        return [
            LldpNeighbor(
                device=self.device.name,
                interface=full_name(local),
                neighbor=text(row, "sys_name"),
                neighbor_interface=remote_port(row),
                neighbor_chassis_id=text(row, "chassis_id"),
            )
            for row in rows(reply, "TABLE_nbor_detail", "ROW_nbor_detail")
            if (local := text(row, "l_port_id"))
        ]

    async def interfaces(self) -> list[Interface]:
        """
        Every interface in the brief list, and every one with an address, in the VRF
        it is in. A switch port bridges in the default network instance, where
        NX-OS keeps its VLANs; the MAC table names each entry's VLAN.
        """
        brief, addressed = await asyncio.gather(self._query("show interface brief"), self._query("show ip interface vrf all"))
        routed: dict[str, Interface] = {}
        # NX-OS lists the VRFs and the interfaces in two tables of their own, in step: the nth VRF is the nth interface's.
        for vrf_table, interface_table in zip(rows(addressed, "TABLE_vrf"), rows(addressed, "TABLE_intf")):
            vrf = next((name for row in rows(vrf_table, "ROW_vrf") if (name := text(row, "vrf-name-out"))), "default")
            for row in rows(interface_table, "ROW_intf"):
                if not (name := text(row, "intf-name")):
                    continue
                addresses = [f"{text(row, 'prefix')}/{text(row, 'masklen')}"] if text(row, "prefix") else []
                addresses += [
                    f"{text(secondary, 'prefix1')}/{text(secondary, 'masklen1')}"
                    for secondary in rows(row, "TABLE_secondary_address", "ROW_secondary_address")
                    if text(secondary, "prefix1")
                ]
                routed[full_name(name)] = Interface(
                    device=self.device.name,
                    name=full_name(name),
                    network_instance=vrf,
                    mode="routed",
                    addresses=addresses,
                    oper_state=text(row, "proto-state"),
                )
        found: list[Interface] = []
        for row in rows(brief, "TABLE_interface", "ROW_interface"):
            if not (name := text(row, "interface")):
                continue
            name = full_name(name)
            aggregate = f"port-channel{channel}" if (channel := text(row, "portchan")) else None
            mode = (text(row, "portmode") or "").lower()
            if name in routed:
                found.append(routed.pop(name).model_copy(update={"aggregate": aggregate}))
            else:
                bridged = mode not in ("", "routed")
                found.append(
                    Interface(
                        device=self.device.name,
                        name=name,
                        network_instance="default",
                        mode="bridged" if bridged else "routed",
                        oper_state=text(row, "state"),
                        aggregate=aggregate,
                    )
                )
        # Some releases leave VLAN interfaces out of the brief list.
        return found + list(routed.values())

    async def interface_counters(self) -> list[InterfaceCounters]:
        reply = await self._query("show interface")
        found = []
        for row in rows(reply, "TABLE_interface", "ROW_interface"):
            name = text(row, "interface") or ""
            if not PORTS.match(name):
                continue

            def count(key: str) -> int:
                return number(row, key) or 0

            bandwidth = number(row, "eth_bw")  # In kilobits per second.
            found.append(
                InterfaceCounters(
                    device=self.device.name,
                    interface=name,
                    oper_state=text(row, "state"),
                    speed_bps=bandwidth * 1000 if bandwidth else None,
                    in_octets=count("eth_inbytes"),
                    out_octets=count("eth_outbytes"),
                    in_packets=count("eth_inpkts"),
                    out_packets=count("eth_outpkts"),
                    in_errors=count("eth_inerr"),
                    out_errors=count("eth_outerr"),
                    in_discards=count("eth_indiscard"),
                    out_discards=count("eth_outdiscard"),
                    # NX-OS says when a link last flapped, but doesn't count how often.
                    carrier_transitions=None,
                )
            )
        return found

    async def _peer_link(self) -> str | None:
        """The port-channel to the switch's vPC peer, if it has one."""
        try:
            reply = await self._query("show vpc brief")
        except PlatformError:
            return None  # No vPC here.
        return next((full_name(port) for row in rows(reply, "TABLE_peerlink", "ROW_peerlink") if (port := text(row, "peerlink-ifindex"))), None)

    async def mac_table(self) -> list[MacEntry]:
        reply, peer_link = await asyncio.gather(self._query("show mac address-table"), self._peer_link())
        found = []
        for row in rows(reply, "TABLE_mac_address", "ROW_mac_address"):
            if not (mac := text(row, "disp_mac_addr")):
                continue
            # (R) marks a routed MAC: the switch's own (on sup-eth1), or its vPC peer's (over the peer link).
            port = (text(row, "disp_port") or "").removesuffix("(R)").strip()
            if port == "vPC Peer-Link" and peer_link:
                port = peer_link  # The MAC table names the peer link by its role; the cables are its port-channel's.
            static = (text(row, "disp_is_static") or "").lower() in ("enabled", "1", "true")
            kind = "gateway" if text(row, "disp_type") == "G" else "static" if static else "dynamic"
            vlan = text(row, "disp_vlan")
            found.append(
                MacEntry(
                    device=self.device.name,
                    network_instance="default",
                    mac=mac,
                    interface=full_name(port),
                    entry_type=kind,
                    vlan=int(vlan) if vlan and vlan.isdigit() else None,
                )
            )
        return found

    async def arp_table(self) -> list[ArpEntry]:
        reply = await self._query("show ip arp vrf all")
        found = []
        for vrf in rows(reply, "TABLE_vrf", "ROW_vrf"):
            for row in rows(vrf, "TABLE_adj", "ROW_adj"):
                # An incomplete entry is an address that hasn't answered ARP: no binding yet.
                if "incomplete" in row or not (mac := text(row, "mac")) or not (address := text(row, "ip-addr-out")):
                    continue
                found.append(
                    ArpEntry(
                        device=self.device.name,
                        interface=full_name(text(row, "intf-out") or ""),
                        ip_address=address,
                        mac=mac,
                        # + marks a binding the vPC peer learned and synced to this switch.
                        origin="synced" if "+" in (text(row, "flags") or "") else "dynamic",
                    )
                )
        return found

    async def routes(self, network_instance: str) -> list[Route]:
        if not NETWORK_INSTANCE_NAME.match(network_instance):
            raise PlatformError(f"'{network_instance}' is not a valid network instance name.")
        reply = await self._query(f"show ip route vrf {network_instance}")
        found = []
        for vrf in rows(reply, "TABLE_vrf", "ROW_vrf"):
            for family in rows(vrf, "TABLE_addrf", "ROW_addrf"):
                for prefix in rows(family, "TABLE_prefix", "ROW_prefix"):
                    owners: dict[str, list[dict[str, Any]]] = {}
                    for path in rows(prefix, "TABLE_path", "ROW_path"):
                        owners.setdefault(protocol_of(text(path, "clientname") or "unknown"), []).append(path)
                    for protocol, paths in owners.items():
                        hops: list[NextHop] = []
                        for path in paths:
                            if (hop := next_hop(path, protocol)) not in hops:
                                hops.append(hop)
                        found.append(
                            Route(
                                device=self.device.name,
                                network_instance=network_instance,
                                prefix=text(prefix, "ipprefix") or "",
                                protocol=protocol,
                                next_hops=hops,
                                active=any(text(path, "ubest") == "true" for path in paths),
                                metric=number(paths[0], "metric"),
                                preference=number(paths[0], "pref"),
                            )
                        )
        return found

    async def ospf_interfaces(self) -> list[OspfInterface]:
        interfaces, processes = await asyncio.gather(
            self._query("show ip ospf interface vrf all"), self._query("show ip ospf vrf all")
        )
        router_ids: dict[tuple[str | None, str | None], str | None] = {}
        areas: dict[tuple[str | None, str | None, str | None], dict[str, Any]] = {}
        for context in rows(processes, "TABLE_ctx", "ROW_ctx"):
            key = (text(context, "ptag"), text(context, "cname"))
            router_ids[key] = text(context, "rid")
            for area in rows(context, "TABLE_area", "ROW_area"):
                areas[(*key, text(area, "aname"))] = area
        found = []
        for context in rows(interfaces, "TABLE_ctx", "ROW_ctx"):
            key = (text(context, "ptag"), text(context, "cname"))
            for row in rows(context, "TABLE_intf", "ROW_intf"):
                if not (name := text(row, "ifname")) or not (area_id := text(row, "area")):
                    continue
                area = areas.get((*key, area_id), {})
                area_type = "nssa" if text(area, "nssa") == "true" else "stub" if text(area, "stub") == "true" else "normal"
                address = text(row, "addr")
                found.append(
                    OspfInterface(
                        device=self.device.name,
                        network_instance=key[1] or "default",
                        router_id=router_ids.get(key),
                        area=area_id,
                        interface=full_name(name),
                        address=f"{address}/{text(row, 'masklen')}" if address else None,
                        network_type=NETWORK_TYPES.get((text(row, "type_str") or "").lower()),
                        state=OSPF_STATES.get((text(row, "state_str") or "").lower(), (text(row, "state_str") or "").lower() or None),
                        area_type=area_type if area else None,
                        passive=text(row, "passive") == "true",
                        # A passive interface reports no authentication of its own; its area's applies.
                        authentication=(text(row, "auth_type") or text(area, "auth_type") or "").lower() or None,
                        keychain=text(row, "auth_keychain"),
                        cost=number(row, "cost"),
                        hello_interval=number(row, "hello_interval"),
                        dead_interval=number(row, "dead_interval"),
                        neighbors=number(row, "nbr_total"),
                        designated_router=text(row, "dr_rid"),
                        backup_designated_router=text(row, "bdr_rid"),
                    )
                )
        return found

    async def ospf_neighbors(self) -> list[OspfNeighbor]:
        reply = await self._query("show ip ospf neighbors detail vrf all")
        found = []
        for context in rows(reply, "TABLE_ctx", "ROW_ctx"):
            for row in rows(context, "TABLE_nbr", "ROW_nbr"):
                if not (router_id := text(row, "rid")):
                    continue
                state = (text(row, "state") or "unknown").lower()
                state = NEIGHBOR_STATES.get(state, state)
                found.append(
                    OspfNeighbor(
                        device=self.device.name,
                        network_instance=text(context, "cname") or "default",
                        area=text(row, "area") or "",
                        interface=full_name(text(row, "intf") or ""),
                        router_id=router_id,
                        address=text(row, "addr"),
                        state=state,
                        priority=number(row, "priority"),
                        dead_seconds=seconds_of(text(row, "deadtimer")),
                        # The time since its state last changed, which for a full adjacency is how long it has been up.
                        uptime_seconds=seconds_of(text(row, "lastchange")) if state == "full" else None,
                        state_changes=number(row, "transition"),
                    )
                )
        return found

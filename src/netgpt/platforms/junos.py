"""
The Junos platform module talks to Juniper Junos over NETCONF, carried by SSH.

Every Junos operational command has an RPC that returns XML (`show ... | display
xml rpc` names it), so most queries need no screen scraping. CLI commands go
through the `<command>` RPC after the read-only check. The session speaks NETCONF
1.0, whose messages end with `]]>]]>`, and opens at the first RPC.
"""

from __future__ import annotations

import asyncio
import ipaddress
import logging
import re
import xml.etree.ElementTree as ElementTree
from typing import Protocol
from xml.sax.saxutils import escape

import asyncssh

from netgpt.guard import JUNIPER_JUNOS
from netgpt.inventory import Credentials, Device, InventoryError
from netgpt.platforms.base import Platform, PlatformError
from netgpt.primitives import (
    ArpEntry,
    BgpSession,
    CommandOutput,
    Interface,
    InterfaceCounters,
    LldpNeighbor,
    MacEntry,
    NextHop,
    Route,
)

# asyncssh logs every connection at INFO, and NetGPT opens one per query.
logging.getLogger("asyncssh").setLevel(logging.WARNING)

END_OF_MESSAGE = "]]>]]>"
HELLO = (
    '<hello xmlns="urn:ietf:params:xml:ns:netconf:base:1.0"><capabilities>'
    "<capability>urn:ietf:params:netconf:base:1.0</capability></capabilities></hello>"
)
# Seconds to wait for a login, or for one reply. A whole switch's interfaces at the extensive level take a few.
TIMEOUT = 60.0

NETWORK_INSTANCE_NAME = re.compile(r"^[A-Za-z0-9_.-]{1,64}$")
# Junos calls the main routing instance master; NetGPT calls it default on every platform, and accepts either.
MAIN_INSTANCE = {"default", "master"}
# Routing instances Junos keeps for itself, and interfaces it uses internally (to its packet engines,
# services, and label switching). Neither is part of the network anyone configured.
PRIVATE_INSTANCE = re.compile(r"^__.*__$")
INTERNAL_INTERFACES = re.compile(r"^(bme|cbp|dsc|esi|fti|gre|ipip|jsrv|lsi|mtun|pfe-|pfh-|pimd|pime|pip|pp\d|rbeb|tap|vtep)")
# The ports whose counters are polled: Ethernet ports and aggregates.
PORTS = re.compile(r"^((ge|xe|et|mge)-\d+/\d+/\d+|ae\d+)$")
SPEED = re.compile(r"^(\d+(?:\.\d+)?)\s*([kmgt])bps$", re.IGNORECASE)
SPEED_UNITS = {"k": 10**3, "m": 10**6, "g": 10**9, "t": 10**12}
PORT_SUFFIX = re.compile(r"\+\d+$")
# The logical unit Junos gives a port nobody configured.
UNCONFIGURED_UNIT = ".16386"

# What Junos's next-hop types do with a packet; any other type forwards it.
NEXT_HOP_ACTIONS = {"local": "receive", "receive": "receive", "multirecv": "receive", "reject": "drop", "discard": "drop"}

# Added to the error when Junos can't parse a command. Models reach for Cisco's commands; working examples
# and the typed tools stop the guessing.
CLI_HINT = (
    "This device runs Juniper Junos, whose commands differ from Cisco's. For example: show interfaces terse, "
    "show route table inet.0, show route table inet6.0, show route table <instance>.inet.0, show bgp summary, "
    "show bgp neighbor, show isis adjacency, show ospf neighbor instance all, show ethernet-switching table, "
    "show arp no-resolve, show lldp neighbors. The typed tools (interfaces, arp_table, mac_table, routes, "
    "bgp_sessions, lldp_neighbors) return the same records from every platform."
)


class Session(Protocol):
    """A NETCONF session: send one RPC's body, get back the reply's XML."""

    async def rpc(self, body: str) -> str: ...

    async def close(self) -> None: ...


class NetconfOverSsh:
    """A NETCONF 1.0 session over SSH, opened at the first RPC, one RPC at a time."""

    def __init__(self, device: Device, credentials: Credentials) -> None:
        self._device = device
        self._credentials = credentials
        self._connection: asyncssh.SSHClientConnection | None = None
        self._writer: asyncssh.SSHWriter[str] | None = None
        self._reader: asyncssh.SSHReader[str] | None = None
        self._lock = asyncio.Lock()
        self._message_id = 0

    async def _open(self) -> None:
        device, credentials = self._device, self._credentials
        known_hosts = device.known_hosts.expanduser() if device.known_hosts else None
        if known_hosts is not None and not known_hosts.is_file():
            raise InventoryError(f"Known hosts file {known_hosts} for {device.name} does not exist.")
        if credentials.ssh_key_file is not None:
            secret = {"client_keys": [str(credentials.ssh_key())], "password": None}
        else:
            secret = {"client_keys": None, "password": credentials.password()}
        try:
            # No ~/.ssh/config and no agent: the inventory says how to log in, and nothing else does.
            self._connection = await asyncio.wait_for(
                asyncssh.connect(
                    device.host,
                    username=credentials.username,
                    known_hosts=str(known_hosts) if known_hosts else (),
                    config=None,
                    agent_path=None,
                    **secret,
                ),
                TIMEOUT,
            )
            self._writer, self._reader, _ = await self._connection.open_session(subsystem="netconf", encoding="utf-8")
            self._writer.write(HELLO + END_OF_MESSAGE)
            await asyncio.wait_for(self._reader.readuntil(END_OF_MESSAGE), TIMEOUT)
        except asyncssh.PermissionDenied:
            await self.close()
            raise PlatformError(f"{device.name}: the device refused the SSH login for {credentials.username}.") from None
        except asyncssh.HostKeyNotVerifiable:
            await self.close()
            where = known_hosts or "~/.ssh/known_hosts"
            raise PlatformError(f"{device.name}: its SSH host key isn't the one in {where}, so NetGPT won't log in.") from None
        except (OSError, asyncssh.Error, asyncio.IncompleteReadError, TimeoutError) as error:
            await self.close()
            raise PlatformError(f"{device.name}: {type(error).__name__}: {error}") from error

    async def rpc(self, body: str) -> str:
        async with self._lock:
            if self._connection is None:
                await self._open()
            assert self._writer is not None and self._reader is not None
            self._message_id += 1
            try:
                self._writer.write(f'<rpc message-id="{self._message_id}">{body}</rpc>{END_OF_MESSAGE}')
                reply = await asyncio.wait_for(self._reader.readuntil(END_OF_MESSAGE), TIMEOUT)
            except (OSError, asyncssh.Error, asyncio.IncompleteReadError, TimeoutError) as error:
                # A reply cut off partway leaves the session out of step, so the next RPC starts a new one.
                await self.close()
                raise PlatformError(f"{self._device.name}: {type(error).__name__}: {error}") from error
            return reply[: -len(END_OF_MESSAGE)]

    async def close(self) -> None:
        connection, self._connection, self._writer, self._reader = self._connection, None, None, None
        if connection is not None:
            connection.close()
            await connection.wait_closed()


def text_of(element: ElementTree.Element | None, path: str) -> str | None:
    """The stripped text at a path below an element, or None when it is absent or empty."""
    found = element.find(path) if element is not None else None
    text = (found.text or "").strip() if found is not None else ""
    return text or None


def int_of(element: ElementTree.Element | None, path: str) -> int | None:
    text = text_of(element, path)
    return int(text) if text is not None and text.isdigit() else None


def instance_name(name: str | None) -> str:
    """The network instance, named as NetGPT names it on every platform."""
    return "default" if name in (None, "master") else name


def filter_output(output: str, pipe: str) -> str:
    """
    Apply one Junos output filter, as the CLI would. Junos's <command> RPC ignores
    pipes without saying so, so the driver filters what comes back itself.
    """
    verb, _, argument = pipe.strip().partition(" ")
    argument = argument.strip().strip('"').strip("'")
    lines = output.splitlines()
    if verb in ("match", "except", "find"):
        try:
            pattern = re.compile(argument, re.IGNORECASE)
        except re.error:
            pattern = re.compile(re.escape(argument), re.IGNORECASE)
        if verb == "find":
            start = next((index for index, line in enumerate(lines) if pattern.search(line)), len(lines))
            return "\n".join(lines[start:])
        return "\n".join(line for line in lines if bool(pattern.search(line)) == (verb == "match"))
    if verb == "count":
        return f"Count: {len([line for line in lines if line.strip()])} lines"
    if verb == "last":
        return "\n".join(lines[-int(argument) :] if argument.isdigit() and int(argument) > 0 else lines)
    if verb == "trim":
        return "\n".join(line[int(argument) :] for line in lines) if argument.isdigit() else output
    return output  # no-more: there is no pager here to turn off.


def with_prefix(address: str | None) -> str | None:
    """
    An interface address with its prefix length. Junos leaves the length off a
    loopback's address, which is a single host. Link-local addresses come back
    as None: every IPv6 interface has one, and they name no network.
    """
    try:
        interface = ipaddress.ip_interface(address or "")
    except ValueError:
        return None
    return None if interface.is_link_local else str(interface)


def port_speed(value: str | None) -> int | None:
    """Read a port speed such as 10Gbps or 1000mbps as bits per second."""
    match = SPEED.match(value or "")
    return int(float(match.group(1)) * SPEED_UNITS[match.group(2).lower()]) if match else None


def remote_port(neighbor: ElementTree.Element) -> str | None:
    """
    The neighbor's port. Junos and most switches send the port's name; a host often
    sends its MAC address instead, and names the port in the description.
    """
    port = text_of(neighbor, "lldp-remote-port-id")
    if (text_of(neighbor, "lldp-remote-port-id-subtype") or "").lower().startswith("mac"):
        return text_of(neighbor, "lldp-remote-port-description") or port
    return port


def arrival_tables(peer: ElementTree.Element) -> list[ElementTree.Element]:
    """
    The tables a session's routes arrive in: the main ones (inet.0), the bgp.* ones
    that VPN families arrive in, and the session's own instance's. Junos lists each
    VPN route again in every instance table that imports it (Global.evpn.0), and
    counting those too would count it twice.
    """
    instance = text_of(peer, "peer-cfg-rti") or "master"
    found = []
    for rib in peer.findall("bgp-rib"):
        parts = (text_of(rib, "name") or "").split(".")
        if len(parts) == 2 or parts[0] in ("bgp", instance):
            found.append(rib)
    return found


def next_hops(entry: ElementTree.Element, protocol: str) -> list[NextHop]:
    """
    Read one route's next hops. A route for the device's own address, or one that
    receives or rejects, says so in its next-hop type. Where Junos selected some of
    several next hops, only those forward.
    """
    action = NEXT_HOP_ACTIONS.get((text_of(entry, "nh-type") or "").lower())
    if action is not None:
        return [NextHop(action=action)]
    hops = entry.findall("nh")
    selected = [hop for hop in hops if hop.find("selected-next-hop") is not None]
    return [
        NextHop(
            address=None if protocol in ("direct", "local") else text_of(hop, "to"),
            interface=text_of(hop, "via") or text_of(hop, "nh-local-interface"),
        )
        for hop in selected or hops
    ]


class JunosPlatform(Platform):
    policy = JUNIPER_JUNOS
    management_interfaces = re.compile(r"^(em|fxp|me)\d+$|^vme$")

    def __init__(self, device: Device, credentials: Credentials, session: Session | None = None) -> None:
        """The session is only for tests, which replay recorded replies through it."""
        super().__init__(device, credentials)
        self._session = session or NetconfOverSsh(device, credentials)

    async def close(self) -> None:
        await self._session.close()

    async def _rpc(self, body: str) -> ElementTree.Element:
        """Make one RPC and return the reply with namespaces removed. Errors fail it; warnings don't."""
        text = await self._session.rpc(body)
        try:
            reply = ElementTree.fromstring(text.strip())
        except ElementTree.ParseError as error:
            raise PlatformError(f"{self.device.name}: an unreadable NETCONF reply: {error}") from error
        for element in reply.iter():
            element.tag = element.tag.rsplit("}", 1)[-1]
        failures = [error for error in reply.iter("rpc-error") if text_of(error, "error-severity") != "warning"]
        if failures:
            messages = []
            for failure in failures:
                message = text_of(failure, "error-message") or "an error with no message"
                bad = text_of(failure, "error-info/bad-element")
                messages.append(f"{message}: {bad}" if bad else message)
            raise PlatformError(f"{self.device.name}: {'; '.join(messages)}")
        return reply

    async def show(self, command: str, max_chars: int = 50_000) -> CommandOutput:
        try:
            return await super().show(command, max_chars)
        except PlatformError as error:
            if "syntax error" in str(error):
                raise PlatformError(f"{error.args[0].rstrip()}. {CLI_HINT}") from error
            raise

    async def _run_checked_cli(self, command: str) -> str:
        head, *pipes = command.split("|")
        displays = [pipe.split()[1:] for pipe in pipes if pipe.split()[0] == "display"]
        if any(display not in (["xml"], ["json"]) for display in displays):
            raise PlatformError(f"{self.device.name}: of the display options, only | display xml and | display json work here.")
        output_format = displays[-1][0] if displays else "text"
        reply = await self._rpc(f'<command format="{output_format}">{escape(head.strip())}</command>')
        if output_format == "xml":
            output = "".join(ElementTree.tostring(child, encoding="unicode") for child in reply)
        else:
            found = reply.find(".//output")
            output = found.text or "" if found is not None else "".join(reply.itertext())
        if not output.strip() and not len(reply):
            # Junos answers some commands it doesn't have (show ip route) with nothing at all, not an error.
            raise PlatformError(f"{self.device.name}: the device returned nothing for '{head.strip()}'. {CLI_HINT}")
        for pipe in pipes:
            if pipe.split()[0] != "display":
                output = filter_output(output, pipe)
        return output

    async def lldp_neighbors(self) -> list[LldpNeighbor]:
        # The summary names each neighbor's system but not its port, which only each port's detail gives.
        summary = await self._rpc("<get-lldp-neighbors-information/>")
        ports = dict.fromkeys(
            port
            for neighbor in summary.iter("lldp-neighbor-information")
            if (port := text_of(neighbor, "lldp-local-port-id") or text_of(neighbor, "lldp-local-interface"))
        )
        found = []
        for port in ports:
            detail = await self._rpc(
                f"<get-lldp-interface-neighbors><interface-device>{escape(port)}</interface-device></get-lldp-interface-neighbors>"
            )
            found += [
                LldpNeighbor(
                    device=self.device.name,
                    interface=text_of(neighbor, "lldp-local-interface") or port,
                    neighbor=text_of(neighbor, "lldp-remote-system-name"),
                    neighbor_interface=remote_port(neighbor),
                    neighbor_chassis_id=text_of(neighbor, "lldp-remote-chassis-id"),
                )
                for neighbor in detail.iter("lldp-neighbor-information")
            ]
        return found

    async def _instances(self) -> dict[str, str]:
        """
        Which routing instance each interface is in. The main instance lists no
        interfaces: an interface no other instance lists is in it.
        """
        instances = await self._rpc("<get-instance-information><detail/></get-instance-information>")
        return {
            interface: text_of(instance, "instance-name") or ""
            for instance in instances.iter("instance-core")
            for member in instance.iter("instance-interface")
            if (interface := text_of(member, "interface-name"))
        }

    @staticmethod
    def _internal(interface: str, instance: str | None) -> bool:
        """Whether an interface is one Junos uses for itself, or a placeholder for a port nobody configured."""
        return bool(
            INTERNAL_INTERFACES.match(interface)
            or PRIVATE_INSTANCE.match(instance or "")
            or interface.endswith(UNCONFIGURED_UNIT)
        )

    async def interfaces(self) -> list[Interface]:
        terse = await self._rpc("<get-interface-information><terse/></get-interface-information>")
        instance_of = await self._instances()
        found = []
        for logical in terse.iter("logical-interface"):
            name = text_of(logical, "name")
            instance = instance_of.get(name or "")
            if not name or self._internal(name, instance):
                continue
            families = {text_of(family, "address-family-name"): family for family in logical.findall("address-family")}
            bridged = bool({"eth-switch", "ethernet-switching", "bridge"} & set(families))
            addresses = [
                address
                for family in ("inet", "inet6")
                for entry in (families[family].findall("interface-address") if family in families else [])
                if (address := with_prefix(text_of(entry, "ifa-local")))
            ]
            found.append(
                Interface(
                    device=self.device.name,
                    name=name,
                    network_instance=(instance or "default-switch") if bridged else instance_name(instance),
                    mode="bridged" if bridged else "routed",
                    addresses=addresses,
                    oper_state=text_of(logical, "oper-status"),
                )
            )
        return found

    async def interface_counters(self) -> list[InterfaceCounters]:
        # A whole switch at the extensive level (the only one with error counters) is most of a megabyte,
        # so ask for the ports that are up, one at a time. A port that is down counts nothing new.
        terse = await self._rpc("<get-interface-information><terse/></get-interface-information>")
        ports = [
            name
            for physical in terse.iter("physical-interface")
            if PORTS.match(name := text_of(physical, "name") or "") and text_of(physical, "oper-status") == "up"
        ]
        found = []
        for port in ports:
            reply = await self._rpc(
                f"<get-interface-information><extensive/><interface-name>{escape(port)}</interface-name></get-interface-information>"
            )
            physical = reply.find(".//physical-interface")
            traffic = physical.find("traffic-statistics") if physical is not None else None
            if traffic is None:
                continue

            def count(path: str, within: ElementTree.Element | None = physical) -> int:
                return int_of(within, path) or 0

            found.append(
                InterfaceCounters(
                    device=self.device.name,
                    interface=port,
                    oper_state=text_of(physical, "oper-status"),
                    speed_bps=port_speed(text_of(physical, "speed")),
                    in_octets=count("input-bytes", traffic),
                    out_octets=count("output-bytes", traffic),
                    in_packets=count("input-packets", traffic),
                    out_packets=count("output-packets", traffic),
                    in_errors=count("input-error-list/input-errors"),
                    out_errors=count("output-error-list/output-errors"),
                    in_discards=count("input-error-list/input-drops"),
                    out_discards=count("output-error-list/output-drops"),
                    carrier_transitions=int_of(physical, "output-error-list/carrier-transitions"),
                )
            )
        return found

    async def mac_table(self) -> list[MacEntry]:
        reply = await self._rpc("<get-ethernet-switching-table-information/>")
        found = []
        for vlan in reply.iter("l2ng-l2ald-mac-entry-vlan"):
            instance = text_of(vlan, "l2ng-l2-mac-routing-instance") or "default-switch"
            for entry in vlan.iter("l2ng-mac-entry"):
                flags = set((text_of(entry, "l2ng-l2-mac-flags") or "").replace(" ", "").split(","))
                kind = "static" if flags & {"S", "P"} else "remote" if "R" in flags else "dynamic"
                found.append(
                    MacEntry(
                        device=self.device.name,
                        network_instance=instance,
                        mac=text_of(entry, "l2ng-l2-mac-address") or "",
                        interface=text_of(entry, "l2ng-l2-mac-logical-interface") or "",
                        entry_type=kind,
                    )
                )
        return found

    async def arp_table(self) -> list[ArpEntry]:
        reply = await self._rpc("<get-arp-table-information><no-resolve/></get-arp-table-information>")
        instance_of = await self._instances()
        found = []
        for entry in reply.iter("arp-table-entry"):
            # An IRB's entry names the port behind it too: "irb.10 [ge-0/0/22.0]".
            interface = (text_of(entry, "interface-name") or "").split(" ")[0]
            if self._internal(interface, instance_of.get(interface)):
                continue
            flags = entry.find("arp-table-entry-flags")
            permanent = flags is not None and flags.find("permanent") is not None
            found.append(
                ArpEntry(
                    device=self.device.name,
                    interface=interface,
                    ip_address=text_of(entry, "ip-address") or "",
                    mac=text_of(entry, "mac-address"),
                    origin="static" if permanent else "dynamic",
                )
            )
        return found

    async def routes(self, network_instance: str) -> list[Route]:
        if not NETWORK_INSTANCE_NAME.match(network_instance):
            raise PlatformError(f"'{network_instance}' is not a valid network instance name.")
        # Each instance keeps IPv4 and IPv6 in tables of their own. One it lacks comes back empty.
        prefix = "" if network_instance in MAIN_INSTANCE else f"{network_instance}."
        found = []
        for table in (f"{prefix}inet.0", f"{prefix}inet6.0"):
            reply = await self._rpc(f"<get-route-information><table>{escape(table)}</table></get-route-information>")
            for destination in reply.iter("rt"):
                for entry in destination.findall("rt-entry"):
                    protocol = (text_of(entry, "protocol-name") or "unknown").lower()
                    found.append(
                        Route(
                            device=self.device.name,
                            network_instance=network_instance,
                            prefix=text_of(destination, "rt-destination") or "",
                            protocol=protocol,
                            next_hops=next_hops(entry, protocol),
                            active=entry.find("current-active") is not None or text_of(entry, "active-tag") == "*",
                            metric=int_of(entry, "metric"),
                            preference=int_of(entry, "preference"),
                        )
                    )
        return found

    async def bgp_sessions(self) -> list[BgpSession]:
        reply = await self._rpc("<get-bgp-neighbor-information/>")
        found = []
        for peer in reply.iter("bgp-peer"):
            # An established session's addresses carry the TCP port: fd00::1+179.
            local = text_of(peer, "local-address") or text_of(peer, "bgp-option-information/local-address")
            ribs = arrival_tables(peer)
            found.append(
                BgpSession(
                    device=self.device.name,
                    network_instance=instance_name(text_of(peer, "peer-cfg-rti")),
                    peer_address=PORT_SUFFIX.sub("", text_of(peer, "peer-address") or ""),
                    peer_as=int_of(peer, "peer-as"),
                    local_address=PORT_SUFFIX.sub("", local) if local else None,
                    local_as=int_of(peer, "local-as"),
                    group=text_of(peer, "peer-group"),
                    state=text_of(peer, "peer-state") or "unknown",
                    last_error=text_of(peer, "last-error"),
                    flags=(text_of(peer, "peer-flags") or "").split(),
                    families=(text_of(peer, "bgp-option-information/address-families") or "").split(),
                    flaps=int_of(peer, "flap-count"),
                    received_prefixes=sum(int_of(rib, "received-prefix-count") or 0 for rib in ribs) if ribs else None,
                    active_prefixes=sum(int_of(rib, "active-prefix-count") or 0 for rib in ribs) if ribs else None,
                )
            )
        return found

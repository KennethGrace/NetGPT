"""
The filters module narrows a table to the rows a question is about. On a real
network one table from every device can hold thousands of rows, more than a small
model can read, so the tables take filters as an engineer writes them, and as
models do: any part of a MAC address in any spelling (0200.c0ff.ee01,
02:00:C0:FF:EE:01, or just c0ff), a port by its name or an abbreviation of it
(Eth1/9, Po2, Gi1/0/51), and an address or a network (192.0.2.5, 192.0.2.0/24,
or 192.0.2.0 255.255.255.0).

A filter left out, or given no values but blank ones, matches every row. Given
several values, a filter matches a row that any of them matches; given several
filters, a row must match each.
"""

from __future__ import annotations

import ipaddress
import re
from collections import defaultdict
from collections.abc import Iterable

from netgpt.primitives import Route
from netgpt.topology import PORT_NAME, mac_key, port_of

Network = ipaddress.IPv4Network | ipaddress.IPv6Network

# The characters a MAC address is written with, in any platform's spelling.
MAC_SPELLING = re.compile(r"^[0-9A-Fa-f.:\-\s]+$")


class FilterError(ValueError):
    """A filter's value isn't one it can match by, such as a MAC address with a letter that isn't hex."""


def _given(values: Iterable[str] | None) -> list[str]:
    return [value.strip() for value in values or () if value and value.strip()]


def mac_parts(given: Iterable[str] | None) -> list[str] | None:
    """Parts of MAC addresses as their hex digits alone, or FilterError for one that isn't."""
    parts = []
    for value in _given(given):
        digits = mac_key(value)
        if not MAC_SPELLING.match(value.strip()) or len(digits) < 2:
            raise FilterError(
                f"{value!r} isn't part of a MAC address. Give its hex digits in any spelling: 0200.c0ff.ee01, "
                "02:00:c0:ff:ee:01, or a part of one, such as c0ff."
            )
        parts.append(digits)
    return parts or None


def networks(given: Iterable[str] | None) -> list[Network] | None:
    """Addresses and networks as networks (an address is its own /32), or FilterError for one that is neither."""
    found = []
    for value in _given(given):
        try:
            # A mask may follow the address after a space, as Cisco writes it: 192.0.2.0 255.255.255.0.
            found.append(ipaddress.ip_network("/".join(value.split()), strict=False))
        except ValueError:
            raise FilterError(f"{value!r} isn't an IP address or network, such as 192.0.2.5 or 192.0.2.0/24.") from None
    return found or None


def ports(given: Iterable[str] | None) -> list[str] | None:
    """Port names as given, without the spaces some models put in them (Ethernet 1/9)."""
    return [re.sub(r"\s+", "", value) for value in _given(given)] or None


def _names(wanted: str, interface: str) -> bool:
    """Whether an interface's name is the port wanted, spelled out or abbreviated: Eth1/9 is Ethernet1/9."""
    if wanted.lower() == interface.lower():
        return True
    short, full = PORT_NAME.match(wanted), PORT_NAME.match(interface)
    return (
        short is not None and full is not None and short["number"] == full["number"]
        and full["word"].lower().startswith(short["word"].lower())
    )


def on_port(wanted: list[str] | None, interface: str | None) -> bool:
    """Whether an interface is one of the ports wanted, or a subinterface of one."""
    if wanted is None:
        return True
    if interface is None:
        return False
    return any(_names(name, interface) or _names(name, port_of(interface)) for name in wanted)


def has_mac(wanted: list[str] | None, mac: str | None) -> bool:
    """Whether a MAC address holds any of the parts wanted (from mac_parts)."""
    if wanted is None:
        return True
    key = mac_key(mac)
    return bool(key) and any(part in key for part in wanted)


def _address(value: str) -> ipaddress.IPv4Address | ipaddress.IPv6Address | None:
    try:
        return ipaddress.ip_address(value.split("/")[0])
    except ValueError:
        return None


def in_networks(wanted: list[Network] | None, address: str | None) -> bool:
    """Whether an address lies in any of the networks wanted (from networks)."""
    if wanted is None:
        return True
    found = _address(address or "")
    return found is not None and any(found.version == network.version and found in network for network in wanted)


def on_networks(wanted: list[Network] | None, addresses: Iterable[str]) -> bool:
    """
    Whether an interface with these addresses (with their prefix lengths) is on any
    of the networks wanted: its network holds the address wanted, or lies in the network.
    """
    if wanted is None:
        return True
    for value in addresses:
        try:
            own = ipaddress.ip_interface(value).network
        except ValueError:
            continue
        if any(own.version == network.version and own.overlaps(network) for network in wanted):
            return True
    return False


def of_protocol(wanted: list[str] | None, protocol: str) -> bool:
    """Whether a route's protocol is one wanted, as the platform spells it or by its start: ospf for ospfv2."""
    names = [name.lower() for name in _given(wanted)]
    return not names or any(protocol.lower().startswith(name) for name in names)


def forwarding(routes: Iterable[Route], wanted: list[Network] | None) -> list[Route]:
    """
    Each device's routes for the destinations wanted: every route inside a
    destination, and the longest route that holds it, the one the device forwards
    it by (a default route, if nothing more specific does). Every route has the
    destination's prefix, whichever protocol it is from, so which one is active is
    the routes' own to say.
    """
    routes = list(routes)
    if wanted is None:
        return routes
    by_device: dict[str, list[tuple[Network, Route]]] = defaultdict(list)
    for route in routes:
        try:
            by_device[route.device].append((ipaddress.ip_network(route.prefix, strict=False), route))
        except ValueError:
            continue
    chosen: set[int] = set()
    for table in by_device.values():
        for destination in wanted:
            same = [(prefix, route) for prefix, route in table if prefix.version == destination.version]
            chosen |= {id(route) for prefix, route in same if prefix.subnet_of(destination)}
            holding = [prefix.prefixlen for prefix, _ in same if destination.subnet_of(prefix)]
            if holding:
                longest = max(holding)
                chosen |= {id(route) for prefix, route in same if prefix.prefixlen == longest and destination.subnet_of(prefix)}
    return [route for route in routes if id(route) in chosen]

"""
The primitives module defines the typed network state that NetGPT's tools return.

The primitives are grouped by the layer of the network that produces them, the
same split as the xspace protobuf stubs (hardware, ethernet, internet). Every
primitive records the device that reported it, so results gathered from many
devices can be merged into one picture of the network.
"""

from __future__ import annotations

from typing import Generic, Literal, TypeVar

from pydantic import BaseModel, Field

PrimitiveT = TypeVar("PrimitiveT", bound=BaseModel)


class DeviceInfo(BaseModel):
    """A device in the inventory, as the model is allowed to see it (no credentials)."""

    name: str
    platform: str
    host: str
    role: str | None = None


class DeviceError(BaseModel):
    """A failure on one device during a multi-device query."""

    device: str
    error: str


class Collection(BaseModel, Generic[PrimitiveT]):
    """
    The result of asking several devices the same question. Devices that answered
    contribute items; devices that failed contribute an error, and the rest of the
    collection is still returned.
    """

    items: list[PrimitiveT] = Field(default_factory=list)
    errors: list[DeviceError] = Field(default_factory=list)


class CommandOutput(BaseModel):
    """The raw text output of a read-only CLI command."""

    device: str
    command: str
    output: str
    truncated: bool = False


# Hardware: physical adjacency.


class LldpNeighbor(BaseModel):
    """One LLDP adjacency: a local interface and the remote system and port it sees."""

    device: str
    interface: str
    neighbor: str | None = Field(None, description="The remote system name.")
    neighbor_interface: str | None = Field(None, description="The remote port ID.")
    neighbor_chassis_id: str | None = None


class Interface(BaseModel):
    """
    One logical interface (a subinterface on most platforms): the network instance it
    belongs to, whether it routes or bridges, and its addresses.
    """

    device: str
    name: str = Field(description="For example ethernet-1/1.0 or GigabitEthernet0/1.")
    network_instance: str | None = Field(None, description="The VRF, or the layer 2 network instance (bridge), it belongs to.")
    mode: Literal["routed", "bridged"] = Field(
        description="routed: it has addresses and routes packets. bridged: it switches frames in a layer 2 network instance."
    )
    addresses: list[str] = Field(
        default_factory=list,
        description="IPv4 and IPv6 addresses with prefix length, such as 192.168.12.1/24 or fd00::1/128. Link-local addresses are left out.",
    )
    oper_state: str | None = Field(None, description="For example up or down.")


class InterfaceCounters(BaseModel):
    """
    One port's counters, as the device reported them at one moment. They only grow,
    until the device restarts or someone clears them, so rates come from the
    difference between two readings.
    """

    device: str
    interface: str = Field(description="The port, for example ethernet-1/1 or GigabitEthernet0/1.")
    oper_state: str | None = None
    speed_bps: int | None = Field(None, description="The port's speed in bits per second, which utilization is a share of.")
    in_octets: int
    out_octets: int
    in_packets: int
    out_packets: int
    in_errors: int
    out_errors: int
    in_discards: int
    out_discards: int
    carrier_transitions: int | None = Field(None, description="How many times the link has gone up or down.")


class InterfaceTraffic(BaseModel):
    """
    One port's traffic and errors over the last minutes, worked out from the counters
    NetGPT polls: the latest rates, the busiest interval's, and what was counted.
    """

    device: str
    interface: str
    oper_state: str | None = None
    speed: str | None = Field(None, description="The port's speed, for example 25G.")
    minutes: float = Field(description="How far back the figures go: less than asked for if polling began more recently.")
    in_mbps: float = Field(description="Megabits per second received, between the latest two polls.")
    out_mbps: float = Field(description="Megabits per second sent, between the latest two polls.")
    in_percent: float | None = Field(None, description="in_mbps as a percent of the port's speed.")
    out_percent: float | None = Field(None, description="out_mbps as a percent of the port's speed.")
    peak_in_mbps: float = Field(description="The highest in_mbps between any two polls in the window.")
    peak_out_mbps: float = Field(description="The highest out_mbps between any two polls in the window.")
    in_errors: int = Field(description="Packets received with errors, over the window.")
    out_errors: int = Field(description="Packets that failed to send, over the window.")
    in_discards: int = Field(description="Received packets dropped without an error, for example for lack of buffer, over the window.")
    out_discards: int = Field(description="Packets dropped instead of sent, over the window.")
    flaps: int | None = Field(None, description="How many times the link went up or down, over the window.")


# Ethernet: layer 2 forwarding.


class MacEntry(BaseModel):
    """One entry in a layer 2 forwarding database (MAC table)."""

    device: str
    network_instance: str
    mac: str
    interface: str
    entry_type: str | None = Field(None, description="For example learned, static, or irb-interface.")


# Internet: layer 3 addressing, routing, and reachability.


class ArpEntry(BaseModel):
    """One ARP binding between an IPv4 address and a MAC address on an interface."""

    device: str
    interface: str
    ip_address: str
    mac: str | None = None
    origin: str | None = Field(None, description="For example dynamic or static.")


class NextHop(BaseModel):
    """Where a route sends a packet."""

    address: str | None = Field(None, description="The next router's address; none for a directly connected network.")
    interface: str | None = Field(None, description="The interface the packet leaves by, when the device names it.")
    action: Literal["forward", "receive", "drop"] = Field(
        "forward", description="receive: the packet is for the device itself. drop: it is discarded."
    )


class Route(BaseModel):
    """One route in a routing table, IPv4 or IPv6."""

    device: str
    network_instance: str
    prefix: str
    protocol: str = Field(description="The route owner, for example local, static, ospfv2, or bgp.")
    next_hops: list[NextHop] = Field(default_factory=list)
    active: bool | None = None
    metric: int | None = None
    preference: int | None = None


class BgpSession(BaseModel):
    """
    One BGP session, as the device that has it configured reports it: who it peers
    with, from which address, and how far it got. A session that isn't Established
    exchanges no routes.
    """

    device: str
    network_instance: str
    peer_address: str
    peer_as: int | None = None
    local_address: str | None = Field(
        None, description="The address the device sends from: configured, or chosen when the session came up."
    )
    local_as: int | None = None
    group: str | None = None
    state: str = Field(
        description=(
            "Established when the session is up. Otherwise how far it got: Idle (not trying), Connect or Active "
            "(trying to open a TCP connection to the peer, and failing: Active does not mean up), OpenSent, OpenConfirm."
        )
    )
    last_error: str | None = None
    flags: list[str] = Field(default_factory=list, description="The device's own notes on the session, as it spells them.")
    families: list[str] = Field(default_factory=list, description="The address families it carries, for example inet-unicast or evpn.")
    flaps: int | None = Field(None, description="How many times it has gone down.")
    received_prefixes: int | None = None
    active_prefixes: int | None = Field(None, description="How many of the received prefixes are the best route, and in use.")


class OspfInterface(BaseModel):
    """
    An interface a router runs OSPF on: the area it is in, and the settings both
    ends of a link must agree on before they become neighbors.
    """

    device: str
    network_instance: str
    router_id: str | None = Field(None, description="This router's OSPF router ID.")
    area: str = Field(description="The area, for example 0.0.0.0. Both ends of a link must be in the same one.")
    interface: str
    address: str | None = None
    network_type: str | None = Field(None, description="point-to-point or broadcast. Both ends of a link must agree.")
    state: str | None = Field(
        None,
        description="Its OSPF state: point-to-point, designated-router, backup-designated-router, dr-other, waiting, or down.",
    )
    area_type: str | None = Field(
        None,
        description=(
            "normal, stub, or nssa. Every router in an area must agree: hellos carry the area's type, and two routers "
            "whose types differ are never neighbors."
        ),
    )
    passive: bool = Field(
        False, description="Passive: OSPF advertises the network but sends no hellos and forms no adjacency on it."
    )
    authentication: str | None = Field(
        None,
        description=(
            "How it authenticates OSPF packets: none, or its keychain's algorithm, such as md5. Both ends of a link must "
            "agree, and share a key, which NetGPT never reads."
        ),
    )
    keychain: str | None = Field(None, description="The keychain it authenticates with, if it does.")
    cost: int | None = Field(
        None, description="This end's cost to send out of the interface. Each direction of a link has its own."
    )
    hello_interval: int | None = Field(
        None, description="Seconds between hellos. Both ends of a link must agree, as on the dead interval."
    )
    dead_interval: int | None = None
    mtu: int | None = Field(
        None, description="The MTU OSPF advertises. When the two ends differ, their adjacency sticks in exstart."
    )
    neighbors: int | None = Field(None, description="How many neighbors it hears.")
    designated_router: str | None = Field(None, description="On a broadcast network, the router ID of the designated router.")
    backup_designated_router: str | None = None


class OspfNeighbor(BaseModel):
    """An OSPF neighbor, as one router sees it on one of its interfaces."""

    device: str
    network_instance: str
    area: str
    interface: str = Field(description="This router's interface that hears the neighbor.")
    router_id: str = Field(description="The neighbor's router ID.")
    address: str | None = Field(None, description="The neighbor's address on the link.")
    state: str = Field(
        description=(
            "full: the adjacency is complete, and routes are exchanged. 2-way: normal between two routers that are "
            "neither DR nor BDR on a broadcast network, but no adjacency anywhere else. init: this router hears the "
            "neighbor, but the neighbor doesn't hear it. exstart or exchange that doesn't move on: usually the two ends' "
            "MTUs differ. down: its hellos stopped."
        )
    )
    priority: int | None = None
    dead_seconds: int | None = Field(None, description="Seconds left before it is declared down, unless it says hello again.")
    uptime_seconds: int | None = Field(None, description="How long the adjacency has been up.")
    state_changes: int | None = Field(
        None, description="How many times its state has changed. A count that keeps climbing is a flap."
    )


class PingResult(BaseModel):
    """The outcome of ICMP echo requests from a source to one destination."""

    source: str = Field(description="The device that sent the pings, or 'netgpt' for the server itself.")
    destination: str
    sent: int
    received: int
    rtt_avg_ms: float | None = None

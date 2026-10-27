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
    addresses: list[str] = Field(default_factory=list, description="IPv4 addresses with prefix length, such as 192.168.12.1/24.")
    oper_state: str | None = Field(None, description="For example up or down.")


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

    address: str | None = Field(None, description="The next router's IPv4 address; none for a directly connected network.")
    interface: str | None = Field(None, description="The interface the packet leaves by, when the device names it.")
    action: Literal["forward", "receive", "drop"] = Field(
        "forward", description="receive: the packet is for the device itself. drop: it is discarded."
    )


class Route(BaseModel):
    """One route in a routing table."""

    device: str
    network_instance: str
    prefix: str
    protocol: str = Field(description="The route owner, for example local, static, or bgp.")
    next_hops: list[NextHop] = Field(default_factory=list)
    active: bool | None = None
    metric: int | None = None
    preference: int | None = None


class PingResult(BaseModel):
    """The outcome of ICMP echo requests from a source to one destination."""

    source: str = Field(description="The device that sent the pings, or 'netgpt' for the server itself.")
    destination: str
    sent: int
    received: int
    rtt_avg_ms: float | None = None

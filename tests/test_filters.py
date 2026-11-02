"""
Tests for the tables' filters (netgpt.filters): what they take, as people and
models write it, and the engine narrowing the recorded Nexus switches' tables by
them, so a question about one host gets its rows, not the network's.
"""

from __future__ import annotations

import ipaddress

import pytest

from netgpt.engine import Engine
from netgpt.filters import FilterError, forwarding, has_mac, mac_parts, networks, on_networks, on_port, ports
from netgpt.inventory import Inventory, InventoryError
from netgpt.primitives import NextHop, Route


@pytest.mark.parametrize("spelling", ["0200.c0ff.ee01", "02:00:C0:FF:EE:01", "0200-C0FF-EE01", "0200c0ffee01", "c0ff", "ff:ee"])
def test_any_part_of_a_mac_in_any_spelling_finds_it(spelling: str) -> None:
    assert has_mac(mac_parts([spelling]), "0200.c0ff.ee01")


def test_a_mac_that_holds_none_of_the_parts_is_left_out() -> None:
    assert not has_mac(mac_parts(["0200.c0ff.ee02"]), "0200.c0ff.ee01")
    assert not has_mac(mac_parts(["c0ff"]), None)


@pytest.mark.parametrize("value", ["zz:zz", "core1", "5"])
def test_a_mac_filter_that_isnt_hex_says_how_to_write_one(value: str) -> None:
    with pytest.raises(FilterError, match="isn't part of a MAC address. Give its hex digits in any spelling"):
        mac_parts([value])


@pytest.mark.parametrize(
    ("wanted", "interface"),
    [
        ("Eth1/9", "Ethernet1/9"), ("ethernet1/9", "Ethernet1/9"), ("Po2", "port-channel2"),
        ("Gi1/0/51", "GigabitEthernet1/0/51"), ("Ethernet 1/9", "Ethernet1/9"), ("Ethernet1/17", "Ethernet1/17.1"),
        ("vlan80", "Vlan80"), ("ethernet-1/1", "ethernet-1/1.0"),
    ],
)
def test_a_port_is_found_by_its_name_or_any_abbreviation_of_it(wanted: str, interface: str) -> None:
    assert on_port(ports([wanted]), interface)


@pytest.mark.parametrize(("wanted", "interface"), [("Eth1/1", "Ethernet1/17"), ("Vlan8", "Vlan80"), ("Po2", "Ethernet1/2")])
def test_a_port_with_another_number_or_word_is_another_port(wanted: str, interface: str) -> None:
    assert not on_port(ports([wanted]), interface)


def test_a_network_may_be_written_with_its_mask_as_cisco_writes_it() -> None:
    assert networks(["192.0.2.0 255.255.255.0"]) == [ipaddress.ip_network("192.0.2.0/24")]
    with pytest.raises(FilterError, match="isn't an IP address or network, such as 192.0.2.5 or 192.0.2.0/24"):
        networks(["core1"])


def test_an_interface_is_on_a_network_its_own_overlaps_or_on_the_address_its_network_holds() -> None:
    assert on_networks(networks(["192.0.2.77"]), ["192.0.2.2/24"])
    assert on_networks(networks(["192.0.0.0/16"]), ["192.0.2.2/24"])
    assert not on_networks(networks(["192.0.3.0/24"]), ["192.0.2.2/24"])
    assert not on_networks(networks(["192.0.2.77"]), ["2001:db8::1/64"])


def test_blank_values_are_no_filter_at_all() -> None:
    # Small models fill in every parameter, the filters with empty strings.
    assert mac_parts(["", " "]) is None and networks([""]) is None and ports([""]) is None
    assert on_port(None, "Ethernet1/9") and has_mac(None, None)


def route(device: str, prefix: str, protocol: str = "static") -> Route:
    hop = NextHop(address="10.0.0.1")
    return Route(device=device, network_instance="default", prefix=prefix, protocol=protocol, next_hops=[hop])


def test_an_address_finds_the_route_each_device_forwards_it_by() -> None:
    table = [
        route("r1", "0.0.0.0/0"), route("r1", "198.51.0.0/16"), route("r1", "198.51.100.0/24", "ospf"),
        route("r1", "198.51.100.0/24", "static"), route("r2", "0.0.0.0/0"), route("r2", "2001:db8::/32"),
    ]
    found = forwarding(table, networks(["198.51.100.77"]))
    # Both of r1's routes with the longest prefix, whichever is active; r2 has nothing better than its default.
    assert [(item.device, item.prefix, item.protocol) for item in found] == [
        ("r1", "198.51.100.0/24", "ospf"), ("r1", "198.51.100.0/24", "static"), ("r2", "0.0.0.0/0", "static"),
    ]


def test_a_network_finds_the_routes_inside_it_and_the_one_that_holds_it() -> None:
    table = [route("r1", "0.0.0.0/0"), route("r1", "192.0.2.0/24", "direct"), route("r1", "192.0.2.1/32", "vrrp"),
             route("r1", "192.0.3.0/24", "direct")]
    found = forwarding(table, networks(["192.0.2.0/24"]))
    assert [item.prefix for item in found] == ["192.0.2.0/24", "192.0.2.1/32"]


@pytest.mark.anyio
async def test_a_mac_in_another_spelling_finds_its_entry_on_the_recorded_nexus(nexus: Engine) -> None:
    found = await nexus.mac_table(mac=["02:00:00:00:00:43"])
    assert [(item.device, item.interface, item.vlan) for item in found.items] == [("dc1.example.net", "Ethernet1/9", 2502)]


@pytest.mark.anyio
async def test_a_vlan_and_a_port_narrow_the_mac_table(nexus: Engine) -> None:
    found = await nexus.mac_table(vlan=[80], interface=["Eth1/32"])
    assert [item.mac for item in found.items] == ["0200.0000.0022", "0200.0000.0023"]


@pytest.mark.anyio
async def test_an_address_finds_the_interface_that_is_its_gateway(nexus: Engine) -> None:
    found = await nexus.interfaces(address=["198.18.12.77"])
    found_at = [(item.device, item.name, item.addresses) for item in found.items]
    assert found_at == [("core1.example.net", "Vlan5", ["198.18.12.2/24"])]


@pytest.mark.anyio
async def test_a_destination_finds_the_route_and_the_gateways_addresses(nexus: Engine) -> None:
    found = await nexus.routes(["core1"], destination=["198.18.12.0/24"])
    assert [(item.prefix, item.protocol) for item in found.items] == [
        ("198.18.12.0/24", "direct"), ("198.18.12.1/32", "vrrp"), ("198.18.12.2/32", "local"),
    ]
    elsewhere = await nexus.routes(None, destination=["203.0.113.9"])
    assert [(item.device, item.prefix) for item in elsewhere.items] == [
        ("core1.example.net", "0.0.0.0/0"), ("dc1.example.net", "0.0.0.0/0"),
    ]


@pytest.mark.anyio
async def test_arp_entries_on_a_network_from_every_device(nexus: Engine) -> None:
    found = await nexus.arp_table(address=["198.18.9.0/24"])
    assert [item.ip_address for item in found.items] == ["198.18.9.20", "198.18.9.21"]


@pytest.mark.anyio
async def test_a_route_protocol_is_matched_by_its_start(nexus: Engine) -> None:
    found = await nexus.routes(["core1.example.net"], protocol=["OSPF"])
    assert {item.protocol for item in found.items} == {"ospf"} and len(found.items) == 3


@pytest.mark.anyio
async def test_a_device_named_without_its_domain_is_read_under_its_own_name(nexus: Engine) -> None:
    found = await nexus.lldp_neighbors(["core1", "DC1"], interface=["Eth1/14", "Ethernet1/30"])
    assert [(item.device, item.neighbor) for item in found.items] == [
        ("core1.example.net", "dc1.example.net"), ("dc1.example.net", "core1.example.net"),
    ]
    assert found.errors == []


def test_a_short_name_two_devices_could_have_is_refused_with_both(nexus: Engine) -> None:
    inventory = Inventory.model_validate(
        {
            "credentials": nexus.inventory.credentials,
            "devices": [
                {"name": name, "host": "192.0.2.1", "platform": "cisco_nxos", "credentials": "lab"}
                for name in ("core1.example.net", "core1.example.org")
            ],
        }
    )
    with pytest.raises(InventoryError, match="'core1' could be core1.example.net or core1.example.org. Name one."):
        inventory.get("core1")


@pytest.mark.anyio
async def test_a_filter_is_refused_before_any_device_is_asked(nexus: Engine, inventory: Inventory, table_reads: list) -> None:
    from fakes import FakePlatform

    with pytest.raises(FilterError):
        await Engine(inventory, platforms={"fake": FakePlatform}).arp_table(mac=["not a mac"])
    assert table_reads == []

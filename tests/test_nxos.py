"""
Tests for the NX-OS driver: each query against replies recorded from real
switches (a Nexus 9396PX core on NX-OS 7.0(3)I7(7) and a Nexus 5548UP on
7.3(8)N1(1)), trimmed and made anonymous by lab/record_nxos.py. The core's
release can't turn its OSPF replies into JSON, so they come from its XML.
Also the guard's NX-OS policy, and logging in again when a switch drops a
connection between commands.
"""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace
from typing import Any

import asyncssh
import pytest

from netgpt.guard import CISCO_NXOS, GuardError
from netgpt.inventory import Credentials, Device
from netgpt.platforms.base import PlatformError
from netgpt.platforms.nxos import CLI_HINT, ExecOverSsh, NxosPlatform, Reply, seconds_of

from fakes import NXOS_FIXTURES, CommandReplay

pytestmark = pytest.mark.anyio

CORE, DC = "core1.example.net", "dc1.example.net"


class Scripted:
    """Answers each command from a table, and fails any it doesn't have the way NX-OS does."""

    def __init__(self, replies: dict[str, str]) -> None:
        self.replies = replies
        self.asked: list[str] = []

    async def run(self, command: str) -> Reply:
        self.asked.append(command)
        if command not in self.replies:
            return Reply(16, f"Syntax error while parsing '{command}'\n\n\nCmd exec error.\n")
        return Reply(0, self.replies[command])

    async def close(self) -> None:
        pass


def nxos(device: str, session: CommandReplay | Scripted | None = None) -> tuple[NxosPlatform, Any]:
    session = session or CommandReplay(device)
    info = Device(name=device, host="192.0.2.1", platform="cisco_nxos", credentials="lab")
    return NxosPlatform(info, Credentials(username="netgpt", password_env="UNUSED"), session=session), session


async def test_lldp_names_local_ports_in_full_and_far_ports_as_the_neighbor_sent_them() -> None:
    platform, session = nxos(CORE)
    neighbors = {neighbor.interface: neighbor for neighbor in await platform.lldp_neighbors()}
    assert session.asked == ["show lldp neighbors detail | json"]
    # NX-OS shortens its own ports in LLDP (Eth1/14); the 5548 sends its port the same way.
    dc = neighbors["Ethernet1/14"]
    assert (dc.neighbor, dc.neighbor_interface, dc.neighbor_chassis_id) == (DC, "Eth1/30", "0200.0000.0004")
    assert (neighbors["mgmt0"].neighbor, neighbors["mgmt0"].neighbor_interface) == ("core2.example.net", "mgmt0")
    # A host that sends no system name, and its MAC as its port.
    host = neighbors["Ethernet1/1"]
    assert (host.neighbor, host.neighbor_interface) == (None, "0200.0000.0002")


async def test_a_neighbor_that_sends_its_mac_as_its_port_is_known_by_the_port_s_description() -> None:
    row = {"chassis_id": "0200.0000.0009", "port_type": "Mac Address", "port_id": "0200.0000.000a", "l_port_id": "Eth1/31",
           "port_desc": "eth2", "sys_name": "storage-a"}
    reply = json.dumps({"TABLE_nbor_detail": {"ROW_nbor_detail": row}})
    platform, _ = nxos(CORE, Scripted({"show lldp neighbors detail | json": reply}))
    [neighbor] = await platform.lldp_neighbors()
    assert (neighbor.interface, neighbor.neighbor, neighbor.neighbor_interface) == ("Ethernet1/31", "storage-a", "eth2")


async def test_interfaces_route_in_their_vrf_or_bridge_in_the_default_instance_with_their_port_channel() -> None:
    platform, session = nxos(CORE)
    interfaces = {interface.name: interface for interface in await platform.interfaces()}
    assert sorted(session.asked) == ["show interface brief | json", "show ip interface vrf all | json"]
    member = interfaces["Ethernet1/1"]
    assert (member.mode, member.network_instance, member.aggregate, member.oper_state) == ("bridged", "default", "port-channel101", "up")
    assert (interfaces["port-channel1"].mode, interfaces["port-channel1"].aggregate) == ("bridged", None)
    # A VLAN interface keeps its secondary addresses after its primary one.
    assert interfaces["Vlan114"].addresses == ["198.18.20.2/24", "198.18.21.2/24", "198.18.22.2/24"]
    assert (interfaces["loopback2"].network_instance, interfaces["loopback2"].addresses) == ("ICL", ["198.18.11.1/32"])
    assert (interfaces["Ethernet1/17.1"].mode, interfaces["Ethernet1/17.1"].addresses) == ("routed", ["198.18.5.2/30"])
    assert (interfaces["mgmt0"].network_instance, interfaces["mgmt0"].addresses) == ("management", ["198.18.0.1/30"])
    # The 9000 leaves VLAN interfaces out of its brief list; they come from the addressed ones.
    assert "Vlan5" in interfaces and "Vlan5" not in (NXOS_FIXTURES / CORE / "11-show-interface-brief-json.txt").read_text()


async def test_the_5548_lists_vlan_interfaces_with_numbers_where_the_9000_has_strings() -> None:
    platform, _ = nxos(DC)
    interfaces = {interface.name: interface for interface in await platform.interfaces()}
    assert (interfaces["Vlan30"].addresses, interfaces["Vlan30"].oper_state) == (["198.18.4.102/24"], "up")
    assert interfaces["Ethernet1/3"].aggregate == "port-channel1121"
    assert (interfaces["Ethernet1/25"].mode, interfaces["Ethernet1/25"].addresses) == ("routed", ["198.18.5.102/30"])


async def test_counters_are_read_for_ethernet_ports_and_port_channels_with_speed_from_bandwidth() -> None:
    platform, _ = nxos(DC)
    counters = {port.interface: port for port in await platform.interface_counters()}
    assert all(name.startswith(("Ethernet", "port-channel")) for name in counters)
    uplink = counters["Ethernet1/3"]
    assert (uplink.speed_bps, uplink.in_octets, uplink.out_octets, uplink.in_packets, uplink.out_packets) == (
        10**10, 387558109, 129193329923, 1570321, 1746357275
    )
    assert (counters["Ethernet1/9"].in_discards, counters["Ethernet1/25"].in_errors) == (534642, 222)
    # A port-channel's bandwidth is its members'; NX-OS counts no link flaps.
    assert counters["port-channel1"].speed_bps == 2 * 10**10
    assert {port.carrier_transitions for port in counters.values()} == {None}


async def test_mac_entries_keep_their_vlan_and_the_switch_s_own_are_gateways() -> None:
    platform, session = nxos(CORE)
    macs = await platform.mac_table()
    assert sorted(session.asked) == ["show mac address-table | json", "show vpc brief | json"]
    by_mac = {entry.mac: entry for entry in macs}
    own = by_mac["0000.5e00.0105"]
    assert (own.interface, own.entry_type, own.vlan, own.network_instance) == ("sup-eth1", "gateway", 5, "default")
    learned = by_mac["0200.0000.0021"]
    assert (learned.interface, learned.entry_type, learned.vlan) == ("Ethernet1/23", "dynamic", 20)
    # The vPC peer's gateway MACs arrive over the peer link, which NX-OS names by its role: it is port-channel1.
    assert (by_mac["0000.0c07.ac59"].interface, by_mac["0000.0c07.ac59"].entry_type) == ("port-channel1", "gateway")
    platform, _ = nxos(DC)
    assert {entry.entry_type for entry in await platform.mac_table()} == {"dynamic", "static"}


async def test_without_vpc_the_peer_link_keeps_the_name_nx_os_gives_it() -> None:
    row = {"disp_mac_addr": "0200.0000.0001", "disp_type": "G", "disp_vlan": "10", "disp_is_static": "enabled",
           "disp_port": "vPC Peer-Link(R)"}
    reply = json.dumps({"TABLE_mac_address": {"ROW_mac_address": row}})
    platform, _ = nxos(CORE, Scripted({"show mac address-table | json": reply}))
    [entry] = await platform.mac_table()
    assert entry.interface == "vPC Peer-Link"


async def test_arp_leaves_out_addresses_that_never_answered_and_marks_bindings_the_vpc_peer_synced() -> None:
    platform, _ = nxos(CORE)
    entries = await platform.arp_table()
    assert '"incomplete"' in (NXOS_FIXTURES / CORE / "05-show-ip-arp-vrf-all-json.txt").read_text()
    assert all(entry.mac for entry in entries)
    by_address = {entry.ip_address: entry for entry in entries}
    assert (by_address["198.18.5.254"].interface, by_address["198.18.5.254"].origin) == ("Vlan3090", "dynamic")
    assert by_address["198.18.9.20"].origin == "synced"
    assert (by_address["198.18.6.2"].interface, by_address["198.18.0.2"].interface) == ("Ethernet1/29", "mgmt0")


async def test_routes_name_their_protocol_without_the_process_tag_and_the_device_s_own_addresses_receive() -> None:
    platform, session = nxos(CORE)
    routes = {(route.prefix, route.protocol): route for route in await platform.routes("default")}
    assert session.asked == ["show ip route vrf default | json"]
    default = routes[("0.0.0.0/0", "ospf")]
    assert (default.metric, default.preference, default.active) == (1, 110, True)
    # NX-OS shortens next-hop interfaces too (Eth1/17.1).
    assert [(hop.address, hop.interface, hop.action) for hop in default.next_hops] == [("198.18.5.1", "Ethernet1/17.1", "forward")]
    connected = routes[("198.18.12.0/24", "direct")]
    assert [(hop.address, hop.interface) for hop in connected.next_hops] == [(None, "Vlan5")]
    # Its own address, and the gateway addresses it shares by VRRP and HSRP, are for the switch itself.
    for owned in [("198.18.12.2/32", "local"), ("198.18.12.1/32", "vrrp"), ("198.18.27.1/32", "hsrp")]:
        assert [hop.action for hop in routes[owned].next_hops] == ["receive"]
    static = routes[("198.18.28.0/24", "static")]
    assert [(hop.address, hop.interface) for hop in static.next_hops] == [("198.18.29.1", None)]


async def test_a_vrf_s_routes_include_equal_cost_paths_and_a_vrf_the_switch_lacks_has_none() -> None:
    platform, _ = nxos(CORE)
    routes = {(route.prefix, route.protocol): route for route in await platform.routes("ICL")}
    assert all(route.network_instance == "ICL" for route in routes.values())
    assert [hop.interface for hop in routes[("198.18.14.0/24", "ospf")].next_hops] == ["Ethernet1/29", "Ethernet1/30"]
    platform, _ = nxos(CORE)
    assert await platform.routes("nosuchvrf") == []
    with pytest.raises(PlatformError, match="not a valid network instance"):
        await platform.routes("default ; show running-config")


async def test_ospf_comes_from_xml_when_the_switch_can_t_make_it_json() -> None:
    platform, session = nxos(CORE)
    interfaces = {(item.network_instance, item.interface): item for item in await platform.ospf_interfaces()}
    assert sorted(session.asked) == [
        "show ip ospf interface vrf all | json", "show ip ospf interface vrf all | xml",
        "show ip ospf vrf all | json", "show ip ospf vrf all | xml",
    ]
    link = interfaces[("ICL", "Ethernet1/29")]
    assert (link.router_id, link.area, link.network_type, link.state, link.cost) == (
        "198.18.11.1", "0.0.0.0", "broadcast", "designated-router", 4
    )
    assert (link.hello_interval, link.dead_interval, link.neighbors, link.authentication, link.area_type) == (10, 40, 1, "none", "normal")
    assert (link.designated_router, link.backup_designated_router, link.address) == ("198.18.11.1", "198.18.14.2", "198.18.6.1/30")
    # Each VRF's OSPF process has a router ID of its own.
    assert interfaces[("default", "Vlan3090")].router_id == "198.18.3.2"
    passive = interfaces[("default", "Vlan3097")]
    assert (passive.passive, passive.hello_interval, passive.authentication) == (True, None, "none")
    assert interfaces[("default", "Ethernet1/25")].network_type == "point-to-point"
    assert interfaces[("ICL", "loopback2")].network_type == "loopback"
    platform, session = nxos(DC)
    assert len(await platform.ospf_interfaces()) == 2
    assert not [command for command in session.asked if command.endswith("| xml")]


async def test_ospf_neighbors_say_their_area_state_and_how_long_they_have_been_up() -> None:
    platform, _ = nxos(CORE)
    neighbors = {(item.network_instance, item.interface): item for item in await platform.ospf_neighbors()}
    assert len(neighbors) == 4
    near = neighbors[("ICL", "Ethernet1/29")]
    assert (near.router_id, near.address, near.area, near.state, near.priority) == ("198.18.14.2", "198.18.6.2", "0.0.0.0", "full", 1)
    assert (near.dead_seconds, near.state_changes) == (37, 10)
    # P1M12DT17H... : a month and 12 days and change, with months taken as 30 days.
    assert 42 * 86400 < near.uptime_seconds < 43 * 86400


def test_nx_os_durations_are_read_as_seconds() -> None:
    assert seconds_of("PT41S") == 41
    assert seconds_of("P4M25DT9H20M51S") == 4 * 30 * 86400 + 25 * 86400 + 9 * 3600 + 20 * 60 + 51
    assert seconds_of("P1Y2W") == 365 * 86400 + 14 * 86400
    assert seconds_of("PT0S") == 0
    assert seconds_of("P") is None and seconds_of("00:00:41") is None and seconds_of(None) is None


async def test_show_runs_the_command_with_its_pipes_and_a_command_nx_os_lacks_fails_with_examples() -> None:
    platform, session = nxos(CORE)
    shown = await platform.show("show vrf | include Up")
    assert session.asked == ["show vrf | include Up"]
    assert [line.split()[0] for line in shown.output.splitlines() if line.strip()] == ["ICL", "default", "management"]
    with pytest.raises(PlatformError, match="Syntax error while parsing 'show bogus'") as rejected:
        await platform.show("show bogus")
    assert CLI_HINT in str(rejected.value) and "Cmd exec error" not in str(rejected.value)


async def test_a_command_the_role_refuses_fails_with_what_nx_os_said() -> None:
    session = Scripted({})

    async def refused(command: str) -> Reply:
        return Reply(30, "% Permission denied for the role\n\nCmd exec error.\n")

    session.run = refused  # type: ignore[method-assign]
    platform, _ = nxos(CORE, session)
    with pytest.raises(PlatformError, match="core1.example.net: % Permission denied for the role$"):
        await platform.show("show running-config")


@pytest.mark.parametrize(
    "command",
    ["show ip route vrf all | json", "show interface brief | include up | count", "sh lldp neighbors", "show run | section ospf"],
)
def test_nx_os_allows_read_only(command: str) -> None:
    assert CISCO_NXOS.check(command) == command


@pytest.mark.parametrize(
    "command",
    [
        "configure terminal",
        "show running-config > bootflash:x",
        "show running-config >> bootflash:x",
        "show version ; reload",
        "show running-config | email to x@example.net",
        "show running-config | tee bootflash:x",
        "show version | python",
        "run bash ls",
        "show clock\nreload",
    ],
)
def test_nx_os_refuses_writes_and_chaining(command: str) -> None:
    with pytest.raises(GuardError):
        CISCO_NXOS.check(command)


class FlakyConnection:
    """An SSH connection whose commands fail in the ways given, in turn, and then succeed."""

    def __init__(self, failures: list[BaseException]) -> None:
        self.failures = failures
        self.ran: list[str] = []

    async def run(self, command: str, check: bool) -> SimpleNamespace:
        self.ran.append(command)
        if self.failures:
            raise self.failures.pop(0)
        return SimpleNamespace(stdout='{"hostname": "core1"}\n', exit_status=0)


class CountedLogin:
    """Stands in for SshLogin: hands out one connection, and counts the times it is closed."""

    def __init__(self, connection: FlakyConnection) -> None:
        self.connection_ = connection
        self.closed = 0

    async def connection(self) -> FlakyConnection:
        return self.connection_

    async def close(self) -> None:
        self.closed += 1


def flaky(failures: list[BaseException]) -> tuple[ExecOverSsh, CountedLogin]:
    info = Device(name="core1", host="192.0.2.1", platform="cisco_nxos", credentials="lab")
    commands = ExecOverSsh(info, Credentials(username="netgpt", password_env="UNUSED"))
    login = CountedLogin(FlakyConnection(failures))
    commands._login = login  # type: ignore[assignment]
    return commands, login


async def test_a_connection_the_switch_dropped_between_commands_is_logged_into_again_once() -> None:
    commands, login = flaky([asyncssh.ChannelOpenError(asyncssh.OPEN_CONNECT_FAILED, "SSH connection closed")])
    assert await commands.run("show hostname | json") == Reply(0, '{"hostname": "core1"}\n')
    assert (login.connection_.ran, login.closed) == (["show hostname | json"] * 2, 1)
    commands, login = flaky([asyncssh.ConnectionLost("gone"), asyncssh.ConnectionLost("gone again")])
    with pytest.raises(PlatformError, match="core1: ConnectionLost: gone again"):
        await commands.run("show hostname | json")
    assert login.closed == 2


async def test_a_command_that_never_finishes_is_run_once_more_on_a_new_login(monkeypatch: pytest.MonkeyPatch) -> None:
    class Stalls(FlakyConnection):
        async def run(self, command: str, check: bool) -> SimpleNamespace:
            if not self.ran:
                self.ran.append(command)
                await asyncio.sleep(10)
            return await super().run(command, check)

    monkeypatch.setattr("netgpt.platforms.nxos.COMMAND_TIMEOUT", 0.05)
    commands, login = flaky([])
    login.connection_ = Stalls([])
    assert (await commands.run("show hostname | json")).status == 0
    assert (len(login.connection_.ran), login.closed) == (2, 1)

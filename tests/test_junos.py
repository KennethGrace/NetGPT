"""
Tests for the Junos driver: each query against NETCONF replies recorded from real
Junos 23.4 switches (a QFX5120 core and two EX4400 office switches, renamed by
lab/record_junos.py), and the CLI filtering that Junos's <command> RPC leaves to
the driver. The switches were recorded while their EVPN overlay was down, every
BGP session short of Established; core-bgp-established.xml is the core's sessions
after it came up.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from netgpt.guard import JUNIPER_JUNOS, GuardError
from netgpt.inventory import Credentials, Device, Inventory, InventoryError
from netgpt.platforms.base import PlatformError
from netgpt.platforms.junos import CLI_HINT, JunosPlatform

from fakes import JUNOS_FIXTURES, NetconfReplay

pytestmark = pytest.mark.anyio


class Scripted:
    """A NETCONF session that answers every RPC with the same reply."""

    def __init__(self, reply: str) -> None:
        self.reply = reply

    async def rpc(self, body: str) -> str:
        return self.reply

    async def close(self) -> None:
        pass


def junos(device: str, session: NetconfReplay | Scripted | None = None) -> tuple[JunosPlatform, NetconfReplay | Scripted]:
    session = session or NetconfReplay(device)
    info = Device(name=device, host="192.0.2.1", platform="juniper_junos", credentials="lab")
    return JunosPlatform(info, Credentials(username="netgpt", password_env="UNUSED"), session=session), session


async def test_lldp_names_each_neighbor_s_port_from_that_port_s_detail() -> None:
    platform, session = junos("core")
    neighbors = {neighbor.interface: neighbor for neighbor in await platform.lldp_neighbors()}
    assert set(neighbors) == {"em0", "xe-0/0/46", "xe-0/0/0", "xe-0/0/1"}
    south = neighbors["xe-0/0/0"]
    assert (south.neighbor, south.neighbor_interface, south.neighbor_chassis_id) == ("south", "xe-0/2/0", "d0:81:c5:26:f4:80")
    assert (neighbors["xe-0/0/46"].neighbor, neighbors["xe-0/0/46"].neighbor_interface) == ("lab-router", "xe-0/0/16")
    # The summary, then one detail per port.
    assert len(session.asked) == 1 + len(neighbors)


async def test_a_neighbor_that_sends_its_mac_as_its_port_is_known_by_the_port_s_description() -> None:
    platform, _ = junos("south")
    neighbors = {neighbor.interface: neighbor for neighbor in await platform.lldp_neighbors()}
    access_point = neighbors["ge-0/0/22"]
    assert (access_point.neighbor, access_point.neighbor_interface) == ("ap-south-7", "ETH0")
    assert neighbors["ge-0/0/19"].neighbor is None and neighbors["ge-0/0/19"].neighbor_chassis_id == "a8:3a:79:a9:c4:26"


async def test_interfaces_are_in_their_routing_instance_and_junos_s_own_are_left_out() -> None:
    platform, _ = junos("core")
    interfaces = {interface.name: interface for interface in await platform.interfaces()}
    # Not the packet engines' (pfe, pfh), the services' (jsrv), label switching's (lsi), or the private instances'.
    assert set(interfaces) == {"xe-0/0/0.0", "xe-0/0/1.0", "xe-0/0/46.0", "em0.0", "irb.0", "lo0.0", "lo0.1", "vme.0"}
    assert (interfaces["xe-0/0/46.0"].network_instance, interfaces["xe-0/0/46.0"].addresses) == ("Global", ["172.16.63.253/31"])
    assert (interfaces["lo0.1"].network_instance, interfaces["lo0.1"].addresses) == ("Global", ["172.16.1.1/32"])
    assert interfaces["vme.0"].network_instance == "mgmt_junos"
    # Junos's master instance is NetGPT's default. The loopback's link-local address names no network.
    assert (interfaces["lo0.0"].network_instance, interfaces["lo0.0"].addresses) == ("default", ["fd00::5c47:fd4c:bd1d:7b62:91c4/128"])
    assert interfaces["xe-0/0/0.0"].addresses == [] and interfaces["xe-0/0/0.0"].mode == "routed"


async def test_switch_ports_are_bridged_in_the_default_switch() -> None:
    platform, _ = junos("south")
    interfaces = {interface.name: interface for interface in await platform.interfaces()}
    port = interfaces["ge-0/0/22.0"]
    assert (port.mode, port.network_instance, port.oper_state) == ("bridged", "default-switch", "up")
    assert (interfaces["irb.10"].network_instance, interfaces["irb.10"].oper_state) == ("Global", "down")


async def test_the_unit_junos_gives_an_unconfigured_port_is_not_an_interface() -> None:
    platform, _ = junos("north")
    names = [interface.name for interface in await platform.interfaces()]
    assert not [name for name in names if name.endswith(".16386")]
    assert "ge-0/0/2.0" in names


async def test_routes_come_from_an_instance_s_ipv4_and_ipv6_tables() -> None:
    platform, session = junos("south")
    routes = {route.prefix: route for route in await platform.routes("default")}
    assert session.asked == [
        "<get-route-information><table>inet.0</table></get-route-information>",
        "<get-route-information><table>inet6.0</table></get-route-information>",
    ]
    core = routes["fd00::5c47:fd4c:bd1d:7b62:91c4/128"]
    assert (core.protocol, core.active, core.metric, core.preference) == ("is-is", True, 10, 18)
    assert [(hop.address, hop.interface) for hop in core.next_hops] == [("fe80::3e08:cdff:fe5a:bb84", "xe-0/2/0.0")]
    own = routes["fd00::5c47:ffc4:2fb9:48e0:fbf2/128"]
    assert own.protocol == "direct" and [(hop.address, hop.interface) for hop in own.next_hops] == [(None, "lo0.0")]
    assert [hop.action for hop in routes["fe80::d281:c5ff:fe26:f4bb/128"].next_hops] == ["receive"]


async def test_a_vrf_s_routes_come_from_its_own_tables_and_an_address_on_a_down_interface_drops() -> None:
    platform, session = junos("south")
    routes = {route.prefix: route for route in await platform.routes("Global")}
    assert session.asked[0] == "<get-route-information><table>Global.inet.0</table></get-route-information>"
    assert set(routes) == {"172.16.1.11/32", "172.16.10.1/32"}
    assert [hop.action for hop in routes["172.16.10.1/32"].next_hops] == ["drop"]
    assert all(route.network_instance == "Global" for route in routes.values())


async def test_master_is_another_name_for_the_main_instance() -> None:
    platform, session = junos("south")
    assert await platform.routes("master")
    assert session.asked[0] == "<get-route-information><table>inet.0</table></get-route-information>"
    with pytest.raises(PlatformError, match="not a valid network instance"):
        await platform.routes("Global</table><table>inet.0")


async def test_bgp_sessions_say_who_each_peers_with_from_where_and_how_far_it_got() -> None:
    platform, _ = junos("south")
    [session] = await platform.bgp_sessions()
    assert (session.peer_address, session.peer_as, session.local_address, session.local_as) == (
        "fd00:ff01:1:1:3e08:cd0f:fc5a:bb80", 65001, "fd00:ff01:1:1:d281:c50f:fc26:f480", 65011
    )
    assert (session.state, session.flags, session.group, session.network_instance) == ("Idle", ["PeerInterfaceUnknown"], "Overlay", "default")
    assert (session.families, session.flaps, session.received_prefixes) == (["evpn"], 0, None)
    platform, _ = junos("core")
    assert [(session.state, session.peer_as) for session in await platform.bgp_sessions()] == [("Active", 65011), ("Active", 65012)]


async def test_an_established_session_counts_each_prefix_once_where_it_arrived() -> None:
    reply = (JUNOS_FIXTURES / "core-bgp-established.xml").read_text()
    platform, _ = junos("core", Scripted(reply))
    south, north = await platform.bgp_sessions()
    # The TCP port is not part of the address.
    assert (south.peer_address, south.local_address) == ("fd00:ff01:1:1:d281:c50f:fc26:f480", "fd00:ff01:1:1:3e08:cd0f:fc5a:bb80")
    assert (south.state, south.flags, south.last_error) == ("Established", ["Sync"], "None")
    # Each EVPN route is in bgp.evpn.0, where it arrived, and again in Global.evpn.0, which imported it.
    assert (south.received_prefixes, south.active_prefixes) == (1, 1)
    assert north.peer_as == 65012 and north.received_prefixes == 2


async def test_counters_are_read_from_each_port_that_is_up_one_at_a_time() -> None:
    platform, session = junos("south")
    counters = {port.interface: port for port in await platform.interface_counters()}
    assert set(counters) == {"ge-0/0/19", "ge-0/0/22", "ge-0/0/23", "xe-0/2/0"}
    assert len(session.asked) == 1 + len(counters)
    uplink = counters["xe-0/2/0"]
    assert (uplink.speed_bps, uplink.in_octets, uplink.out_octets, uplink.in_packets, uplink.out_packets) == (
        10**10, 487146091, 1186590941, 3466444, 10247703
    )
    assert (uplink.in_errors, uplink.in_discards, uplink.carrier_transitions) == (0, 0, 1)
    assert counters["ge-0/0/19"].speed_bps == 10**9


async def test_mac_and_arp_entries_name_the_logical_interface() -> None:
    platform, _ = junos("south")
    macs = {entry.mac: entry for entry in await platform.mac_table()}
    assert (macs["a8:3a:79:a9:c0:c0"].interface, macs["a8:3a:79:a9:c0:c0"].network_instance) == ("ge-0/0/22.0", "default-switch")
    assert {entry.entry_type for entry in macs.values()} == {"dynamic"}
    platform, _ = junos("core")
    arp = {(entry.interface, entry.ip_address): entry for entry in await platform.arp_table()}
    assert ("xe-0/0/46.0", "172.16.63.252") in arp and ("vme.0", "192.0.2.1") in arp
    # The QFX's internal network (em2.32768, in a private instance) is Junos's own business.
    assert not [interface for interface, _ in arp if interface.startswith("em2")]


async def test_the_driver_applies_the_pipes_junos_s_command_rpc_ignores() -> None:
    platform, session = junos("south")
    matched = await platform.show("show bgp summary | match peer")
    assert session.asked[-1] == '<command format="text">show bgp summary</command>'
    assert matched.output.splitlines() == [
        "Groups: 1 Peers: 1 Down peers: 1",
        "Peer                     AS      InPkt     OutPkt    OutQ   Flaps Last Up/Dwn State|#Active/Received/Accepted/Damped...",
    ]
    assert matched.command == "show bgp summary | match peer"
    excepted = (await platform.show("show bgp summary | except peer | last 1")).output
    assert excepted.startswith("fd00:ff01:1:1:3e08:cd0f:fc5a:bb80") and excepted.rstrip().endswith("Idle")
    assert (await platform.show("show bgp summary | count")).output == "Count: 8 lines"
    with pytest.raises(PlatformError, match="only \\| display xml and \\| display json"):
        await platform.show("show bgp summary | display set")


async def test_a_command_junos_lacks_fails_with_examples_of_ones_it_has() -> None:
    platform, _ = junos("south")
    # Some commands Junos lacks come back as an empty reply rather than an error.
    with pytest.raises(PlatformError, match="returned nothing for 'show ip route'") as nothing:
        await platform.show("show ip route")
    assert CLI_HINT in str(nothing.value)
    with pytest.raises(PlatformError, match="syntax error, expecting <command>: bogus") as rejected:
        await platform.show("show bogus")
    assert CLI_HINT in str(rejected.value)


async def test_an_rpc_error_fails_the_query_but_a_warning_does_not() -> None:
    reply = """<rpc-reply xmlns="urn:ietf:params:xml:ns:netconf:base:1.0"><rpc-error>
      <error-severity>{severity}</error-severity><error-message>License key missing; requires 'bgp' license</error-message>
    </rpc-error><bgp-information/></rpc-reply>"""
    platform, _ = junos("core", Scripted(reply.format(severity="warning")))
    assert await platform.bgp_sessions() == []
    platform, _ = junos("core", Scripted(reply.format(severity="error")))
    with pytest.raises(PlatformError, match="core: License key missing"):
        await platform.bgp_sessions()


@pytest.mark.parametrize(
    "command",
    ["show route table inet6.0", "show bgp summary | match Idle", "show interfaces terse | except down | count", "sh lldp neighbors"],
)
def test_junos_allows_read_only(command: str) -> None:
    assert JUNIPER_JUNOS.check(command) == command


@pytest.mark.parametrize(
    "command",
    [
        "request system reboot",
        "configure",
        "show configuration | save /var/tmp/x",
        "show log messages | request message all message hi",
        "show version\nrequest system halt",
        "file delete /var/tmp/x",
    ],
)
def test_junos_refuses_writes_and_chaining(command: str) -> None:
    with pytest.raises(GuardError):
        JUNIPER_JUNOS.check(command)


def test_a_login_takes_exactly_one_secret() -> None:
    with pytest.raises(ValueError, match="exactly one of password_env, password_file, or ssh_key_file"):
        Credentials(username="netgpt", password_env="X", ssh_key_file=Path("key"))
    with pytest.raises(ValueError, match="exactly one"):
        Credentials(username="netgpt")


def test_an_ssh_key_readable_by_others_is_refused_and_a_key_login_has_no_password(tmp_path: Path) -> None:
    key = tmp_path / "netgpt_ed25519"
    key.write_text("not really a key")
    key.chmod(0o644)
    (tmp_path / "inventory.yml").write_text(
        "credentials: {key: {username: netgpt, ssh_key_file: netgpt_ed25519}}\n"
        "devices: [{name: core, host: 192.0.2.21, platform: juniper_junos, credentials: key, known_hosts: known_hosts}]\n"
    )
    inventory = Inventory.load(tmp_path / "inventory.yml")
    device = inventory.get("core")
    credentials = inventory.credentials_for(device)
    # Paths in the inventory resolve against its own folder.
    assert (credentials.ssh_key_file, device.known_hosts) == (key, tmp_path / "known_hosts")
    with pytest.raises(InventoryError, match="chmod 600"):
        credentials.ssh_key()
    key.chmod(0o600)
    assert credentials.ssh_key() == key
    with pytest.raises(InventoryError, match="uses an SSH key"):
        credentials.password()

"""
Tests for the MCP server, driven through an in-process MCP client against the
fake platform in conftest.py, so the protocol layer is exercised without any
devices.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from mcp import Client

from netgpt.inventory import Inventory, InventoryError
from netgpt.platforms import PlatformError
from netgpt.primitives import PingResult
from netgpt.server import build_server

from fakes import FakePlatform


@pytest.mark.anyio
async def test_every_tool_is_annotated_read_only(client: Client) -> None:
    async with client:
        tools = (await client.list_tools()).tools
    assert {tool.name for tool in tools} == {
        "list_devices", "show", "lldp_neighbors", "interfaces", "mac_table", "arp_table", "routes", "topology",
        "forwarding_path", "ping", "ping_sweep",
    }
    assert all(tool.annotations and tool.annotations.read_only_hint for tool in tools)


@pytest.mark.anyio
async def test_list_devices_never_exposes_credentials(client: Client) -> None:
    async with client:
        result = await client.call_tool("list_devices", {})
    text = str(result.structured_content)
    assert "leaf1" in text and "NotARealPassword" not in text and "admin" not in text


@pytest.mark.anyio
async def test_show_runs_allowed_command(client: Client) -> None:
    async with client:
        result = await client.call_tool("show", {"device": "leaf1", "command": "show version"})
    assert not result.is_error
    assert result.structured_content["output"] == "leaf1: ran show version"


@pytest.mark.anyio
async def test_show_refuses_write_and_explains(client: Client, commands_run: list[str]) -> None:
    async with client:
        result = await client.call_tool("show", {"device": "leaf1", "command": "info > /tmp/exfil"})
    assert result.is_error
    assert "'>' is not allowed" in result.content[0].text
    assert commands_run == []


@pytest.mark.anyio
async def test_show_refuses_hosts_outside_inventory(client: Client) -> None:
    async with client:
        result = await client.call_tool("show", {"device": "10.0.0.1", "command": "show version"})
    assert result.is_error
    assert "Unknown device '10.0.0.1'" in result.content[0].text


@pytest.mark.anyio
async def test_collection_keeps_results_when_some_devices_fail(client: Client) -> None:
    async with client:
        result = await client.call_tool("lldp_neighbors", {})
    assert not result.is_error
    content = result.structured_content
    assert sorted(item["device"] for item in content["items"]) == ["leaf1", "leaf2"]
    errors = {error["device"]: error["error"] for error in content["errors"]}
    assert set(errors) == {"broken", "mystery"}
    assert "connection refused" in errors["broken"]
    assert "supported platforms: fake" in errors["mystery"]


@pytest.mark.anyio
async def test_one_device_name_is_accepted_where_a_list_is_expected(client: Client) -> None:
    async with client:
        result = await client.call_tool("lldp_neighbors", {"devices": "leaf1"})
    assert not result.is_error
    assert [item["device"] for item in result.structured_content["items"]] == ["leaf1"]


@pytest.mark.anyio
async def test_unsupported_query_is_reported_per_device(client: Client) -> None:
    async with client:
        result = await client.call_tool("mac_table", {"devices": ["leaf1"]})
    assert result.structured_content["errors"][0]["error"] == "fake does not support MAC tables yet."


@pytest.mark.anyio
async def test_ping_sweep_refuses_before_sending_anything(client: Client) -> None:
    async with client:
        outside = await client.call_tool("ping_sweep", {"network": "10.20.48.0/24"})
        too_big = await client.call_tool("ping_sweep", {"network": "198.18.0.0/21"})
    assert outside.is_error and "outside the inventory's sweep networks" in outside.content[0].text
    assert too_big.is_error and "the limit is 1024" in too_big.content[0].text


@pytest.mark.anyio
async def test_a_sweep_from_a_device_returns_what_answered_there(client: Client) -> None:
    FakePlatform.pings = []
    async with client:
        result = await client.call_tool("ping_sweep", {"network": "192.0.2.0/29", "device": "leaf1", "network_instance": "blue"})
    answered = [(ping["source"], ping["destination"]) for ping in result.structured_content["result"]]
    assert answered == [("leaf1", "192.0.2.1"), ("leaf1", "192.0.2.2")]
    # Every host address, once each (a device's pings are slow), inside the network instance asked for.
    assert sorted(FakePlatform.pings) == [("leaf1", f"192.0.2.{n}", 1, "blue") for n in range(1, 7)]


@pytest.mark.anyio
async def test_a_device_sweep_keeps_to_the_sweep_networks_and_a_smaller_limit(client: Client) -> None:
    async with client:
        outside = await client.call_tool("ping_sweep", {"network": "10.20.48.0/24", "device": "leaf1"})
        too_big = await client.call_tool("ping_sweep", {"network": "198.18.0.0/23", "device": "leaf1"})
        broken = await client.call_tool("ping_sweep", {"network": "192.0.2.0/29", "device": "broken"})
    assert outside.is_error and "outside the inventory's sweep networks" in outside.content[0].text
    assert too_big.is_error and "the limit from a device is 256" in too_big.content[0].text
    assert broken.is_error and "connection refused" in broken.content[0].text


@pytest.mark.anyio
async def test_a_ping_that_fails_is_tried_once_more(inventory: Inventory) -> None:
    class Flaky(FakePlatform):
        failures: dict[str, int] = {}

        async def ping(self, destination: str, count: int, network_instance: str) -> PingResult:
            if Flaky.failures.get(destination, 0) > 0:
                Flaky.failures[destination] -= 1
                raise PlatformError(f"{self.device.name}: JSON-RPC session limit reached")
            return await super().ping(destination, count, network_instance)

    device = inventory.get("leaf1")
    Flaky.failures = {"192.0.2.2": 1}
    async with Flaky(device, inventory.credentials_for(device)) as platform:
        results = await platform.ping_sweep(["192.0.2.1", "192.0.2.2", "192.0.2.3"], 1, "default")
    assert [result.received for result in results] == [1, 1, 0]
    Flaky.failures = {"192.0.2.2": 2}
    async with Flaky(device, inventory.credentials_for(device)) as platform:
        with pytest.raises(PlatformError, match="session limit"):
            await platform.ping_sweep(["192.0.2.1", "192.0.2.2", "192.0.2.3"], 1, "default")


def test_world_readable_password_file_is_refused(tmp_path: Path) -> None:
    password_file = tmp_path / "password"
    password_file.write_text("secret")
    password_file.chmod(0o644)
    inventory = Inventory.model_validate(
        {
            "credentials": {"lab": {"username": "admin", "password_file": str(password_file)}},
            "devices": [{"name": "leaf1", "host": "192.0.2.21", "platform": "fake", "credentials": "lab"}],
        }
    )
    with pytest.raises(InventoryError, match="chmod 600"):
        inventory.credentials_for(inventory.get("leaf1")).password()


@pytest.mark.anyio
async def test_topology_merges_every_device_and_reports_the_failures(client: Client) -> None:
    async with client:
        result = await client.call_tool("topology", {})
    graph = result.structured_content
    assert [(node["id"], node["kind"]) for node in graph["nodes"]] == [
        ("leaf1", "device"), ("leaf2", "device"), ("broken", "device"), ("mystery", "device"),
        ("spine1", "neighbor"), ("198.51.100.11", "host"), ("198.51.100.12", "host"),
    ]
    assert {error["device"] for error in graph["errors"]} == {"broken", "mystery"}
    # One error per device, though both of its queries failed.
    assert len(graph["errors"]) == 2


@pytest.mark.anyio
async def test_display_is_only_offered_to_a_client_that_can_draw(inventory: Inventory) -> None:
    async with Client(build_server(inventory, platforms={"fake": FakePlatform}, display=True)) as client:
        assert "display" in {tool.name for tool in (await client.list_tools()).tools}
        assert "map of the network" in (client.instructions or "")
        result = await client.call_tool("display", {"title": "leaf1's host", "focus": "198.51.100.11"})
        # gemma4:e4b once sent highlight as an object keyed by the names it meant.
        keyed = await client.call_tool("display", {"title": "Uplink", "highlight": {"leaf1--spine1": []}})
        refused = await client.call_tool("display", {"title": "Nope", "focus": ["leaf9"]})
    assert not result.is_error
    # The model reads a summary; the drawing goes to the page as structured content.
    assert result.content[0].text.startswith('The user now sees "leaf1\'s host"')
    view = result.structured_content
    assert view["emphasis"]["198.51.100.11"] == "focus" and view["emphasis"]["leaf1"] == "near"
    assert view["emphasis"]["leaf2"] == "context"
    assert refused.is_error and "'leaf9'" in refused.content[0].text
    assert keyed.structured_content["highlight"] == ["leaf1", "leaf1:ethernet-1/49--spine1", "spine1"]


@pytest.mark.anyio
async def test_forwarding_path_follows_routes_until_the_packet_leaves_the_inventory(client: Client) -> None:
    async with client:
        result = await client.call_tool("forwarding_path", {"source": "198.51.100.11", "destination": "203.0.113.5"})
        refused = await client.call_tool("forwarding_path", {"source": "nowhere", "destination": "203.0.113.5"})
    path = result.structured_content
    # leaf1's default route resolves through its /31 to ethernet-1/49, cabled to spine1, which NetGPT can't query.
    assert (path["outcome"], path["nodes"]) == ("left_inventory", ["198.51.100.11", "leaf1", "spine1"])
    assert path["hops"][1]["interfaces"] == ["ethernet-1/49.0"]
    assert refused.is_error and "'nowhere'" in refused.content[0].text


@pytest.mark.anyio
async def test_display_traces_a_path_and_marks_where_it_is_lost(inventory: Inventory) -> None:
    async with Client(build_server(inventory, platforms={"fake": FakePlatform}, display=True)) as client:
        result = await client.call_tool(
            "display", {"title": "Out of leaf1", "path": {"source": "198.51.100.11", "destination": "203.0.113.5"}}
        )
        # gemma4:e4b sometimes gives the two ends as a list.
        listed = await client.call_tool("display", {"title": "Out of leaf1", "path": ["198.51.100.11", "203.0.113.5"]})
        lopsided = await client.call_tool("display", {"title": "Hmm", "path": ["198.51.100.11"]})
    view = result.structured_content
    assert view["trouble"] == ["spine1"] and view["path"]["outcome"] == "left_inventory"
    assert set(view["highlight"]) >= {"198.51.100.11", "leaf1", "spine1"}
    assert view["notes"]["leaf1"] == "static 0.0.0.0/0"
    assert "Forwarding path, left_inventory:" in result.content[0].text
    assert listed.structured_content["path"] == view["path"]
    assert lopsided.is_error and "path" in lopsided.content[0].text


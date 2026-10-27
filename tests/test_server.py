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
from netgpt.server import build_server

from fakes import FakePlatform


@pytest.mark.anyio
async def test_every_tool_is_annotated_read_only(client: Client) -> None:
    async with client:
        tools = (await client.list_tools()).tools
    assert {tool.name for tool in tools} == {
        "list_devices", "show", "lldp_neighbors", "mac_table", "arp_table", "routes", "topology", "ping", "ping_sweep",
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


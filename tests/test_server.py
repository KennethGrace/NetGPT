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


@pytest.mark.anyio
async def test_every_tool_is_annotated_read_only(client: Client) -> None:
    async with client:
        tools = (await client.list_tools()).tools
    assert {tool.name for tool in tools} == {
        "list_devices", "show", "lldp_neighbors", "mac_table", "arp_table", "routes", "ping", "ping_sweep",
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
        result = await client.call_tool("arp_table", {"devices": ["leaf1"]})
    assert result.structured_content["errors"][0]["error"] == "fake does not support ARP tables yet."


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

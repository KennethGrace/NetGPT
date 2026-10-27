"""
Tests for the MCP server, driven through an in-process MCP client against a fake
platform, so the protocol layer is exercised without any devices.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from mcp import Client

from netgpt.guard import NOKIA_SRLINUX
from netgpt.inventory import Inventory, InventoryError
from netgpt.platforms import Platform, PlatformError
from netgpt.primitives import LldpNeighbor
from netgpt.server import build_server


class FakePlatform(Platform):
    """Answers from canned data; a device named 'broken' fails like an unreachable device."""

    policy = NOKIA_SRLINUX
    commands_run: list[str] = []

    async def _run_checked_cli(self, command: str) -> str:
        FakePlatform.commands_run.append(command)
        return f"{self.device.name}: ran {command}"

    async def lldp_neighbors(self) -> list[LldpNeighbor]:
        if self.device.name == "broken":
            raise PlatformError("broken: connection refused")
        return [LldpNeighbor(device=self.device.name, interface="ethernet-1/49", neighbor="spine1")]


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


@pytest.fixture
def inventory(tmp_path: Path) -> Inventory:
    password_file = tmp_path / "password"
    password_file.write_text("NotARealPassword\n")
    password_file.chmod(0o600)
    return Inventory.model_validate(
        {
            "credentials": {"lab": {"username": "admin", "password_file": str(password_file)}},
            "devices": [
                {"name": "leaf1", "host": "192.0.2.21", "platform": "fake", "credentials": "lab", "role": "leaf"},
                {"name": "leaf2", "host": "192.0.2.22", "platform": "fake", "credentials": "lab", "role": "leaf"},
                {"name": "broken", "host": "192.0.2.99", "platform": "fake", "credentials": "lab"},
                {"name": "mystery", "host": "192.0.2.98", "platform": "juniper_junos", "credentials": "lab"},
            ],
            "sweep_networks": ["192.0.2.0/24", "198.18.0.0/15"],
        }
    )


@pytest.fixture
def client(inventory: Inventory) -> Client:
    FakePlatform.commands_run = []
    return Client(build_server(inventory, platforms={"fake": FakePlatform}))


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
async def test_show_refuses_write_and_explains(client: Client) -> None:
    async with client:
        result = await client.call_tool("show", {"device": "leaf1", "command": "info > /tmp/exfil"})
    assert result.is_error
    assert "'>' is not allowed" in result.content[0].text
    assert FakePlatform.commands_run == []


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

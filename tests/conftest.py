"""
Shared fixtures: an inventory of fake devices, and an in-process MCP client for a
NetGPT server built on a fake platform, so the protocol layer is exercised
without any devices.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from mcp import Client

from netgpt.inventory import Inventory
from netgpt.server import build_server

from fakes import FakePlatform


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
def commands_run() -> list[str]:
    """The CLI commands the fake platform has run during this test."""
    FakePlatform.commands_run = []
    return FakePlatform.commands_run


@pytest.fixture
def client(inventory: Inventory, commands_run: list[str]) -> Client:
    return Client(build_server(inventory, platforms={"fake": FakePlatform}))

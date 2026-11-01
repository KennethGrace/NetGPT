"""
Tests for the engine on its own, with no face in front of it: it reads the network,
and refuses what it won't do with errors of its own, for each face to translate (the
MCP server into a tool error, the HTTP API into a refusal).
"""

from __future__ import annotations

import asyncio

import pytest

from netgpt.engine import Engine, RequestError
from netgpt.guard import GuardError
from netgpt.inventory import Inventory, InventoryError
from netgpt.statistics import Poller

from fakes import FakePlatform


@pytest.fixture
def engine(inventory: Inventory) -> Engine:
    return Engine(inventory, platforms={"fake": FakePlatform})


@pytest.mark.anyio
async def test_the_engine_reads_the_network_without_any_face(engine: Engine) -> None:
    topology = await engine.topology()
    assert [(node.id, node.kind) for node in topology.nodes] == [
        ("leaf1", "device"), ("leaf2", "device"), ("broken", "device"), ("mystery", "device"),
        ("spine1", "neighbor"), ("198.51.100.11", "host"), ("198.51.100.12", "host"),
    ]
    path = await engine.forwarding_path("198.51.100.11", "203.0.113.5")
    assert (path.outcome, path.nodes) == ("left_inventory", ["198.51.100.11", "leaf1", "spine1"])
    view = await engine.view("leaf1's host", focus=["198.51.100.11"])
    assert view.emphasis["198.51.100.11"] == "focus" and view.emphasis["leaf1"] == "near"
    assert [device.name for device in engine.devices()] == ["leaf1", "leaf2", "broken", "mystery"]


@pytest.mark.anyio
async def test_the_engine_refuses_with_errors_of_its_own_before_asking_a_device(engine: Engine, commands_run: list[str]) -> None:
    with pytest.raises(InventoryError, match="outside the inventory's sweep networks"):
        await engine.ping_sweep("10.20.48.0/24")
    with pytest.raises(RequestError, match="the limit is 1024"):
        await engine.ping_sweep("198.18.0.0/21")
    with pytest.raises(RequestError):
        await engine.ping_sweep("not a network")
    with pytest.raises(GuardError):
        await engine.show("leaf1", "info > /tmp/exfil")
    with pytest.raises(RequestError, match="isn't polling"):
        await engine.traffic()
    assert commands_run == []


@pytest.mark.anyio
async def test_an_engine_polls_only_while_a_face_holds_it_running(inventory: Inventory, counter_reads: dict[str, int]) -> None:
    async with Engine(inventory, platforms={"fake": FakePlatform}).running():
        pass  # An engine with no poller runs without one.
    poller = Poller(inventory, platforms={"fake": FakePlatform}, interval=0.01)
    engine = Engine(inventory, platforms={"fake": FakePlatform}, statistics=poller)
    async with engine.running():
        await asyncio.sleep(0.05)
    reads = counter_reads["leaf1"]
    assert reads >= 2
    await asyncio.sleep(0.05)
    assert counter_reads["leaf1"] == reads

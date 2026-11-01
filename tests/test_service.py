"""
Tests for netgpt-engine's service: the engine's API at /api/v1, for the token, with
the engine polling for as long as the service runs. Starlette's TestClient runs the
service's lifespan, as uvicorn would.
"""

from __future__ import annotations

import argparse
import time

import pytest
from starlette.testclient import TestClient

from netgpt.engine import Engine
from netgpt.inventory import Inventory
from netgpt.service import build_service, listen_address
from netgpt.statistics import Poller

from fakes import FakePlatform

TOKEN = "correct-horse-battery-staple"
BEARER = {"Authorization": f"Bearer {TOKEN}"}


def test_the_service_serves_the_api_and_polls_for_as_long_as_it_runs(inventory: Inventory, counter_reads: dict[str, int]) -> None:
    poller = Poller(inventory, platforms={"fake": FakePlatform}, interval=0.02)
    service = build_service(Engine(inventory, platforms={"fake": FakePlatform}, statistics=poller), TOKEN)
    with TestClient(service) as client:
        devices = client.get("/api/v1/devices", headers=BEARER)
        assert devices.status_code == 200 and [device["name"] for device in devices.json()["items"]][:2] == ["leaf1", "leaf2"]
        # No client of any other kind is connected: the service itself keeps the engine polling.
        time.sleep(0.2)
        assert counter_reads["leaf1"] >= 3
        traffic = client.get("/api/v1/traffic", headers=BEARER).json()
        polled = {(port["device"], port["interface"]) for port in traffic["ports"]}
        assert polled == {("leaf1", "ethernet-1/1"), ("leaf2", "ethernet-1/1")}
    reads = counter_reads["leaf1"]
    time.sleep(0.1)
    assert counter_reads["leaf1"] == reads


def test_the_service_answers_nothing_without_the_token(inventory: Inventory) -> None:
    with TestClient(build_service(Engine(inventory, platforms={"fake": FakePlatform}), TOKEN)) as client:
        assert client.get("/api/v1/devices").status_code == 401
        assert client.get("/api/v1/devices", headers={"Authorization": "Bearer guess"}).status_code == 401


def test_an_address_to_listen_on_is_on_loopback_unless_it_names_a_host() -> None:
    assert listen_address("8081") == ("127.0.0.1", 8081)
    assert listen_address("100.69.0.11:8081") == ("100.69.0.11", 8081)
    assert listen_address("[::1]:8081") == ("::1", 8081)
    for wrong in ("host", "host:http", "8081:", "0", "65536"):
        with pytest.raises(argparse.ArgumentTypeError):
            listen_address(wrong)

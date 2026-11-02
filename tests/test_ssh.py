"""
Tests for NetGPT's SSH logins: no more than SESSIONS_PER_DEVICE connections to
one device at a time, whatever asks, and a place given back however a login ends.
"""

from __future__ import annotations

import asyncio
from collections import Counter

import asyncssh
import pytest

from netgpt.inventory import Credentials, Device
from netgpt.platforms.base import PlatformError
from netgpt.platforms.ssh import SESSIONS_PER_DEVICE, SshLogin

pytestmark = pytest.mark.anyio

# While it holds anything, every login is refused.
refusing: set[str] = set()


class FakeConnection:
    def close(self) -> None:
        pass

    async def wait_closed(self) -> None:
        pass


@pytest.fixture
def logins(monkeypatch: pytest.MonkeyPatch) -> Counter[str]:
    """Stand in for asyncssh.connect, and count how many connections are open to each host at the most."""
    monkeypatch.setenv("NETGPT_TEST_PASSWORD", "not-a-real-one")
    refusing.clear()
    open_now: Counter[str] = Counter()
    most: Counter[str] = Counter()

    async def connect(host: str, **options: object) -> FakeConnection:
        if refusing:
            raise asyncssh.PermissionDenied("no")
        open_now[host] += 1
        most[host] = max(most[host], open_now[host])
        connection = FakeConnection()
        original_close = connection.close

        def close() -> None:
            open_now[host] -= 1
            original_close()

        connection.close = close  # type: ignore[method-assign]
        return connection

    monkeypatch.setattr(asyncssh, "connect", connect)
    return most


def login(host: str) -> SshLogin:
    device = Device(name=f"switch-{host}", host=host, platform="cisco_nxos", credentials="lab")
    return SshLogin(device, Credentials(username="netgpt", password_env="NETGPT_TEST_PASSWORD"))


async def test_no_more_than_a_few_connections_to_one_device_are_open_at_once(logins: Counter[str]) -> None:
    async def query(host: str) -> None:
        session = login(host)
        await session.connection()
        await asyncio.sleep(0.01)
        await session.close()

    await asyncio.gather(*(query("192.0.2.1") for _ in range(6)), *(query("192.0.2.2") for _ in range(2)))
    assert logins["192.0.2.1"] == SESSIONS_PER_DEVICE
    assert logins["192.0.2.2"] == 2


async def test_a_refused_login_gives_its_place_back(logins: Counter[str]) -> None:
    refusing.add("yes")
    for _ in range(SESSIONS_PER_DEVICE + 1):
        with pytest.raises(PlatformError, match="refused the SSH login for netgpt"):
            await login("192.0.2.4").connection()
    refusing.clear()
    # Had the refusals kept their places, these would wait for ever.
    await asyncio.wait_for(asyncio.gather(*(login("192.0.2.4").connection() for _ in range(SESSIONS_PER_DEVICE))), 1)


async def test_closing_a_login_twice_gives_its_place_back_once(logins: Counter[str]) -> None:
    first = login("192.0.2.3")
    await first.connection()
    await first.close()
    await first.close()
    others = [login("192.0.2.3") for _ in range(SESSIONS_PER_DEVICE)]
    await asyncio.wait_for(asyncio.gather(*(other.connection() for other in others)), 1)
    late = login("192.0.2.3")
    with pytest.raises(TimeoutError):
        await asyncio.wait_for(late.connection(), 0.05)

"""
The ssh module opens NetGPT's SSH connections to devices.

A login uses the inventory's key or password and nothing else (no ~/.ssh/config,
no agent), and refuses a device whose host key isn't the one its known hosts file
pins. NetGPT opens a connection per query, and a graph asks several tables of
every device at once, so no more than SESSIONS_PER_DEVICE connections to one
device are open at a time: devices on a shared network have only so many VTY
lines, and the people who use them need some too.
"""

from __future__ import annotations

import asyncio
import logging
import weakref

import asyncssh

from netgpt.inventory import Credentials, Device, InventoryError
from netgpt.platforms.base import PlatformError

# asyncssh logs every connection at INFO, and NetGPT opens one per query.
logging.getLogger("asyncssh").setLevel(logging.WARNING)

# Seconds to wait for a login, or for one reply.
TIMEOUT = 60.0
# How many connections NetGPT keeps open to one device at once. The rest wait their turn.
SESSIONS_PER_DEVICE = 2

# A semaphore belongs to the event loop it was first used in, so each loop keeps its own.
_gates: weakref.WeakKeyDictionary[asyncio.AbstractEventLoop, dict[str, asyncio.Semaphore]] = weakref.WeakKeyDictionary()


def gate(host: str) -> asyncio.Semaphore:
    """The semaphore that limits NetGPT's connections to one host, in the running event loop."""
    gates = _gates.setdefault(asyncio.get_running_loop(), {})
    return gates.setdefault(host, asyncio.Semaphore(SESSIONS_PER_DEVICE))


class SshLogin:
    """
    One SSH connection to one device, opened when first asked for. It holds one of
    the device's SESSIONS_PER_DEVICE places from the moment it starts to log in
    until it is closed.
    """

    def __init__(self, device: Device, credentials: Credentials) -> None:
        self._device = device
        self._credentials = credentials
        self._connection: asyncssh.SSHClientConnection | None = None
        self._place: asyncio.Semaphore | None = None
        self._lock = asyncio.Lock()

    async def connection(self) -> asyncssh.SSHClientConnection:
        """The open connection, logging in first if there isn't one."""
        async with self._lock:
            if self._connection is None:
                await self._open()
            assert self._connection is not None
            return self._connection

    async def _open(self) -> None:
        device, credentials = self._device, self._credentials
        known_hosts = device.known_hosts.expanduser() if device.known_hosts else None
        if known_hosts is not None and not known_hosts.is_file():
            raise InventoryError(f"Known hosts file {known_hosts} for {device.name} does not exist.")
        if credentials.ssh_key_file is not None:
            secret = {"client_keys": [str(credentials.ssh_key())], "password": None}
        else:
            secret = {"client_keys": None, "password": credentials.password()}
        place = gate(device.host)
        await place.acquire()
        self._place = place
        try:
            # No ~/.ssh/config and no agent: the inventory says how to log in, and nothing else does.
            self._connection = await asyncio.wait_for(
                asyncssh.connect(
                    device.host,
                    username=credentials.username,
                    known_hosts=str(known_hosts) if known_hosts else (),
                    config=None,
                    agent_path=None,
                    **secret,
                ),
                TIMEOUT,
            )
        except asyncssh.PermissionDenied:
            await self.close()
            raise PlatformError(f"{device.name}: the device refused the SSH login for {credentials.username}.") from None
        except asyncssh.HostKeyNotVerifiable:
            await self.close()
            where = known_hosts or "~/.ssh/known_hosts"
            raise PlatformError(f"{device.name}: its SSH host key isn't the one in {where}, so NetGPT won't log in.") from None
        except (OSError, asyncssh.Error, TimeoutError) as error:
            await self.close()
            raise PlatformError(f"{device.name}: {type(error).__name__}: {error}") from error
        except BaseException:
            await self.close()
            raise

    async def close(self) -> None:
        """Close the connection, if one is open, and give up its place."""
        connection, self._connection = self._connection, None
        place, self._place = self._place, None
        try:
            if connection is not None:
                connection.close()
                await connection.wait_closed()
        finally:
            if place is not None:
                place.release()

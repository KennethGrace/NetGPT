"""Tests for the read-only CLI policies."""

from __future__ import annotations

import pytest

from netgpt.guard import CISCO_IOS, NOKIA_SRLINUX, GuardError


@pytest.mark.parametrize(
    "command",
    [
        "show version",
        "sh ip int br",
        "show running-config | include hostname",
        "show ip route | i 0.0.0.0",
        "show interfaces | section GigabitEthernet0/1",
        "  show clock  ",
    ],
)
def test_ios_allows_read_only(command: str) -> None:
    assert CISCO_IOS.check(command) == command.strip()


@pytest.mark.parametrize(
    "command",
    [
        "configure terminal",
        "reload",
        "copy running-config startup-config",
        "show running-config | redirect flash:exfil.txt",
        "show tech-support | tee flash:tech.txt",
        "show running-config | append flash:x",
        "show version\nconfigure terminal",
        "show version\rreload",
        "show version |",
        "",
        "showconfig",
    ],
)
def test_ios_refuses_writes_and_chaining(command: str) -> None:
    with pytest.raises(GuardError):
        CISCO_IOS.check(command)


@pytest.mark.parametrize(
    "command",
    [
        "show version",
        "show interface brief",
        "info from state /system/lldp",
        "info flat /network-instance default",
        "show network-instance default route-table | as json",
        "info from state /interface | grep oper-state",
    ],
)
def test_srlinux_allows_read_only(command: str) -> None:
    assert NOKIA_SRLINUX.check(command) == command


@pytest.mark.parametrize(
    "command",
    [
        "enter candidate",
        "tools system reboot",
        "info > /tmp/config.txt",
        "info >> /tmp/config.txt",
        "show version; enter candidate",
        "show version && tools system reboot",
        "show version | save /tmp/x",
        "bash",
        "show version\ncommit now",
    ],
)
def test_srlinux_refuses_writes_and_chaining(command: str) -> None:
    with pytest.raises(GuardError):
        NOKIA_SRLINUX.check(command)

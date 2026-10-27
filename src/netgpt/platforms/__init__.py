"""
The platforms package maps platform names to the classes that drive them.

Platform names match containerlab's node kinds, so a lab's inventory can use the
kind directly.
"""

from __future__ import annotations

from netgpt.platforms.base import Platform, PlatformError, UnsupportedError
from netgpt.platforms.srlinux import SRLinuxPlatform

PLATFORMS: dict[str, type[Platform]] = {
    "nokia_srlinux": SRLinuxPlatform,
}

__all__ = ["PLATFORMS", "Platform", "PlatformError", "UnsupportedError"]

"""
The platforms package maps platform names to the classes that drive them.

Platform names match containerlab's node kinds, so a lab's inventory can use the
kind directly.
"""

from __future__ import annotations

from netgpt.inventory import Inventory, InventoryError
from netgpt.platforms.base import Platform, PlatformError, UnsupportedError
from netgpt.platforms.srlinux import SRLinuxPlatform

PLATFORMS: dict[str, type[Platform]] = {
    "nokia_srlinux": SRLinuxPlatform,
}


def platform_for(inventory: Inventory, name: str, platforms: dict[str, type[Platform]] = PLATFORMS) -> type[Platform]:
    """The class that drives an inventory device, found without resolving its credentials."""
    device = inventory.get(name)
    platform = platforms.get(device.platform)
    if platform is None:
        supported = ", ".join(sorted(platforms))
        raise InventoryError(f"Device {name} has platform '{device.platform}'; supported platforms: {supported}.")
    return platform


def connect(inventory: Inventory, name: str, platforms: dict[str, type[Platform]] = PLATFORMS) -> Platform:
    """Open a connection to an inventory device; use it as an async context manager."""
    device = inventory.get(name)
    return platform_for(inventory, name, platforms)(device, inventory.credentials_for(device))


__all__ = ["PLATFORMS", "Platform", "PlatformError", "UnsupportedError", "connect", "platform_for"]

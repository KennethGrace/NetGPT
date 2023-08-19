"""
The Clients module defines the interface for connecting to and
discovering Network Infrastructure devices.
"""
from __future__ import annotations

from typing import List, Type
from clients import cisco_ios, cisco_nxos

from clients.client import NetworkDevicePlatform

# Official list of supported network device platforms. This is used to
# control the available options in the UI.
_network_devices: List[Type[NetworkDevicePlatform]] = [
    cisco_ios.CiscoIOSPlatform,
    cisco_nxos.CiscoNXOSPlatform,
]


def get_network_device_platforms() -> List[Type[NetworkDevicePlatform]]:
    """
    The getNetworkDevices function returns a list of all supported network device handlers.
    """
    return _network_devices


def get_network_device_platform(deviceType: str) -> Type[NetworkDevicePlatform] | None:
    """
    The get_network_device_platform function returns the first device handler that matches the
    specified device type string.
    """
    return next((device for device in _network_devices if device.vendor == deviceType), None)

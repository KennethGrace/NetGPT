"""
The Clients module defines the interface for connecting to and
discovering Network Infrastructure devices.
"""
from __future__ import annotations

from typing import Dict, Type

from clients import cisco_ios, cisco_nxos
from clients.schema import DeviceType, NetworkDevicePlatform

_network_devices: Dict[DeviceType, Type[NetworkDevicePlatform]] = {
    DeviceType.CISCO_IOS: cisco_ios.CiscoIOSPlatform,
    DeviceType.CISCO_NXOS: cisco_nxos.CiscoNXOSPlatform,
}


def get_network_device_platforms() -> Dict[DeviceType, Type[NetworkDevicePlatform]]:
    """
    The getNetworkDevices function returns a list of all supported network device handlers.
    """
    return _network_devices


def get_network_device_platform(deviceType: DeviceType) -> Type[NetworkDevicePlatform] | None:
    """
    The get_network_device_platform function returns the first device handler that matches the
    specified device type string.
    """
    return _network_devices.get(deviceType)

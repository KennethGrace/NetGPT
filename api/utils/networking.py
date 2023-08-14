from typing import List, Type
from clients import cisco_ios, cisco_nxos

from interfaces.client import NetworkDevice

NetworkDevices: List[Type[NetworkDevice]] = [
    cisco_ios.CiscoIOSDeviceHandler,
    cisco_nxos.CiscoNXOSDeviceHandler,
]


def getNetworkDevices() -> List[Type[NetworkDevice]]:
    """
    The getNetworkDevices function returns a list of all supported network device handlers.
    """
    return NetworkDevices


def getNetworkDevice(deviceType: str) -> Type[NetworkDevice] | None:
    """
    The getDeviceHandler function returns the first device handler that matches the
    specified device type.
    """
    return next((device for device in NetworkDevices if device.vendor == deviceType), None)

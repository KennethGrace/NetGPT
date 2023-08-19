"""
The Juniper JunOS module defines the interface for connecting to and
discovering Juniper JunOS devices.
"""

from netmiko import ConnectHandler
from napalm import get_network_driver
from clients.client import NetworkDevicePlatform, NetworkSettings


class JuniperJunOSDeviceHandlerPlatform(NetworkDevicePlatform):
    vendor = "Juniper JunOS"

    def __init__(self, settings: NetworkSettings):
        super().__init__(settings)
        self.driver = get_network_driver("junos")

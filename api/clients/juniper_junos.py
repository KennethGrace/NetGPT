"""
The Juniper JunOS module defines the interface for connecting to and
discovering Juniper JunOS devices.
"""

from netmiko import ConnectHandler
from napalm import get_network_driver
from clients.schema import NetworkSettings, NetworkDevicePlatform


class JuniperJunOSDeviceHandlerPlatform(NetworkDevicePlatform):
    vendor = "Juniper JunOS"

    def __init__(self, settings: NetworkSettings):
        super().__init__(settings)
        self.driver = get_network_driver("junos")

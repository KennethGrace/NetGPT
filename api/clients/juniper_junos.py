"""
The Juniper JunOS module defines the interface for connecting to and
discovering Juniper JunOS devices.
"""

from netmiko import ConnectHandler
from napalm import get_network_driver
from napalm.ios import IOSDriver

from interfaces.client import NetworkDevice, ConnectionParameters

class CiscoIOSDeviceHandler(NetworkDevice):
    def __init__(self, hostname: str, connectionParameters: ConnectionParameters):
        super().__init__(hostname, connectionParameters)
        self.driver = get_network_driver("ios")
        
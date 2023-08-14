"""
The Cisco NX-OS module defines the interface for connecting to and
discovering Cisco NX-OS devices.
"""

from typing import Any
from netmiko import ConnectHandler
from napalm import get_network_driver
import json

from interfaces.client import NetworkDevice, ConnectionParameters, DeviceCapability, Property

class CiscoNXOSDeviceHandler(NetworkDevice):

    vendor = "Cisco NX-OS"

    def __init__(self, connectionParameters: ConnectionParameters):
        super().__init__(connectionParameters)
        if connectionParameters is None:
            raise Exception("Connection Parameters not set.")
        self.driver = get_network_driver("nxos")


@CiscoNXOSDeviceHandler.register_capability(
    description="Execute a CLI \"show\" command on Cisco NXOS devices.",
    properties={
        "hostnames": Property(
            type="array",
            description="The hostnames of the devices to execute the command on.",
            items={"type": "string"},
        ),
        "command": Property(
            type="string",
            description="The command to execute on the device.",
        ),
    },
)
def execute_command(
    self: CiscoNXOSDeviceHandler, hostnames: list[str], command: str
) -> dict[str, str]:
    """
    The execute_command function executes the specified command on the device.
    """
    device_outputs = {}
    connect_handlers = [
        ConnectHandler(
            device_type="cisco_ios",
            host=host,
            username=self.connectionParameters.username,
            password=self.connectionParameters.password,
        )
        for host in hostnames
    ]
    for connect_handler in connect_handlers:
        with connect_handler as device:
            device.enable()
            output = device.send_command(command)
            device_outputs.update({connect_handler.host: output})
    return device_outputs

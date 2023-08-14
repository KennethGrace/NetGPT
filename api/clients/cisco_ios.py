"""
The Cisco IOS module defines the interface for connecting to and
discovering Cisco IOS devices.
"""
from __future__ import annotations
from typing import List

from netmiko import ConnectHandler
from netmiko.exceptions import NetmikoTimeoutException
from napalm import get_network_driver
from napalm.ios import IOSDriver

from interfaces.client import NetworkDevice, ConnectionParameters, Property


class CiscoIOSDeviceHandler(NetworkDevice):
    vendor = "Cisco IOS"

    def __init__(self, connectionParameters: ConnectionParameters):
        super().__init__(connectionParameters)
        if connectionParameters is None:
            raise Exception("Connection Parameters not set.")
        self.driver = get_network_driver("ios")


@CiscoIOSDeviceHandler.register_capability(
    description='Execute a CLI "show" command on Cisco IOS devices.',
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
    self: CiscoIOSDeviceHandler, hostnames: list[str], command: str
) -> dict[str, str]:
    """
    The execute_command function executes the specified command on the device.
    """
    if not command.startswith("show"):
        raise Exception("Only show commands are supported.")
    device_outputs = {}
    for host in hostnames:
        try:
            with ConnectHandler(
                device_type="cisco_ios",
                host=host,
                username=self.connectionParameters.username,
                password=self.connectionParameters.password,
            ) as device:
                device.enable()
                output = device.send_command(command)
                if not output:
                    output = "No output from command."
                device_outputs.update(
                    {
                        host: {
                            "command": command,
                            "output": output,
                        }
                    }
                )
        except NetmikoTimeoutException as e:
            device_outputs.update(
                {
                    host: {
                        "error connecting": str(e),
                    }
                }
            )
    return device_outputs


@CiscoIOSDeviceHandler.register_capability(
    description="Get LLDP Neighbors",
    properties={
        "hostnames": Property(
            type="array",
            description="The hostnames of the devices to get LLDP neighbors from.",
            items={"type": "string"},
        )
    },
)
def get_lldp_neighbors(
    self: CiscoIOSDeviceHandler, hostnames: list[str]
) -> dict[str, dict]:
    """
    The get_lldp_neighbors function returns a dictionary of the LLDP neighbors
    for the device.
    """
    device_outputs = {}
    for host in hostnames:
        with self.driver(
            hostname=host,
            username=self.connectionParameters.username,
            password=self.connectionParameters.password,
        ) as device:
            device_outputs.update({host: device.get_lldp_neighbors()})
    return device_outputs

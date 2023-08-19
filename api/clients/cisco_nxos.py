"""
The Cisco NX-OS module defines the interface for connecting to and
discovering Cisco NX-OS devices.
"""

from __future__ import annotations

from typing import Any

from netmiko import ConnectHandler
from napalm import get_network_driver

from capabilities import Capability, Property
from clients.client import NetworkDevicePlatform, NetworkSettings


class CiscoNXOSPlatform(NetworkDevicePlatform):
    """
    The CiscoNXOSPlatform class defines the interface for connecting to and
    troubleshooting Cisco NX-OS devices.
    """
    vendor = "Cisco NXOS"

    def __init__(self, settings: NetworkSettings):
        super().__init__(settings)
        if settings is None:
            raise Exception("Connection Parameters not set.")
        self.driver = get_network_driver("nxos")

    @Capability.make(
        description="Gather logging information from Cisco NXOS devices.",
        properties={
            "hostnames": Property(
                type="array",
                description="The hostnames of the devices to gather logging information from.",
                items={"type": "string"},
            ),
            "severity": Property(
                type="string",
                description="The severity of the logs to gather.",
                enum=["info", "notify", "warning", "error"],
            ),
        },
    )
    def get_logs(self: CiscoNXOSPlatform, hostnames: list[str], severity: str) -> dict[str, str]:
        """
        The get_logs function gathers logging information from the device.
        """
        # Map the severity to a regex expression for identifying if a log is of that severity.
        mapping = {
            "info": ".*-(7|6|5)-.*",
            "notify": ".*-(5|4|3)-.*",
            "warning": ".*-(4|3|2)-.*",
            "error": ".*-(3|2|1|0)-.*",
        }
        severity_exp = mapping[severity]
        device_outputs = {}
        for host in hostnames:
            with ConnectHandler(
                device_type="cisco_ios",
                host=host,
                username=self.settings.username,
                password=self.settings.password,
            ) as device:
                device.enable()
                output = device.send_command(f"show logging | egrep \"{severity_exp}\"")
                if not output:
                    output = "No logs found matching for severity level, " + severity + "."
                # Slice the output number of lines to only include the last 100 lines.
                output = "\n".join(output.split("\n")[-100:])
                device_outputs.update({host: output})
        return device_outputs

    @Capability.make(
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
        self: CiscoNXOSPlatform, hostnames: list[str], command: str
    ) -> dict[str, Any]:
        """
        The execute_command function executes the specified command on the device.
        """
        device_outputs = {}
        connect_handlers = [
            ConnectHandler(
                device_type="cisco_ios",
                host=host,
                username=self.settings.username,
                password=self.settings.password,
            )
            for host in hostnames
        ]
        for connect_handler in connect_handlers:
            with connect_handler as device:
                device.enable()
                output = device.send_command(command)
                device_outputs.update({connect_handler.host: output})
        return device_outputs

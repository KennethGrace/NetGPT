"""
The Pings module defines the capabilities for sending ICMP echo requests
at scale to a list of IP addresses.
"""
from __future__ import annotations

import asyncio
import ipaddress
from typing import List, Any
import logging

import icmplib

from capabilities import Property, Capability
from plugins.plugin import Plugin
from schema import PluginSettings

logger = logging.getLogger("uvicorn")


class PingsSettings(PluginSettings):
    """
    The PingsSettings class defines the data model for settings used by the Pings plugin.
    """

    name: str = "Pings"
    description: str = "Pings a network of IP addresses."
    fields: dict[str, str] = {
        "Count": "3",
        "Timeout": "3",
    }
    enabled: bool = True


class PingsPlugin(Plugin):
    """
    The PingsPlugin class defines the Pings plugin.
    """

    @classmethod
    def get_settings(cls) -> PingsSettings:
        """
        The getSettings method returns the settings model for the Pings plugin.
        """
        return PingsSettings()

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)

    @Capability.make(
        description="Ping a network of IP addresses for active IPs",
        properties={
            "ip_address": Property(
                description="An IP address in the network to ping",
                type="string"),
            "cidr": Property(
                description="The CIDR notation of the IP network to ping",
                type="integer",
                required=False),
        }
    )
    def ping(self, ip_address: str, cidr: str = None) -> dict[str, Any]:
        """
        The ping function sends ICMP echo requests to all hosts in the specified network.
        The network is indicated by any IP address in the network and the CIDR notation of
        the network. The results are returned as a dictionary of Active IP addresses to the
        results of the ICMP echo request.
        """
        # Compute the subnetwork from the IP address and CIDR notation
        if cidr is None:
            cidr = 32
        net = ipaddress.ip_network(f"{ip_address}/{str(cidr)}", strict=False)
        logger.info(f"Pinging network {net}")
        count = int(self.settings.fields['Count'])
        timeout = int(self.settings.fields['Timeout'])
        # Send ICMP echo requests to all hosts in the network
        hosts = asyncio.run(
            ping_network(net, count=count, timeout=timeout)
        )
        active_hosts = {
            host.address: {
                "rtt_avg": host.avg_rtt if host.is_alive else None,
                "packet_loss": f'{host.packet_loss}%' if host.is_alive else None,
            }
            for host in hosts if host.is_alive
        }
        # Return the results
        return active_hosts if len(active_hosts) > 0 else "No active hosts found."


async def ping_network(net: ipaddress.IPv4Network, count: int = 3, timeout: int = 1) -> List[icmplib.Host]:
    """
    The ping_network function sends ICMP echo requests to all hosts in the specified network.
    """
    ips = [str(ip) for ip in net]
    replies = await icmplib.async_multiping(addresses=ips, count=count, timeout=1,
                                            privileged=False, concurrent_tasks=128)
    return replies

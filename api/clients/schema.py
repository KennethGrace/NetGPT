from __future__ import annotations

from abc import ABC, abstractmethod
from typing import List

from pydantic import BaseModel
from enum import Enum

from capabilities import Capability


class DeviceType(str, Enum):
    CISCO_IOS = "Cisco IOS"
    CISCO_NXOS = "Cisco NXOS"


# Link the DeviceType enum to the device handlers for each device type.
class NetworkDevicePlatform(ABC):
    """
    NetworkDevicePlatform defines the interface for connecting to and
    discovering Network Infrastructure devices. It is an abstract
    base class that must be implemented by all device handlers. It
    requires that the vendor property be implemented and that
    NetworkSettings be passed to the constructor.

    Capabilities are used to determine which interactions can be
    performed on a device and are defined by decorating a Capability
    with the register_capability decorator.
    """

    @property
    @abstractmethod
    def vendor(self) -> str: ...

    def __init__(self, settings: NetworkSettings):
        self.settings = settings

    @classmethod
    def get_capabilities(cls) -> list[Capability]:
        """
        The get_capabilities function returns a list of all capabilities
        that are supported by the device. To do this we iterate over all
        the methods of the class and check if they are of type Capability.
        """
        return [
            getattr(cls, method)
            for method in dir(cls)
            if isinstance(getattr(cls, method), Capability)
        ]


class NetworkSettings(BaseModel):
    username: str
    password: str
    deviceType: str
    enablePassword: str = None


class DeviceOptions(BaseModel):
    """
    The DeviceOptions class defines the data model for all the available DeviceTypes.
    """
    options: List[DeviceType]



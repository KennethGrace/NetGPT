"""
The Libraries module defines the interface for connecting to and
discovering Network Infrastructure devices.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from pydantic import BaseModel

from capabilities import Capability


class NetworkSettings(BaseModel):
    username: str
    password: str
    deviceType: str
    enablePassword: str = None


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

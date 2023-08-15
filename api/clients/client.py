"""
The Libraries module defines the interface for connecting to and
discovering Network Infrastructure devices.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, List, Generic, Type, TypeVar
from abc import ABC, abstractmethod
from pydantic import BaseModel
import json

from interfaces import Capability, Property


@dataclass
class Property:
    """
    The Property class defines a model for a property that is passed to a
    DeviceCapability.
    """

    type: str
    description: str = None
    enum: list[str] = None
    items: list[str] = None


class ConnectionParameters(BaseModel):
    username: str
    password: str
    deviceType: str
    enablePassword: str = None


@dataclass
class DeviceCapability(Capability):
    """
    DeviceCapability defines a model for a capability that a device
    can support. Capabilities are used to determine which interactions
    can be performed on a device. A DeviceCapability is a decorator
    that is applied to a function that performs an interaction with
    a device.
    """

    name: str
    description: str
    capability: Callable[[NetworkDevice], Any]
    properties: dict[str, Property] = None

    def __call__(self, nd: NetworkDevice, *args: Any, **kwds: Any) -> Any:
        return self.capability(nd, *args, **kwds)
    
    def wrap(self, nd: NetworkDevice, func: Callable[[NetworkDevice], Any]):
        """
        wrap is a function that wraps a function with a DeviceCapability
        and returns a DeviceCapability.
        """
        self.capability = lambda *args, **kwds: func(nd, *args, **kwds)
        return self


class NetworkDevice(ABC):
    """
    NetworkDevice defines the interface for connecting to and
    discovering Network Infrastructure devices. It is an abstract
    base class that must be implemented by all device handlers. It
    requires that the vendor property be implemented and that
    ConnectionParameters be passed to the constructor.

    Capabilities are used to determine which interactions can be
    performed on a device and are defined by decorating a function
    with the register_capability decorator.
    """
    capabilities: List[DeviceCapability] = []

    def __init__(self, connectionParameters: ConnectionParameters):
        self.connectionParameters = connectionParameters

    @classmethod
    def get_capabilities(cls) -> list[DeviceCapability]:
        return cls.capabilities

    @property
    @classmethod
    @abstractmethod
    def vendor(self) -> str:
        ...

    @classmethod
    def register_capability(
        cls, description: str = None, properties: dict[str, Property] = None
    ) -> Callable[[NetworkDevice], Any]:
        """
        register_capability is a decorator that is applied to a function
        that performs an interaction with a device. The decorator adds
        the function to the list of capabilities that the device supports
        allowing the function to be called by a LanguageFlow.
        """

        def func_wrapper(callable: Callable[[NetworkDevice], Any]):
            cls.capabilities.append(
                DeviceCapability(
                    name=callable.__name__,
                    capability=callable,
                    description=description,
                    properties=properties,
                )
            )
            return callable

        return func_wrapper

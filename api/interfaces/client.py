"""
The Libraries module defines the interface for connecting to and
discovering Network Infrastructure devices.
"""
from __future__ import annotations
from collections import namedtuple

from dataclasses import dataclass
from typing import Any, Callable, List, Generic, Type, TypeVar
from abc import ABC, abstractmethod
from pydantic import BaseModel
import json


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
class DeviceCapability:
    """
    DeviceCapability defines a model for a capability that a device
    can support. Capabilities are used to determine which interactions
    can be performed on a device. A DeviceCapability is a decorator
    that is applied to a function that performs an interaction with
    a device.
    """

    @property
    def name(self) -> str:
        return self.capability.__name__

    capability: Callable[[NetworkDevice], Any]
    description: str
    properties: dict[str, Property] = None

    def __call__(self, nd: NetworkDevice, *args: Any, **kwds: Any) -> Any:
        return self.capability(nd, *args, **kwds)


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
        cls, *, description: str = None, properties: dict[str, Property] = None
    ) -> Callable[[Callable[[NetworkDevice], dict]], Callable[[NetworkDevice], dict]]:
        """
        register_capability is a decorator that is applied to a function
        that performs an interaction with a device. The decorator adds
        the function to the list of capabilities that the device supports
        allowing the function to be called by a LanguageFlow.
        """

        def func_wrapper(callable: Callable[[NetworkDevice], dict]):
            cls.capabilities.append(
                DeviceCapability(
                    capability=callable,
                    description=description,
                    properties=properties,
                )
            )
            return callable

        return func_wrapper

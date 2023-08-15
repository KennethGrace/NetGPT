from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Any, Callable

@dataclass
class Capability(ABC):
    """
    A Capability is an abstract base class that defines the interface
    for a capability that can be implemented by a class.
    """

    @abstractmethod
    def __call__(self, *args, **kwargs):
        pass

    name: str
    description: str
    capability: Callable[[Any], Any]
    properties: dict[str, Property] = None

@dataclass
class Property:
    """
    The Property class defines a model for a parameter that is passed to a
    Capability.
    """

    type: str
    description: str = None
    enum: list[str] = None
    items: list[str] = None

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable


class CapabilityRunner:
    """
    The CapabilityRunner class defines a model for a callable that can be
    executed by a language flow and provides a method for executing the
    callable and passing it the required argument.
    """

    def __init__(self, capability: Capability, argument: Any):
        self.name = capability.name
        self.description = capability.description
        self.capability = capability
        self.argument = argument

    def __call__(self, *args, **kwargs):
        new_args = [self.argument] + list(args)
        return self.capability.callable(*new_args, **kwargs)

    @classmethod
    def make(cls, argument: Any):
        """
        The make method is used to decorate a Capability into a CapabilityRunner.
        """

        def decorator(capability: Capability) -> CapabilityRunner:
            return CapabilityRunner(
                capability=capability,
                argument=argument
            )

        return decorator

    @property
    def parameters(self) -> Parameters:
        """
        The parameters prop returns a Parameters
        object that describes the parameters that are required by the
        LanguageFunction.
        """
        return Parameters(
            type="object",
            properties={name: prop for name, prop in self.capability.properties.items()},
            required=[name for name, prop in self.capability.properties.items() if prop.required]
        )

    def __dict__(self):
        return {
            "name": self.name,
            "description": self.description,
            "parameters": self.parameters.__dict__(),
        }


@dataclass
class Capability:
    """
    A Capability is an abstract base class that defines the interface
    for a callable that can be implemented by a class.
    """

    name: str
    description: str
    callable: Callable[[Any], Any]
    properties: dict[str, Property] = None

    @classmethod
    def make(cls, description: str, properties: dict[str, Property] = None):
        """
        The make decorator is used to decorate a function into a Capability.
        """

        def decorator(func: Callable[[Any], Any]) -> Capability:
            return Capability(
                name=func.__name__,
                description=description,
                properties=properties,
                callable=func
            )

        return decorator


@dataclass
class Parameters:
    """
    The Parameters class defines a model for the parameters
    that are passed to a CapabilityRunner.
    """

    type: str
    properties: dict[str, Property]
    required: list[str] = None

    def __dict__(self):
        default = {
            "type": self.type,
            "properties": {name: prop.__dict__() for name, prop in self.properties.items()},
        }
        if self.required is not None:
            default["required"] = self.required
        return default


@dataclass
class Property:
    """
    The Property class defines a model for a parameter that is passed to a
    Capability.
    """

    type: str
    description: str = None
    enum: list[str] = None
    items: dict[str, str] = None
    required: bool = True

    def __dict__(self):
        default = {
            "type": self.type,
        }
        if self.description is not None:
            default["description"] = self.description
        if self.enum is not None:
            default["enum"] = self.enum
        if self.items is not None:
            default["items"] = self.items
        return default

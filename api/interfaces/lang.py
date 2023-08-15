from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, List, TypeVar
from pydantic import BaseModel, Field
from abc import ABC, abstractmethod

from interfaces import Capability, Property
from interfaces.chat import Message, BotMessage
from clients.client import DeviceCapability, NetworkDevice


class LanguageException(Exception):
    """
    A LanguageException is raised when an error occurs while
    processing a message in a LanguageFlow. The exception
    contains a message that can be displayed to the end user.
    """

    def __init__(self, message: str):
        self.message = message

    ...


@dataclass
class LanguageCapability(Capability):
    """
    The LanguageCapability class defines a model for a capability that a
    language flow can support. Capabilities are used to determine which
    interactions can be performed.
    """

    capability: Callable[[NaturalLanguageProcessor, Any], Any]

    def __call__(self, nlp: NaturalLanguageProcessor, *args: Any, **kwds: Any) -> Any:
        return self.capability(nlp, *args, **kwds)


def languageCapability(description: str, properties: dict[str, Property] = None):
    """
    The languageCapability decorator is used to decorate a function that
    performs an interaction with a language flow.
    """

    def decorator(func: Callable[[NaturalLanguageProcessor, Any], Any]):
        func.__annotations__["description"] = description
        func.__annotations__["properties"] = properties
        return LanguageCapability(
            name=func.__name__,
            description=description,
            properties=properties,
            capability=func,
        )

    return decorator


def getApproximateType(value: str) -> str:
    """
    The getApproximateType function returns the approximate type mapping
    for the specified value.
    """
    if value.isnumeric():
        return "integer"
    elif value.lower() == "true" or value.lower() == "false":
        return "boolean"
    elif (value.startswith("[") and value.endswith("]")) or value.startswith("list"):
        return "array"
    elif value.startswith("{") and value.endswith("}"):
        return "object"
    else:
        return "string"


@dataclass
class LanguageFunctionProperty:
    """
    The LanguageFunctionProperty class defines a model for a property
    that is passed to a LanguageFunction.
    """

    name: str
    type: str
    description: str = None
    enum: list[str] = None
    items: list[str] = None

    def __dict__(self):
        default = {
            "type": self.type,
        }
        if self.description != None:
            default["description"] = self.description
        if self.enum != None:
            default["enum"] = self.enum
        if self.items != None:
            default["items"] = self.items
        return default


@dataclass
class LanguageFunctionParameters:
    """
    The LanguageFunctionParameters class defines a model for the parameters
    that are passed to a LanguageFunction.
    """

    type: str
    properties: list[LanguageFunctionProperty]
    required: list[str] = None

    def __dict__(self):
        default = {
            "type": self.type,
            "properties": {prop.name: prop.__dict__() for prop in self.properties},
        }
        if self.required != None:
            default["required"] = self.required
        return default


class LanguageFunction:
    """
    The LanguageFunction class defines a model for a function that can
    be executed by the LanguageFlow.
    """

    def __init__(
        self,
        capability: Capability,
        arguments: Any,
    ):
        self.name = capability.name
        self.description = capability.description
        self.capability = capability
        self.arguments = arguments

    def __call__(self, *args: Any, **kwds: Any) -> Any:
        return self.capability.capability(self.arguments, *args, **kwds)
        

    @property
    def parameters(self) -> LanguageFunctionParameters:
        """
        The parameters property returns a LanguageFunctionParameters
        object that describes the parameters that are required by the
        LanguageFunction.
        """
        return LanguageFunctionParameters(
            type="object",
            properties=[
                LanguageFunctionProperty(
                    name=name,
                    description=property.description,
                    type=property.type,
                    items=property.items,
                    enum=property.enum,
                )
                for name, property in self.capability.properties.items()
            ],
        )

    def __dict__(self):
        return {
            "name": self.name,
            "description": self.description,
            "parameters": self.parameters.__dict__(),
        }


class LanguageSettings(BaseModel):
    """
    The Settings class defines a model for the settings parameters for
    a NetGPT LanguageFlow. These settings provide information about the
    parameters that the LanguageFlow requires in order to function, as
    well as being the mechanism for passing those parameters to the
    LanguageFlow.

    name: The name of the LanguageFlow.
    description: A description of the LanguageFlow.
    fields: A dictionary of the parameters that the LanguageFlow requires.
    """

    name: str
    description: str
    fields: dict[str, str] = Field(default_factory=dict)


class Alias(BaseModel):
    name: str
    value: str


class NaturalLanguageProcessor(ABC):
    """
    The NaturalLanguageProcessor class defines the interface for communicating with
    the natural language processing models used by the NetGPT Service.

    It defines a common set of capabilities that are implemented by the
    various NLP models.
    """

    def __init__(
        self,
        settings: LanguageSettings,
        aliases: List[Alias] = [],
        availableFunctions: List[LanguageFunction] = [],
    ):
        self.settings = settings
        self.aliases = aliases
        self.availableFunctions = availableFunctions

    @classmethod
    @abstractmethod
    def getSettings(self) -> LanguageSettings:
        """
        The getSettings method returns the settings model for the
        LanguageFlow.
        """
        ...

    @abstractmethod
    def getHostnames(self) -> List[str]:
        """
        The getHostnames method returns a list of hostnames that
        can be extracted from the language flow.
        """
        ...

    @abstractmethod
    def requestResponse(self, messageHistory: List[Message]) -> BotMessage:
        """
        The requestMessage method requests a message from the AI.
        """
        ...

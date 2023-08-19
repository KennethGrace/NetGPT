from __future__ import annotations

from abc import ABC, abstractmethod
from typing import List

from capabilities import CapabilityRunner
from schema import BotMessage, ChatConfiguration, LanguageSettings, Alias, UserMessage


class LanguageException(Exception):
    """
    A LanguageException is raised when an error occurs while
    processing a message in a LanguageFlow. The exception
    contains a message that can be displayed to the end user.
    """

    def __init__(self, message: str):
        self.message = message

    ...


def get_approximate_type(value: str) -> str:
    """
    The getApproximateType function returns an approximate type mapping
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


class NaturalLanguageProcessor(ABC):
    """
    The NaturalLanguageProcessor class defines the interface for communicating with
    the natural language processing models used by the NetGPT Service.

    It defines a common set of capabilities that are implemented by the
    various NLP models.
    """
    settings: LanguageSettings
    aliases: List[Alias]
    runners: List[CapabilityRunner]

    def __init__(
            self,
            settings: LanguageSettings,
            configuration: ChatConfiguration,
            aliases: List[Alias] = None,
            runners: List[CapabilityRunner] = None
    ):
        if aliases is None:
            aliases = []
        if runners is None:
            runners = []
        self.settings = settings
        self.configuration = configuration
        self.aliases = aliases
        self.runners = runners

    @classmethod
    @abstractmethod
    def get_settings(cls) -> LanguageSettings:
        """
        The getSettings method returns the settings model for the
        LanguageFlow.
        """
        ...

    @abstractmethod
    def request_response(self, message: UserMessage) -> BotMessage:
        """
        The requestMessage method requests a message from the AI.
        """
        ...

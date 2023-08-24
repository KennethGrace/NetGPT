from __future__ import annotations

import datetime
from abc import ABC, abstractmethod
from enum import Enum
from pathlib import Path
from typing import List

from pydantic import BaseModel, Field
from yaml import safe_load

from capabilities import CapabilityRunner
from clients.schema import NetworkSettings
from plugins.schema import PluginList, PluginSettings


class SenderType(str, Enum):
    You = "You"
    NetGPT = "NetGPT"


class MessageSection(BaseModel):
    messageType: MessageType
    content: str


class Message(BaseModel):
    sender: SenderType
    sections: list[MessageSection]
    timestamp: int


class MessageType(str, Enum):
    text = "text"
    code = "code"
    error = "error"


class BotMessage(Message):
    caption: str = None

    @classmethod
    def quick(cls, messageType: MessageType, content: str, **kwargs):
        """
        The quick method creates a BotMessage with a single section.
        """
        return cls(
            sender=SenderType.NetGPT,
            timestamp=int(datetime.datetime.now().timestamp()),
            sections=[
                MessageSection(
                    messageType=messageType,
                    content=content,
                )
            ],
            **kwargs,
        )

    @classmethod
    def filled(cls, sections: list[MessageSection], **kwargs):
        """
        The filled method creates a BotMessage with the specified sections.
        """
        return cls(
            sender=SenderType.NetGPT,
            timestamp=int(datetime.datetime.now().timestamp()),
            sections=sections,
            **kwargs,
        )


class ChatConfiguration(BaseModel):
    """
    The ChatConfiguration class defines a model for the configuration
    of the chatbot.
    """

    name: str
    prompt: str
    max_tokens: int
    temperature: float
    top_p: float

    @classmethod
    def load_configuration(cls, config_file: Path):
        """
        The load_configuration method loads the configuration from the
        specified file.
        """
        with open(config_file, "r") as f:
            config = safe_load(f.read())
            return cls(**config)


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


class UserMessage(BaseModel):
    """
    The UserMessage class defines a model for a message that is received from
    a user. The message contains the message message_history, the connection parameters,
    and the language settings.
    """
    message_history: List[Message]
    network_settings: NetworkSettings
    language_settings: LanguageSettings
    plugin_list: PluginList = None


class Alias(BaseModel):
    name: str
    value: str


class LanguageSettingsBatch(BaseModel):
    """
    A LanguageSettingsBatch is a list of PluginSettings models.
    """
    settings: List[LanguageSettings]


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

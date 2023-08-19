from __future__ import annotations

import datetime
from enum import Enum
from pathlib import Path
from typing import List

from pydantic import BaseModel, Field
from yaml import safe_load

from clients.client import NetworkSettings


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


class PluginList(BaseModel):
    """
    The PluginList class defines a model for a list of plugins.
    This is necessary because more than one plugin can be enabled
    at a time.
    """
    plugins: List[PluginSettings]


class PluginSettings(BaseModel):
    """
    The PluginSettings
    """

    name: str
    description: str
    fields: dict[str, str] = Field(default_factory=dict)
    enabled: bool = False


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


class Options(BaseModel):
    """
    The StringOptions class defines the data model for a string option.
    """
    options: List[str]


class LanguageSettingsBatch(BaseModel):
    """
    A LanguageSettingsBatch is a list of PluginSettings models.
    """
    settings: List[LanguageSettings]


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

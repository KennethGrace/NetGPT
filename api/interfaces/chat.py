from __future__ import annotations

import datetime
from enum import Enum
from pydantic import BaseModel


class ChatException(Exception):
    ...


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
    def quick(cls, messageType: MessageType, content: str, *args, **kwargs):
        return cls(
            sender=SenderType.NetGPT,
            timestamp=int(datetime.datetime.now().timestamp()),
            sections=[
                MessageSection(
                    messageType=messageType,
                    content=content,
                )
            ],
            *args,
            **kwargs,
        )
    
    @classmethod
    def filled(cls, sections: list[MessageSection], *args, **kwargs):
        return cls(
            sender=SenderType.NetGPT,
            timestamp=int(datetime.datetime.now().timestamp()),
            sections=sections,
            *args,
            **kwargs,
        )

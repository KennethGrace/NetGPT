"""
The Chat Router serves requests for performing chat operations.

When a message is received, it is deserialized into a "Message" class.
The message is then evaluated on it's "messageType" and "senderType"
attributes. System messages are sent to the appropriate registered
handler and User messages are interpreted by a "LanguageInterface" class.
A Bot message or System message is then returned to the user.
"""
from __future__ import annotations
import datetime
from typing import List

from fastapi import APIRouter

import logging

from pydantic import BaseModel

logger = logging.getLogger("uvicorn")

from interfaces.chat import Message, BotMessage, MessageType
from clients.client import ConnectionParameters
from interfaces.lang import (
    LanguageSettings,
    LanguageFunction,
    LanguageException,
)

from utils.networking import getNetworkDevice
from utils.language import getLanguage

ChatRouter = APIRouter(prefix="/chat")


class UserMessage(BaseModel):
    messageHistory: List[Message]
    connectionParameters: ConnectionParameters
    languageSettings: LanguageSettings


@ChatRouter.post("/message", response_model=BotMessage)
def receive_message(message: UserMessage):
    """
    Receive a message from the user and return a response.
    """
    print(message)
    networkDeviceType = getNetworkDevice(message.connectionParameters.deviceType)
    if networkDeviceType == None:
        return BotMessage.quick(
            messageType=MessageType.error,
            content=f"Device type {message.connectionParameters.deviceType} not found.",
        )
    languageType = getLanguage(message.languageSettings.name)
    if languageType == None:
        return BotMessage.quick(
            messageType=MessageType.error,
            content=f"Language {message.languageSettings.name} not found.",
        )
    language = languageType(
        settings=message.languageSettings,
        availableFunctions=[
            LanguageFunction(
                capability=capability,
                arguments=networkDeviceType(
                    connectionParameters=message.connectionParameters
                )
            )
            for capability in networkDeviceType.get_capabilities()
        ],
    )
    try:
        response = language.requestResponse(message.messageHistory)
        return response
    except LanguageException as e:
        # Log the error to the console.
        logger.error(str(e))
        # Return an error status code and message to the user if the
        # language interface fails to process the message.
        return BotMessage.quick(
            messageType=MessageType.error,
            content=str(e),
        )

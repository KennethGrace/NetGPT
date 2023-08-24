"""
The Chat Router serves requests for performing chat operations.

When a message is received, it is deserialized into a UserMessage object. The
UserMessage object contains the message message_history, the connection parameters, and
the language settings. The message message_history is a list of Message objects that
contain the message sections, the sender, and the timestamp. The connection
parameters contain the device type and the device settings. The language
settings contain the name of the language and the language settings.
"""
from __future__ import annotations

from fastapi import APIRouter

import logging

from core.chat import ChatCore
from flow.schema import BotMessage, UserMessage

logger = logging.getLogger("uvicorn")

ChatRouter = APIRouter(prefix="/chat")


@ChatRouter.post("/message", response_model=BotMessage)
def receive_message(message: UserMessage):
    """
    Receive a message from the user and return a response.
    """
    logger.info(f"Received message: {message}")
    chat_core = ChatCore(
        languageSettings=message.language_settings,
        networkSettings=message.network_settings,
        pluginList=message.plugin_list,
    )
    bot_message = chat_core.process_message(message)
    logger.info(f"Sending message: {bot_message}")
    return bot_message

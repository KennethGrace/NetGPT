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

import logging

from fastapi import APIRouter, Depends, HTTPException
from jose import JWTError

from core.chat import ChatCore
from core.security import SecurityCore as SC
from flow.schema import BotMessage, UserMessage, MessageType

logger = logging.getLogger("uvicorn")

ChatRouter = APIRouter(prefix="/chat")

SecurityCore = SC.from_config()


def get_user():
    """
    The get_user method returns a dependency that verifies the user's token.
    """

    async def user_dependency(token: str = Depends(SecurityCore.get_token_verifier())):
        return token

    return user_dependency


@ChatRouter.post("/message", response_model=BotMessage)
async def receive_message(message: UserMessage, token: str = Depends(get_user())) -> BotMessage:
    """
    Receive a message from the user and return a response.
    """
    logger.info(f"Received message: {message}")
    try:
        chat_core = ChatCore(
            languageSettings=message.language_settings,
            networkSettings=message.network_settings,
            pluginList=message.plugin_list,
        )
        bot_message = chat_core.process_message(message)
    except Exception as e:
        logger.error(f"Error processing message: {e}")
        raise HTTPException(status_code=500, detail=f"Error processing message: {e}")
    logger.info(f"Sending message: {bot_message}")
    return bot_message


@ChatRouter.get("/greeting", response_model=BotMessage)
async def get_greeting(token: str = Depends(get_user())):
    """
    Get a greeting message from the bot.
    """
    logger.info(f"Received greeting request")
    try:
        logger.info(f"Token: {token}")
        message = BotMessage.quick(
            message_type=MessageType.text,
            content="Hello, I am NetGPT. How can I help you today?",
        )
        logger.info(f"Sending greeting: {message}")
        return message
    except JWTError:
        logger.error(f"Invalid authentication token")
        raise HTTPException(status_code=401, detail="Invalid authentication token")

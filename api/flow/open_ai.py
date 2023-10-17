"""
The OpenAI Flow module defines an AI chat interface for the NetGPT Service.
It implements the "LanguageFlow" interface defined in `interfaces/flow.py.`
"""

from __future__ import annotations

from datetime import datetime
import json
import logging
from typing import Any, Dict, List

import openai

from capabilities import CapabilityRunner
from flow.exceptions import (
    LanguageException
)
from flow import NaturalLanguageProcessor
from flow.schema import LanguageSettings, SenderType, UserMessage, BotMessage, Message, MessageSection, MessageType

logger = logging.getLogger("uvicorn")

API_URL = "https://api.openai.com/v1/chat/completions"
AI_CHAT_MODEL_NAME = "gpt-3.5-turbo-16k-0613"


class OpenAISettings(LanguageSettings):
    """
    The OpenAISettings class defines the data model for settings used by the OpenAI Flow.
    """

    name: str = "Open AI"
    description: str = "Natural Language Processing using OpenAI's API."
    fields: dict[str, str] = {
        "API Key": "api_key",
    }


RoleMappings = {
    SenderType.You: "user",
    SenderType.NetGPT: "assistant",
}


class OpenAIFlow(NaturalLanguageProcessor):
    """
    This OpenAIFlow uses the OpenAI Chat API and so it caches all the messages that
    are sent to it.
    """

    @classmethod
    def get_settings(cls) -> OpenAISettings:
        """
        The getSettings method returns the settings model for the
        OpenAIFlow.
        """
        return OpenAISettings()

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        openai.api_key = self.settings.fields["API Key"]
        self.message_history = []
        self.function_log = []

    def get_openai_parameters(self,
                              message_history: List[Message] = None,
                              runners: List[CapabilityRunner] = None) -> dict[str, Any]:
        """
        The get_openai_parameters function creates the parameters that will be
        sent to the OpenAI Chat API. Both the message_history and the runners
        are optional parameters. If the message_history is not provided, then
        the message_history is set to the message_history of the language. If
        the runners are not provided, then the runners are set to the runners
        of the language.
        """
        if runners is None:
            runners = self.runners
        if message_history is None:
            message_history = self.message_history
        message_history = [
            {
                "content": "\n".join([section.content for section in message.sections]),
                "role": RoleMappings[message.sender],
            }
            for message in message_history
        ]
        params = {
            "model": AI_CHAT_MODEL_NAME,
            "messages": [{"role": "system", "content": self.configuration.prompt}] + message_history,
            "max_tokens": self.configuration.max_tokens,
            "stop": [],
            "temperature": self.configuration.temperature,
            "top_p": self.configuration.top_p,
            "n": 1,
        }
        if len(runners) > 0:
            params["functions"] = [runner.__dict__() for runner in runners]
        return params

    def run(self, runner_name: str, arguments: str) -> str:
        """
        The run function executes a function from the list of available runners.
        It will return a string of either plain text or JSON depending on the
        function.
        """
        func = next(
            (runner for runner in self.runners if runner.name == runner_name),
            None,
        )
        if func is None:
            logger.error(f"Function {runner_name} not found.")
            raise LanguageException(
                f"Sorry. The task you've requested is not one that I can perform."
            )
        try:
            function_params = json.loads(arguments)
        except json.decoder.JSONDecodeError:
            logger.error("Invalid JSON in function call.")
            raise LanguageException(
                f"Sorry. I didn't understand the information needed."
            )
        try:
            output = func(**function_params)
        except Exception as e:
            logger.error(str(e))
            raise LanguageException(
                f"Sorry. I've experienced an error trying to perform the task."
            )
        if isinstance(output, str):
            return output
        elif isinstance(output, dict):
            return json.dumps(output, sort_keys=True)

    def chat(self, message_history: List[Message] = None, runners: List[CapabilityRunner] = None) -> Dict[str, Any]:
        """
        The chat function sends a request to the OpenAI Chat API and returns
        the OpenAI response.
        """
        params = self.get_openai_parameters(
            message_history=message_history,
            runners=runners,
        )
        logger.info(f"Sending - {json.dumps(params, indent=4, sort_keys=True)}")
        try:
            r = openai.ChatCompletion.create(**params)
        except openai.InvalidRequestError as e:
            logger.error(str(e))
            raise LanguageException(
                "Sorry. I've experienced an error trying to understand your message."
            )
        if r["choices"][0]["finish_reason"] == "max_tokens":
            logger.error("OpenAI has run out of tokens.")
            raise LanguageException(
                "Sorry. I've run out of tokens. Please try decreasing the scale of your request."
            )
        response_message = r["choices"][0]["message"]
        if "function_call" not in response_message:
            return r
        # If the message is a function call, then we need to execute the function.
        # Then make a new request to the AI with the output of the function to
        # get the response.
        logger.info("Executing function call - " + str(response_message))
        function_output = self.run(
            runner_name=response_message["function_call"]["name"],
            arguments=response_message["function_call"]["arguments"],
        )
        self.function_log.append(function_output)
        # Recursively call the chat function with the output of the function.
        # We remove the available functions so that we don't get stuck in a loop.
        r = self.chat(
            message_history=self.message_history + [Message(
                sender=SenderType.NetGPT,
                sections=[
                    MessageSection(
                        messageType=MessageType.code,
                        content=function_output,
                    )
                ],
                timestamp=int(datetime.now().timestamp()),
            )],
            runners=[],
        )
        return r

    def request_response(self, message: UserMessage) -> BotMessage:
        """
        The request_response function processes a message from the user and returns
        a BotMessage response. If the language raises an exception, the exception is
        caught and returned as a BotMessage indicating an error.
        """
        self.message_history = message.message_history
        r = self.chat()
        return BotMessage.filled(
            sections=[
                         MessageSection(
                             messageType=MessageType.code,
                             content=codeSection,
                         ) for codeSection in self.function_log] + [
                         MessageSection(
                             messageType=MessageType.text,
                             content=str(r["choices"][0]["message"]["content"]),
                         ),
                     ]
        )

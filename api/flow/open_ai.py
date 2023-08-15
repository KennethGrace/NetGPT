"""
The OpenAI Flow module defines an AI chat interface for the NetGPT Service.
It implements the "LanguageFlow" interface defined in `interfaces/flow.py.`
"""

from __future__ import annotations

from copy import deepcopy
from typing import Any, Callable, Dict, List
import openai
import json
import re

import logging

logger = logging.getLogger("uvicorn")

from interfaces import Capability, Property
from interfaces.chat import Message, SenderType, MessageType, BotMessage, MessageSection
from interfaces.lang import (
    NaturalLanguageProcessor,
    LanguageSettings,
    LanguageException,
    LanguageFunction,
    languageCapability,
)

API_URL = "https://api.openai.com/v1/chat/completions"
AI_CHAT_MODEL_NAME = "gpt-3.5-turbo-16k-0613"
MAX_TOKENS = 10000
TEMPERATURE = 0.1
TOP_P = 1


def call_function(
    function_name: str, arguments: str, functions: List[LanguageFunction]
) -> str:
    """
    The call_function function calls a function from the list of available functions.
    It will return a string of either plain text or JSON.
    """
    func = next(
        (function for function in functions if function.name == function_name),
        None,
    )
    if func == None:
        raise LanguageException(
            f"Sorry. The function {function_name} is not available, so I can't execute it."
        )
    try:
        function_params = json.loads(arguments)
    except json.decoder.JSONDecodeError:
        raise LanguageException(
            f"Sorry. The function {function_name} was called with invalid arguments. Try rephrasing your question."
        )
    output = func(**function_params)
    if isinstance(output, str):
        return output
    elif isinstance(output, dict):
        return json.dumps(output, sort_keys=True)


def chat_completion(
    history: List[Dict[str, str]], functions: List[Dict[str, Any]] = []
) -> Dict[str, Any]:
    """
    The chat_completion function sends a request to the OpenAI Chat API and returns
    the response.
    """
    default_params = {
        "model": AI_CHAT_MODEL_NAME,
        "messages": history + [system_message],
        "max_tokens": MAX_TOKENS,
        "stop": [],
        "temperature": TEMPERATURE,
        "top_p": TOP_P,
        "n": 1,
    }
    if len(functions) > 0:
        default_params["functions"] = functions
    logger.info(f"Sending - {json.dumps(default_params, indent=4, sort_keys=True)}")
    r = openai.ChatCompletion.create(**default_params)
    if r["choices"][0]["finish_reason"] == "max_tokens":
        logger.error("OpenAI has run out of tokens.")
        raise LanguageException(
            "Sorry. I've run out of tokens. Please try decreasing the scale of your request."
        )
    return r


class OpenAISettings(LanguageSettings):
    """
    The OpenAISettings class defines the data model for settings used by the OpenAI Flow.
    """

    name: str = "Open AI"
    description: str = "Natural Language Processing using OpenAI's API."
    fields: dict[str, str] = {
        "API Key": "api_key",
    }


NetGPTtoOpenAI_RoleMappings = {
    SenderType.You: "user",
    SenderType.NetGPT: "assistant",
}

system_message = {
    "role": "system",
    "content": "You are NetGPT, an assistant to a network engineer. You are able to directly access devices on the network. If you can not perform an action, you should inform your engineer that you are unable to do so. The engineer may ask you for information about the network. You should provide this information in a clear and concise manner.",
}


class OpenAIFlow(NaturalLanguageProcessor):
    """
    This OpenAIFlow uses the OpenAI Chat API and so it caches all of the messages that
    are sent to it.
    """

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        openai.api_key = self.settings.fields["API Key"]
        self.lastMessage = ""

    @classmethod
    def getSettings(self) -> OpenAISettings:
        """
        The getSettings method returns the settings model for the
        OpenAIFlow.
        """
        return OpenAISettings()

    def requestResponse(self, messageHistory: List[Message]) -> BotMessage:
        # Convert the message history into OpenAI's format.
        history = [
            {
                "content": "\n".join([section.content for section in message.sections]),
                "role": NetGPTtoOpenAI_RoleMappings[message.sender],
            }
            for message in messageHistory
        ]
        # Convert the available functions into OpenAI's format.
        openaiFunctionOutputs = []

        def make_request(
            history: List[Dict[str, str]], functions: List[LanguageFunction] = []
        ) -> Dict[str, Any]:
            try:
                func_dict = [function.__dict__() for function in functions]
                r = chat_completion(history=history, functions=func_dict)
                logger.info(f"Received - {json.dumps(r, indent=4, sort_keys=True)}")
                response_message = r["choices"][0]["message"]
                if "function_call" not in response_message:
                    self.lastMessage = response_message["content"]
                    return r
                # If the message is a function call, then we need to execute the function.
                # Then make a new request to the AI with the output of the function to
                # get the response.
                logger.info("Executing function call - " + str(response_message))
                function_output = call_function(
                    function_name=response_message["function_call"]["name"],
                    arguments=response_message["function_call"]["arguments"],
                    functions=functions,
                )
                openaiFunctionOutputs.append(function_output)
                trimmed_function_output = deepcopy(function_output)
                trimmed_function_output = re.sub(r"\n+", "\n", trimmed_function_output)
                trimmed_function_output = re.sub(r" +", " ", trimmed_function_output)
                self.lastMessage = trimmed_function_output
                r = make_request(
                    history=history
                    + [
                        {
                            "content": trimmed_function_output,
                            "role": "function",
                            "name": response_message["function_call"]["name"],
                        }
                    ],
                    functions=[],
                )
                return r
            except openai.InvalidRequestError as e:
                logger.error(str(e))
                raise LanguageException(
                    "Sorry. I've experienced an error in understanding your message."
                )

        r = make_request(history=history, functions=self.availableFunctions)
        return BotMessage.filled(
            sections=[
                MessageSection(
                    messageType=MessageType.code,
                    content=codeSection,
                )
                for codeSection in openaiFunctionOutputs
            ]
            + [
                MessageSection(
                    messageType=MessageType.text,
                    content=str(r["choices"][0]["message"]["content"]),
                ),
            ]
        )

    def getHostnames(self) -> List[str]:
        """
        The getHostnames method returns a list of hostnames that
        can be extracted from the language flow.
        """
        return []

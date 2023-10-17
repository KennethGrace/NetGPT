"""
The Core Linguistic module defines an operational model for the NetGPT Service.
The ChatCore class.
"""

from __future__ import annotations

import logging
from pathlib import Path

from capabilities import CapabilityRunner
from clients import get_network_device_platform
from clients.schema import NetworkSettings
from flow import get_language
from flow.exceptions import (
    LanguageException
)
from flow.schema import LanguageSettings, ChatConfiguration, UserMessage, BotMessage, MessageType
from plugins import get_plugins, get_all_plugins
from plugins.schema import PluginList

logger = logging.getLogger("uvicorn")

CONFIGURATION_FILE = Path("config/netgpt.yml")


class ChatCore:
    """
    The ChatCore class defines the operational model for the NetGPT Service's
    ability to process natural language and to perform actions in a chat
    environment.
    """

    def __init__(self, languageSettings: LanguageSettings, networkSettings: NetworkSettings,
                 pluginList: PluginList = None):
        """
        The constructor for the ChatCore class assembles the capabilities from the
        specified language, device type, and plugin settings. The capabilities are
        then passed to the language to be used for processing messages.
        """
        language = get_language(languageSettings.name)
        device_type = get_network_device_platform(networkSettings.deviceType)
        if language is None:
            raise LanguageException(f"Language {languageSettings.name} not found.")
        if device_type is None:
            raise LanguageException(f"Device type {networkSettings.deviceType} not found.")
        self.device_functions = [
            CapabilityRunner(
                capability=capability,
                argument=device_type(settings=networkSettings)
            )
            for capability in device_type.get_capabilities()
        ]
        # The plugin_functions are all the plugins that are enabled.
        # This can either be because the plugin is enabled by default or
        # because the user has marked it as enabled in the plugin list.
        default_plugins = [CapabilityRunner(
            capability=capability,
            argument=plugin(plugin.get_settings())
        ) for plugin in get_all_plugins() if plugin.get_settings().enabled for capability in plugin.get_capabilities()]
        if pluginList is not None:
            # Group the enabled plugins with their settings.
            plugin_settings = {ps.name: ps for ps in pluginList.plugins if ps.enabled}
            # Get the plugin classes for the enabled plugins.
            listed_plugins = get_plugins(list(plugin_settings.keys()))
            # Combine the plugin classes with their settings.
            paired_plugins = [(p, plugin_settings[p.get_settings().name]) for p in listed_plugins]
            self.plugin_functions = [
                                        CapabilityRunner(
                                            capability=capability,
                                            argument=plugin(settings=plugin_setting)
                                        )
                                        for plugin, plugin_setting in paired_plugins
                                        for capability in plugin.get_capabilities()
                                    ] + default_plugins
        else:
            self.plugin_functions = default_plugins
        self.language = language(
            settings=languageSettings,
            configuration=ChatConfiguration.load_configuration(CONFIGURATION_FILE),
            runners=self.device_functions + self.plugin_functions
        )

    def process_message(self, message: UserMessage) -> BotMessage:
        """
        The process_message method processes a message from the user and returns a
        BotMessage response. If the language raises an exception, the exception is
        caught and returned as a BotMessage indicating an error.
        """
        try:
            return self.language.request_response(message)
        except LanguageException as e:
            return BotMessage.quick(
                message_type=MessageType.error,
                content=str(e),
            )

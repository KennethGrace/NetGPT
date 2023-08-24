"""
The ReadyLinksPlugin module defines the ReadyLinksPlugin plugin for the API.
"""

from __future__ import annotations

from plugins import Plugin
from plugins.schema import PluginSettings


class ReadyLinksSettings(PluginSettings):
    """
    The ReadyLinksSettings class defines a model for the settings parameters for
    building a ReadyLinksPlugin plugin.
    """

    name: str = "ReadyLinks"
    description: str = "A plugin for the ReadyLinksPlugin API."
    fields: dict[str, str] = {
        "API Key": "api_key",
    }


class ReadyLinksPlugin(Plugin):
    """
    ReadyLinksPlugin plugin for the API
    """

    @classmethod
    def get_settings(cls) -> PluginSettings:
        """
        The get_settings method returns the settings model for the
        ReadyLinksPlugin plugin.
        """
        return ReadyLinksSettings()

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.api_key = self.settings.fields["API Key"]
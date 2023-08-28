from __future__ import annotations

from abc import ABC, abstractmethod
from typing import List

from pydantic import BaseModel, Field

from capabilities import Capability


class PluginSettings(BaseModel):
    """
    The PluginSettings
    """

    name: str
    description: str
    fields: dict[str, str] = Field(default_factory=dict)
    enabled: bool = False


class PluginList(BaseModel):
    """
    The PluginList class defines a model for a list of plugins.
    This is necessary because more than one plugin can be enabled
    at a time.
    """
    plugins: List[PluginSettings]


class Plugin(ABC):
    """
    The Plugin class defines the interface for communicating with
    the plugins used by the NetGPT Service.
    """

    def __init__(self, settings: PluginSettings):
        self.settings = settings

    @classmethod
    @abstractmethod
    def get_settings(cls) -> PluginSettings:
        """
        The getSettings method returns the settings model for the
        Plugin.
        """
        ...

    @classmethod
    def get_capabilities(cls) -> list[Capability]:
        """
        The get_capabilities function returns a list of all capabilities
        that are supported by the plugin. To do this we iterate over all
        the methods of the class and check if they are of type Capability.
        """
        return [
            getattr(cls, method)
            for method in dir(cls)
            if isinstance(getattr(cls, method), Capability)
        ]

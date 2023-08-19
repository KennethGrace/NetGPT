"""
The Plugin module defines a Plugins operations. A Plugin
is similar to a "clients", but the Plugin has a more dynamic
structure.
"""
from abc import ABC, abstractmethod

from capabilities import Capability
from schema import PluginSettings


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



"""
The Plugins module defines the interfaces for extended functionality
beyond the core capabilities of NetGPT to manipulate network devices.
This is typically used to pull in a 3rd party library that provides
additional functionality.
"""
from __future__ import annotations

from typing import List, Type

from plugins.plugin import Plugin
from plugins.pings import PingsPlugin

# Official list of supported plugins.
_plugins: List[Type[Plugin]] = [
    PingsPlugin,
]


def get_all_plugins() -> List[Type[Plugin]]:
    """
    The get_plugins function returns a list of all supported plugin types.
    """
    return _plugins


def get_plugins(plugin_names: List[str]) -> List[Type[Plugin]]:
    """
    The get_plugins function returns a list of plugin types with the specified names.
    """
    return [p for p in _plugins if p.get_settings().name in plugin_names]


def get_plugin(plugin_name: str) -> Type[Plugin] | None:
    """
    The get_plugin function returns the plugin with the specified name.
    """
    return next((p for p in _plugins if p.get_settings().name == plugin_name), None)

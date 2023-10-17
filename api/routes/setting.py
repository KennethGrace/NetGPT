from fastapi import APIRouter

from flow import get_all_languages
from flow.schema import LanguageSettingsBatch
from clients.schema import DeviceOptions
from clients import get_network_device_platforms
from plugins import get_all_plugins
from plugins.schema import PluginList

SettingsRouter = APIRouter(prefix="/settings")


@SettingsRouter.get("/deviceTypes")
def get_device_types():
    """
    Return a list of supported device type options as a list of strings.
    """
    return DeviceOptions(options=get_network_device_platforms().keys())


@SettingsRouter.get("/languages")
def get_language_flows():
    """
    Return a list of supported language flows.
    """
    return LanguageSettingsBatch(
        settings=[language.get_settings() for language in get_all_languages()]
    )


@SettingsRouter.get("/plugins")
def get_plugins():
    """
    Return a list of supported plugins.
    """
    return PluginList(
        plugins=[plugin.get_settings() for plugin in get_all_plugins()]
    )

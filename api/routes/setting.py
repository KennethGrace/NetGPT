from fastapi import APIRouter

from clients import get_network_device_platforms
from flow import get_all_languages
from schema import Options, LanguageSettingsBatch

SettingsRouter = APIRouter(prefix="/settings")


@SettingsRouter.get("/deviceTypes")
def get_device_types():
    """
    Return a list of supported device type options as a list of strings.
    """
    return Options(options=[
        deviceType.vendor for deviceType in get_network_device_platforms()
    ])


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
    return Options(options=[
        plugin.get_settings().name for plugin in get_plugins()
    ])

from typing import List
from fastapi import APIRouter
from pydantic import BaseModel

from interfaces.lang import LanguageSettings
from interfaces.client import NetworkDevice

from utils.networking import getNetworkDevices
from utils.language import getAllLanguages

SettingsRouter = APIRouter(prefix="/settings")

class Options(BaseModel):
    """
    The StringOptions class defines the data model for a string option.
    """
    options: List[str]

class LanguageSettingsBatch(BaseModel):
    """
    A LanguageSettingsBatch is a list of LanguageSettings models.
    """
    settings: List[LanguageSettings]


@SettingsRouter.get("/deviceTypes")
def get_device_types():
    """
    Return a list of supported device type options as a list of strings.
    """
    return Options(options=[
        deviceType.vendor for deviceType in getNetworkDevices()
    ])


@SettingsRouter.get("/languages")
def get_language_flows():
    """
    Return a list of supported language flows.
    """
    return LanguageSettingsBatch(
        settings=[language.getSettings() for language in getAllLanguages()]
    )

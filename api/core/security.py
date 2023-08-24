"""
The security core module defines the operational model for the NetGPT Service's
ability to authenticate users.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path
from yaml import safe_load

from security.schema import AuthenticationServerInformation

logger = logging.getLogger("uvicorn")

CONFIG_FILE_PATH = os.getenv("CONFIG_FILE_PATH", "config/default_config.yml")
CONFIGURATION_FILE = Path(CONFIG_FILE_PATH)


class SecurityCore:
    """
    The SecurityCore class defines the operational model for the NetGPT Service's ability to authenticate users. The
    primary function of this class is to validate the user's token and to provide the available authentication
    options, such as the available authentication domains. But importantly, this class provides the actual
    authentication server info that the user should use for authentication. This class, therefore, does not actually
    perform the authentication, but rather provides the information needed to perform the authentication.
    """

    def __init__(self):
        """
        The constructor for the SecurityCore class loads the configuration file
        and sets the authentication server url.
        """
        with open(CONFIGURATION_FILE, "r") as f:
            self.authentication_config = safe_load(f)["authentication"]

    def get_server_info(self) -> AuthenticationServerInformation:
        """
        The get_authentication_server_url method returns the authentication
        server url.
        """
        return AuthenticationServerInformation(**self.authentication_config)

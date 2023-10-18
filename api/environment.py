from __future__ import annotations

import logging
import os
import sys
from pathlib import Path

from pydantic import BaseModel
from yaml import safe_load

logger = logging.getLogger("uvicorn")


class AuthenticationServerInformation(BaseModel):
    """
    The AuthenticationServerInformation class defines a model for the information
    needed to connect to an authentication server. This is a separate service
    from the NetGPT service.
    """

    provider: str
    server: str
    realm: str
    clientId: str
    clientSecret: str = os.getenv("AUTH_CLIENT_SECRET", None)  # TODO: Refactor to support secrets managers

    @classmethod
    def load(cls) -> AuthenticationServerInformation:
        """
        The load method returns an instance of the AuthenticationServerInformation
        """
        configuration = load_config_file("authentication")
        # Override the configuration with environment variables
        configuration["provider"] = os.getenv("AUTH_PROVIDER", None) or configuration["provider"]
        configuration["server"] = os.getenv("AUTH_SERVER", None) or configuration["server"]
        configuration["realm"] = os.getenv("AUTH_REALM", None) or configuration["realm"]
        configuration["clientId"] = os.getenv("AUTH_CLIENT_ID", None) or configuration["clientId"]
        configuration["clientSecret"] = os.getenv("AUTH_CLIENT_SECRET", None) or configuration["clientSecret"]
        return cls(**configuration)


class NetGPTServerInformation(BaseModel):
    """
    The NetGPTServerInformation class defines a model for the information
    needed to initialize the NetGPT service.
    """

    allowed_origins: list[str] = ["https://localhost", "https://localhost:8443"]

    @classmethod
    def load(cls) -> NetGPTServerInformation:
        """
        The load method returns an instance of the NetGPTServerInformation
        """
        configuration = load_config_file("server")
        # Override the configuration with environment variables
        configuration["allowed_origins"] = os.getenv("ALLOWED_ORIGINS", None) or configuration["allowed_origins"]
        return cls(**configuration)


def get_configuration_file() -> Path:
    """
    The get_configuration_file method returns the configuration file path.
    """

    config_file_path = os.getenv("CONFIG_FILE_PATH", "config/config.yml")
    configuration_file = Path(config_file_path)
    return configuration_file


def load_config_file(section: str) -> dict:
    """
    The load_config_file method attempts to load the configuration file and
    returns the configuration for the specified section.
    """

    configuration_file = get_configuration_file()
    try:
        with open(configuration_file, "r") as f:
            configuration = safe_load(f)[section]
            return configuration
    except FileNotFoundError:
        logger.critical(f"Configuration file {configuration_file} not found. Exiting.")
        sys.exit(1)
    except KeyError as e:
        logger.critical(f"Configuration invalid, {configuration_file}, {e}. Exiting.")
        sys.exit(1)

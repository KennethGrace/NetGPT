"""
The Auth Router serves requests for performing authentication operations.

Typically, the Auth Router is used to validate the user's token.
The token is validated by the SecurityCore class. The SecurityCore class is instantiated
with the user's token. The SecurityCore class validates the token and returns the user's
information. If the token is invalid, the SecurityCore class raises an exception.
The user's information is returned as a User object. The User object contains the
user's name, email, and role.

The Auth Router also provides an info service for authenticating with the
Authentication service. The Authentication service is a separate service that
provides authentication services for the NetGPT API. The info service provides
the authentication service's URL and the available authentication domains.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter

from environment import AuthenticationServerInformation

logger = logging.getLogger("uvicorn")

AuthRouter = APIRouter(prefix="/security")


@AuthRouter.get("/server", response_model=AuthenticationServerInformation)
def get_auth_server_info():
    """
    Get the authentication server info.
    """
    return AuthenticationServerInformation.load()

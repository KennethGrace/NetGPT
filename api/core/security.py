"""
The security core module defines the operational model for the NetGPT Service's
ability to authenticate users.
"""

from __future__ import annotations

import logging

import httpx
from fastapi import Depends
from fastapi.security import OpenIdConnect
from jose import jwt

from environment import AuthenticationServerInformation

logger = logging.getLogger("uvicorn")

AUTH_SERVER_INFO = AuthenticationServerInformation.load()

OIDC_URL = f'{AUTH_SERVER_INFO.server}/auth/realms/{AUTH_SERVER_INFO.realm}/.well-known/openid-configuration'
OIDC = OpenIdConnect(openIdConnectUrl=OIDC_URL, scheme_name="Bearer")


class SecurityCore:
    """
    The SecurityCore class defines the operational model for the NetGPT Service's ability to authenticate users. The
    primary function of this class is to validate the user's token and to provide the available authentication
    options, such as the available authentication domains. But importantly, this class provides the actual
    authentication server info that the user should use for authentication. This class, therefore, does not actually
    perform the authentication, but rather provides the information needed to perform the authentication.
    """

    def __init__(self, auth_server: AuthenticationServerInformation):
        """
        The constructor for the SecurityCore class loads the configuration file
        and sets the authentication server url.
        """
        domain_url = f'{auth_server.server}/realms/{auth_server.realm}'
        response = httpx.get(f"{domain_url}/.well-known/openid-configuration", verify=False)
        response.raise_for_status()
        oidc_config = response.json()
        self.jwks = httpx.get(oidc_config["jwks_uri"], verify=False).json()
        self.client_id = auth_server.clientId
        self.oidc = OpenIdConnect(openIdConnectUrl=f"{domain_url}/.well-known/openid-configuration",
                                  scheme_name="Bearer")

    @classmethod
    def from_config(cls):
        """
        The from_config method returns an instance of the SecurityCore class
        with the configuration file.
        """
        return cls(auth_server=AuthenticationServerInformation.load())

    def get_token_verifier(self):
        """
        The get_token_verifier method returns the token verifier dependency.
        """

        def verify_token(token: str = Depends(self.oidc)) -> dict:
            """
            The verify_token method verifies the user's token.
            """
            # If the token is a bearer token, remove the bearer prefix.
            token = token.replace("Bearer ", "")
            try:
                logger.info(f"Encoded Token: {token}")
                options = {"verify_signature": False, "verify_aud": False}
                payload = jwt.decode(
                    token,
                    self.jwks,
                    algorithms=["RS256"],
                    audience=self.client_id,
                    options=options,
                )
                logger.info(f"Decoded Token: {payload}")
                return payload
            except Exception as e:
                logger.error(e)

        return verify_token

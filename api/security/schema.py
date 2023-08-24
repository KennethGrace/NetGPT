from __future__ import annotations

from pydantic import BaseModel


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

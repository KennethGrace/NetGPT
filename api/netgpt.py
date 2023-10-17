"""
The NetGPT Service serves natural language processing for IT operations.
"""

import logging
import sys

import fastapi
from fastapi.middleware.cors import CORSMiddleware

from environment import NetGPTServerInformation
from routes.chat import ChatRouter
from routes.security import AuthRouter
from routes.setting import SettingsRouter

logging.basicConfig(level=logging.DEBUG, stream=sys.stdout)

server_info = NetGPTServerInformation.load()

application = fastapi.FastAPI()

application.include_router(ChatRouter)
application.include_router(SettingsRouter)
application.include_router(AuthRouter)

application.add_middleware(
    CORSMiddleware,
    allow_origins=server_info.allowed_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

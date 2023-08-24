"""
The NetGPT Service serves natural language processing for IT operations.
"""

import sys
import fastapi
from fastapi.middleware.cors import CORSMiddleware

from routes.chat import ChatRouter
from routes.setting import SettingsRouter
from routes.security import AuthRouter

import logging

logging.basicConfig(level=logging.DEBUG, stream=sys.stdout)


application = fastapi.FastAPI()

application.include_router(ChatRouter)
application.include_router(SettingsRouter)
application.include_router(AuthRouter)

origins = [
    "http://localhost",
    "http://localhost:8080",
    "http://localhost:49488",
]

application.add_middleware(
    CORSMiddleware,
    allow_origins=origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

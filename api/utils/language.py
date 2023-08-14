from typing import Any, List, Type
from pydantic import BaseModel
from dataclasses import dataclass

from interfaces.lang import NaturalLanguageProcessor

from flow.open_ai import OpenAIFlow

Languages: List[Type[NaturalLanguageProcessor]] = [
    OpenAIFlow,
]

def getLanguage(name: str) -> Type[NaturalLanguageProcessor] | None:
    """
    The getLanguage function returns the first language processor that matches the
    specified name.
    """
    return next((language for language in Languages if language.getSettings().name == name), None)


def getAllLanguages() -> List[Type[NaturalLanguageProcessor]]:
    """
    The getAllLanguages function returns a list of all supported language processors.
    """
    return Languages

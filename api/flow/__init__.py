"""
The Language module defines the data models used by the NetGPT Service
for communicating with Natural Language Processing (NLP) models.
"""
from __future__ import annotations

from typing import List, Type

from flow.schema import NaturalLanguageProcessor

from flow.open_ai import OpenAIFlow

Languages: List[Type[NaturalLanguageProcessor]] = [
    OpenAIFlow,
]


def get_language(name: str) -> Type[NaturalLanguageProcessor] | None:
    """
    The getLanguage function returns the first language processor that matches the
    specified name.
    """
    return next((language for language in Languages if language.get_settings().name == name), None)


def get_all_languages() -> List[Type[NaturalLanguageProcessor]]:
    """
    The getAllLanguages function returns a list of all supported language processors.
    """
    return Languages

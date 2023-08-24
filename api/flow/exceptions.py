from __future__ import annotations


class LanguageException(Exception):
    """
    A LanguageException is raised when an error occurs while
    processing a message in a LanguageFlow. The exception
    contains a message that can be displayed to the end user.
    """

    def __init__(self, message: str):
        self.message = message

    ...



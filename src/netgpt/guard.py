"""
The guard module enforces NetGPT's read-only CLI policy.

The guard is the second line of defence. The first is the account on the device:
wherever a platform supports it, give NetGPT a read-only role. The guard exists
because a prefix check alone is easy to get around. On Cisco IOS, `show run |
redirect flash:x` writes a file. On SR Linux, `info > x` does the same. An
embedded newline turns one command into two.

So a command must start with an allowed verb, every pipe must lead to an allowed
filter, and control characters and platform-specific shell syntax are refused.
Abbreviations are accepted only where they are listed explicitly, because an
unlisted abbreviation could resolve on the device to an option we never allowed.
"""

from __future__ import annotations

from dataclasses import dataclass, field


class GuardError(ValueError):
    """Raised when a command is refused by the read-only policy."""


@dataclass(frozen=True)
class CliPolicy:
    """
    A read-only CLI policy for one platform.

    verbs: Accepted first words, including any accepted abbreviations.
    pipe_filters: Accepted first words after each `|`, including abbreviations.
    forbidden: Substrings refused anywhere in the command (control characters
        are always refused).
    """

    verbs: frozenset[str]
    pipe_filters: frozenset[str] = frozenset()
    forbidden: tuple[str, ...] = field(default_factory=tuple)

    def check(self, command: str) -> str:
        """Return the normalized command if the policy allows it, else raise GuardError."""
        command = command.strip()
        if not command:
            raise GuardError("The command is empty.")
        if any(ord(char) < 32 or ord(char) == 127 for char in command):
            raise GuardError("Control characters (including newlines) are not allowed in commands.")
        for token in self.forbidden:
            if token in command:
                raise GuardError(f"'{token}' is not allowed in commands on this platform.")

        head, *pipes = command.split("|")
        words = head.split()
        if not words or words[0].lower() not in self.verbs:
            allowed = ", ".join(sorted(self.verbs))
            raise GuardError(f"Only read-only commands are allowed. Commands must start with one of: {allowed}.")
        for pipe in pipes:
            words = pipe.split()
            if not words or words[0].lower() not in self.pipe_filters:
                allowed = ", ".join(sorted(self.pipe_filters)) or "none"
                raise GuardError(f"Pipe filter not allowed. Allowed filters: {allowed}.")
        return command


NOKIA_SRLINUX = CliPolicy(
    verbs=frozenset({"show", "info"}),
    pipe_filters=frozenset({"as", "grep"}),
    # The SR Linux CLI supports output redirection and command chaining.
    forbidden=(">", "<", ";", "`", "$(", "&"),
)

JUNIPER_JUNOS = CliPolicy(
    verbs=frozenset({"show", "sho", "sh"}),
    # `save`, `append`, and `tee` write files, and `request` acts on the device, so they are deliberately absent.
    pipe_filters=frozenset({"match", "except", "count", "last", "find", "display", "no-more", "trim"}),
)

CISCO_IOS = CliPolicy(
    verbs=frozenset({"show", "sho", "sh"}),
    # `redirect`, `append`, and `tee` write files, so they are deliberately absent.
    pipe_filters=frozenset(
        {"include", "inc", "i", "exclude", "exc", "ex", "begin", "b", "section", "sec", "count"}
    ),
)

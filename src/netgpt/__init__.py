"""NetGPT: read-only network engineering tools for language models, served over MCP."""

from importlib.metadata import PackageNotFoundError, version

try:
    __version__ = version("netgpt")
except PackageNotFoundError:
    __version__ = "0.0.0"

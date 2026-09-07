"""Installed package version shared by CLI, GUI and portable builds."""

from importlib.metadata import PackageNotFoundError, version

try:
    __version__ = version("wechat-local-archive")
except PackageNotFoundError:
    __version__ = "0+unknown"

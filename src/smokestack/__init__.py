"""Smokestack: a local monitoring bridge for Pit Boss grills."""

from importlib.metadata import PackageNotFoundError, version

# pyproject.toml is the only place the version is written.
try:
    __version__ = version("smokestack")
except PackageNotFoundError:  # running from a source tree that isn't installed
    __version__ = "0+unknown"

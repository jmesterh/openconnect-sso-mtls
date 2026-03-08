"""Browser module for handling SSO authentication in a web browser."""

from openconnect_sso.config import DisplayMode

from .browser import Browser
from .browser import TerminatedError


__all__ = ["Browser", "DisplayMode", "TerminatedError"]

"""Configuration management for OpenConnect SSO.

This module handles loading, saving, and managing configuration including
VPN profiles, credentials, and auto-fill rules.
"""

from __future__ import annotations

import enum
from pathlib import Path
from typing import Any
from typing import TypeVar
from urllib.parse import urlparse
from urllib.parse import urlunparse

import attr
import keyring
import keyring.errors
import pyotp
import structlog
import toml  # type: ignore[import-untyped]
import xdg.BaseDirectory  # type: ignore[import-untyped]


logger = structlog.get_logger()

APP_NAME = "openconnect-sso"

T = TypeVar("T", bound="ConfigNode")


def load() -> Config:
    """Load configuration from the XDG config directory.

    Returns:
        Config instance with loaded settings, or default Config if not found.
    """
    path = xdg.BaseDirectory.load_first_config(APP_NAME)
    if not path:
        return Config()
    config_path = Path(path) / "config.toml"
    if not config_path.exists():
        return Config()
    with config_path.open() as config_file:
        try:
            result = Config.from_dict(toml.load(config_file))
            return result if result is not None else Config()
        except Exception:
            logger.error(
                "Could not load configuration file, ignoring",
                path=config_path,
                exc_info=True,
            )
            return Config()


def save(config: Config) -> None:
    """Save configuration to the XDG config directory.

    Args:
        config: Config instance to save.
    """
    path = xdg.BaseDirectory.save_config_path(APP_NAME)
    config_path = Path(path) / "config.toml"
    try:
        config_path.touch()
        with config_path.open("w") as config_file:
            toml.dump(config.as_dict(), config_file)
    except Exception:
        logger.error("Could not save configuration file", path=config_path, exc_info=True)


@attr.s
class ConfigNode:
    """Base class for configuration data nodes."""

    @classmethod
    def from_dict(cls: type[T], d: dict[str, Any] | None) -> T | None:
        """Create an instance from a dictionary."""
        if d is None:
            return None
        return cls(**d)

    def as_dict(self) -> dict[str, Any]:
        """Convert the instance to a dictionary."""
        return attr.asdict(self)


@attr.s
class HostProfile(ConfigNode):
    """VPN host profile containing server address and authentication group."""

    address: str = attr.ib(converter=str)
    user_group: str = attr.ib(converter=str)
    name: str = attr.ib(converter=str)  # authgroup

    @property
    def vpn_url(self) -> str:
        """Construct the full VPN URL from address and user group."""
        parts = urlparse(self.address)
        group = self.user_group or parts.path
        if parts.path == self.address and not self.user_group:
            group = ""
        return urlunparse((parts.scheme or "https", parts.netloc or self.address, group, "", "", ""))


@attr.s
class AutoFillRule(ConfigNode):
    """Rule for auto-filling form fields during SSO authentication."""

    selector: str = attr.ib()
    fill: str | None = attr.ib(default=None)
    action: str | None = attr.ib(default=None)


def get_default_auto_fill_rules() -> dict[str, list[dict[str, Any]]]:
    """Return the default auto-fill rules for Azure AD SSO."""
    return {
        "https://*": [
            AutoFillRule(selector="div[id=passwordError]", action="stop").as_dict(),
            AutoFillRule(selector="input[type=email]", fill="username").as_dict(),
            AutoFillRule(selector="input[name=passwd]", fill="password").as_dict(),
            AutoFillRule(selector="input[data-report-event=Signin_Submit]", action="click").as_dict(),
            AutoFillRule(selector="div[data-value=PhoneAppOTP]", action="click").as_dict(),
            AutoFillRule(selector="a[id=signInAnotherWay]", action="click").as_dict(),
            AutoFillRule(selector="input[id=idTxtBx_SAOTCC_OTC]", fill="totp").as_dict(),
        ]
    }


@attr.s
class Credentials(ConfigNode):
    """User credentials with keyring-backed password and TOTP storage."""

    username: str = attr.ib()

    @property
    def password(self) -> str | None:
        """Retrieve password from system keyring."""
        try:
            return keyring.get_password(APP_NAME, self.username)
        except keyring.errors.KeyringError:
            logger.info("Cannot retrieve saved password from keyring.")
            return ""

    @password.setter
    def password(self, value: str) -> None:
        """Store password in system keyring."""
        try:
            keyring.set_password(APP_NAME, self.username, value)
        except keyring.errors.KeyringError:
            logger.info("Cannot save password to keyring.")

    @property
    def totp(self) -> str | None:
        """Generate current TOTP code from stored secret."""
        try:
            totpsecret = keyring.get_password(APP_NAME, "totp/" + self.username)
            return pyotp.TOTP(totpsecret).now() if totpsecret else None
        except keyring.errors.KeyringError:
            logger.info("Cannot retrieve saved totp info from keyring.")
            return ""

    @totp.setter
    def totp(self, value: str) -> None:
        """Store TOTP secret in system keyring."""
        try:
            keyring.set_password(APP_NAME, "totp/" + self.username, value)
        except keyring.errors.KeyringError:
            logger.info("Cannot save totp secret to keyring.")


def _convert_host_profile(d: dict[str, Any] | None) -> HostProfile | None:
    return HostProfile.from_dict(d)


def _convert_credentials(d: dict[str, Any] | None) -> Credentials | None:
    return Credentials.from_dict(d)


def _convert_auto_fill_rules(rules: dict[str, list[dict[str, Any]]]) -> dict[str, list[AutoFillRule | None]]:
    return {n: [AutoFillRule.from_dict(r) for r in rule] for n, rule in rules.items()}


@attr.s
class Config(ConfigNode):
    """Main configuration container for OpenConnect SSO."""

    default_profile: HostProfile | None = attr.ib(default=None, converter=_convert_host_profile)
    credentials: Credentials | None = attr.ib(default=None, converter=_convert_credentials)
    auto_fill_rules: dict[str, list[AutoFillRule | None]] = attr.ib(
        factory=get_default_auto_fill_rules,
        converter=_convert_auto_fill_rules,
    )
    on_disconnect: str = attr.ib(converter=str, default="")


class DisplayMode(enum.Enum):
    """Browser display mode options."""

    HIDDEN = 0
    SHOWN = 1

"""SAML authentication handler using a browser for SSO workflows."""

from __future__ import annotations

from typing import TYPE_CHECKING

import structlog

from openconnect_sso.browser import Browser


if TYPE_CHECKING:
    from openconnect_sso.auth.authenticator import AuthRequestResponse
    from openconnect_sso.config import Credentials
    from openconnect_sso.config import DisplayMode

log = structlog.get_logger()


async def authenticate_in_browser(
    proxy: str | None,
    auth_info: AuthRequestResponse,
    credentials: Credentials | None,
    display_mode: DisplayMode,
    log_level: int | None = None,
) -> str:
    """Authenticate via browser and extract the SSO token from cookies.

    Args:
        proxy: Optional proxy URL to use for browser requests.
        auth_info: Authentication request response containing login URLs.
        credentials: Optional user credentials for auto-fill.
        display_mode: Browser display mode (shown or hidden).
        log_level: Optional logging level override.

    Returns:
        The SSO token extracted from browser cookies.
    """
    async with Browser(proxy, display_mode, log_level) as browser:
        await browser.authenticate_at(auth_info.login_url, credentials)

        while browser.url != auth_info.login_final_url:
            await browser.page_loaded()
            log.debug("Browser loaded page", url=browser.url)

    return browser.cookies[auth_info.token_cookie_name]

"""Tests for the browser module."""

import sys

import pytest

from openconnect_sso.browser import Browser
from openconnect_sso.browser import DisplayMode
from openconnect_sso.config import Credentials


@pytest.mark.asyncio
async def test_browser_context_manager_should_work_in_empty_context_manager():
    """Test that browser context manager works with empty body."""
    async with Browser() as _:
        pass


@pytest.mark.xfail(
    sys.platform in ["darwin", "win32"],
    reason="https://github.com/vlaci/openconnect-sso/issues/23",
)
@pytest.mark.asyncio
async def test_browser_reports_loaded_url(httpserver):
    """Test that browser reports the loaded URL correctly."""
    async with Browser(display_mode=DisplayMode.HIDDEN) as browser:
        auth_url = httpserver.url_for("/authenticate")

        await browser.authenticate_at(auth_url, credentials=None)

        assert browser.url is None
        await browser.page_loaded()
        assert browser.url == auth_url


@pytest.mark.xfail(
    sys.platform in ["darwin", "win32"],
    reason="https://github.com/vlaci/openconnect-sso/issues/23",
)
@pytest.mark.asyncio
async def test_browser_cookies_accessible(httpserver):
    """Test that cookies set by the page are accessible."""
    async with Browser(display_mode=DisplayMode.HIDDEN) as browser:
        httpserver.expect_request("/authenticate").respond_with_data(
            "<html><body>Hello</body></html>",
            headers={"Set-Cookie": "cookie-name=cookie-value"},
        )
        auth_url = httpserver.url_for("/authenticate")
        cred = Credentials("username")

        await browser.authenticate_at(auth_url, cred)
        await browser.page_loaded()
        assert browser.cookies.get("cookie-name") == "cookie-value"

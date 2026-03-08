"""Browser management for SSO authentication.

This module provides the Browser class that manages a headless or visible
browser process for handling SSO authentication flows.
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING
from typing import Any

import structlog

from openconnect_sso.config import DisplayMode

from . import webengine_process as web


if TYPE_CHECKING:
    from types import TracebackType

    from openconnect_sso.config import Credentials

logger = structlog.get_logger()


class Browser:
    """Manages a browser process for SSO authentication.

    This class spawns a separate browser process and communicates with it
    to handle SSO login flows and extract authentication cookies.
    """

    def __init__(
        self,
        proxy: str | None = None,
        display_mode: DisplayMode = DisplayMode.SHOWN,
        log_level: int | None = None,
    ) -> None:
        """Initialize the browser manager.

        Args:
            proxy: Optional proxy URL for browser requests.
            display_mode: Whether to show or hide the browser window.
            log_level: Optional logging level override.
        """
        self.browser_proc: web.Process | None = None
        self.updater: asyncio.Task[None] | None = None
        self.running = False
        self._urls: asyncio.Queue[str | None] = asyncio.Queue()
        self.url: str | None = None
        self.cookies: dict[str, str] = {}
        self.loop = asyncio.get_event_loop()
        self.proxy = proxy
        self.display_mode = display_mode
        self.log_level = log_level

    async def spawn(self) -> None:
        """Start the browser process."""
        self.browser_proc = web.Process(self.proxy, self.display_mode, self.log_level)
        self.browser_proc.start()
        self.running = True

        self.updater = asyncio.ensure_future(self._update_status())

        def stop(_task: Any) -> None:
            self.running = False

        asyncio.ensure_future(self.browser_proc.wait()).add_done_callback(stop)

    async def _update_status(self) -> None:
        while self.running:
            logger.debug("Waiting for message from browser process")

            try:
                assert self.browser_proc is not None
                state = await self.browser_proc.get_state_async()
            except EOFError:
                if self.running:
                    logger.warn("Connection terminated with browser")
                    self.running = False
                else:
                    logger.debug("Browser exited")  # Reached when stop() callback sets running=False
                await self._urls.put(None)
                return
            logger.debug("Message received from browser", message=state)

            if isinstance(state, web.Url):
                await self._urls.put(state.url)
            elif isinstance(state, web.SetCookie):
                self.cookies[state.name] = state.value
            else:
                logger.error("Message unrecognized", message=state)

    async def authenticate_at(self, url: str, credentials: Credentials | None) -> None:
        """Direct the browser to authenticate at the given URL.

        Args:
            url: The SSO login URL.
            credentials: Optional credentials for auto-fill.
        """
        if not self.running:
            raise RuntimeError("Browser is not running")
        assert self.browser_proc is not None
        self.browser_proc.authenticate_at(url, credentials)

    async def page_loaded(self) -> None:
        """Wait for the browser to finish loading a page.

        Raises:
            TerminatedError: If the browser process has exited.
        """
        rv = await self._urls.get()
        if not self.running:
            raise TerminatedError()
        self.url = rv

    async def __aenter__(self) -> Browser:
        """Async context manager entry - spawns the browser."""
        await self.spawn()
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc_val: BaseException | None,
        exc_tb: TracebackType | None,
    ) -> None:
        """Async context manager exit - terminates the browser."""
        try:
            self.running = False
            assert self.browser_proc is not None
            self.browser_proc.terminate()
        except ProcessLookupError:
            # already stopped
            pass
        assert self.browser_proc is not None
        await self.browser_proc.wait()
        assert self.updater is not None
        await self.updater


class TerminatedError(Exception):
    """Raised when the browser process terminates unexpectedly."""

    pass

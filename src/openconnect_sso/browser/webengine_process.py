"""Qt WebEngine browser process for SSO authentication.

This module runs in a separate process and provides a Qt WebEngine-based
browser for handling SSO login flows.
"""

from __future__ import annotations

import asyncio
import importlib.resources
import json
import multiprocessing
import multiprocessing.queues
import os
import queue
import signal
import sys
from typing import TYPE_CHECKING
from typing import Any
from typing import Callable
from urllib.parse import urlparse

import attr
import structlog
from PyQt6.QtCore import QByteArray
from PyQt6.QtCore import QMessageLogContext
from PyQt6.QtCore import Qt
from PyQt6.QtCore import QTimer
from PyQt6.QtCore import QtMsgType
from PyQt6.QtCore import QUrl
from PyQt6.QtCore import pyqtSlot
from PyQt6.QtCore import qInstallMessageHandler
from PyQt6.QtNetwork import QNetworkCookie
from PyQt6.QtNetwork import QNetworkProxy
from PyQt6.QtWebEngineCore import QWebEnginePage
from PyQt6.QtWebEngineCore import QWebEngineProfile
from PyQt6.QtWebEngineCore import QWebEngineScript
from PyQt6.QtWebEngineWidgets import QWebEngineView
from PyQt6.QtWidgets import QApplication
from PyQt6.QtWidgets import QSizePolicy
from PyQt6.QtWidgets import QVBoxLayout
from PyQt6.QtWidgets import QWidget

from openconnect_sso import config


if TYPE_CHECKING:
    from openconnect_sso.config import AutoFillRule
    from openconnect_sso.config import Credentials as ConfigCredentials
    from openconnect_sso.config import DisplayMode

app: QApplication | None = None
logger = structlog.get_logger("webengine")


@attr.s
class Url:
    """Message indicating the browser loaded a new URL."""

    url: str = attr.ib()


@attr.s
class Credentials:
    """Container for user credentials."""

    credentials: ConfigCredentials | None = attr.ib()


@attr.s
class StartupInfo:
    """Initial browser configuration with URL and credentials."""

    url: str = attr.ib()
    credentials: ConfigCredentials | None = attr.ib()


@attr.s
class SetCookie:
    """Message indicating a cookie was set in the browser."""

    name: str = attr.ib()
    value: str = attr.ib()


class Process(multiprocessing.Process):
    """Browser process that runs in a separate Python process.

    This class manages a Qt WebEngine browser in a subprocess,
    communicating via multiprocessing queues.
    """

    def __init__(
        self,
        proxy: str | None,
        display_mode: DisplayMode,
        log_level: int | None = None,
    ) -> None:
        """Initialize the browser process.

        Args:
            proxy: Optional proxy URL for browser requests.
            display_mode: Whether to show or hide the browser window.
            log_level: Optional logging level override.
        """
        super().__init__()

        self._commands: multiprocessing.Queue[StartupInfo] = multiprocessing.Queue()
        self._states: multiprocessing.Queue[Url | SetCookie] = multiprocessing.Queue()
        self.proxy = proxy
        self.display_mode = display_mode
        self.log_level = log_level

    def authenticate_at(self, url: str, credentials: ConfigCredentials | None) -> None:
        """Queue a URL for the browser to navigate to.

        Args:
            url: The URL to load.
            credentials: Optional credentials for auto-fill.
        """
        self._commands.put(StartupInfo(url, credentials))

    async def get_state_async(self) -> Url | SetCookie:
        """Asynchronously wait for and retrieve the next state message.

        Returns:
            The next message from the browser process.

        Raises:
            EOFError: If the browser process has exited.
        """
        while self.is_alive():
            try:
                return self._states.get_nowait()
            except queue.Empty:
                await asyncio.sleep(0.01)
        if not self.is_alive():
            raise EOFError()
        raise EOFError()  # Unreachable but satisfies type checker

    def run(self) -> None:
        """Main entry point for the browser subprocess."""
        # To work around funky GC conflicts with C++ code by ensuring QApplication terminates last
        global app

        import logging as _logging

        _root_logger = _logging.getLogger()
        _handler = _logging.StreamHandler()
        _handler.setFormatter(structlog.stdlib.ProcessorFormatter(processor=structlog.dev.ConsoleRenderer()))
        _root_logger.addHandler(_handler)
        _root_logger.setLevel(self.log_level if self.log_level is not None else _logging.INFO)
        structlog.configure(
            processors=[
                structlog.stdlib.add_log_level,
                structlog.processors.format_exc_info,
                structlog.stdlib.ProcessorFormatter.wrap_for_formatter,
            ],
            logger_factory=structlog.stdlib.LoggerFactory(),
        )

        # Suppress noisy Qt/Chromium native log messages
        def _qt_message_handler(msg_type: QtMsgType, context: QMessageLogContext, message: str | None) -> None:
            if msg_type == QtMsgType.QtFatalMsg:
                logger.error(message or "")

        qInstallMessageHandler(_qt_message_handler)

        # Suppress Chromium/GPU subprocess stderr (Skia, service_utils, etc.)
        # All useful logging goes through structlog, so stderr in this process
        # is only noise from Chromium internals.
        _devnull = os.open(os.devnull, os.O_WRONLY)
        os.dup2(_devnull, 2)
        os.close(_devnull)

        signal.signal(signal.SIGTERM, on_sigterm)
        signal.signal(signal.SIGINT, signal.SIG_DFL)

        cfg = config.load()

        argv = sys.argv.copy()
        if self.display_mode == config.DisplayMode.HIDDEN:
            argv += ["-platform", "minimal"]
        app = QApplication(argv)

        if self.proxy:
            parsed = urlparse(self.proxy)
            if parsed.scheme.startswith("socks5"):
                proxy_type = QNetworkProxy.ProxyType.Socks5Proxy
            elif parsed.scheme.startswith("http"):
                proxy_type = QNetworkProxy.ProxyType.HttpProxy
            else:
                raise ValueError("Unsupported proxy type", parsed.scheme)
            proxy = QNetworkProxy(proxy_type, parsed.hostname or "", parsed.port or 0)

            QNetworkProxy.setApplicationProxy(proxy)

        # In order to make Python able to handle signals
        force_python_execution = QTimer()
        force_python_execution.start(200)

        def ignore() -> None:
            pass

        force_python_execution.timeout.connect(ignore)
        web = WebBrowser(cfg.auto_fill_rules, self._states.put)

        startup_info = self._commands.get()
        logger.debug("Browser started", startup_info=startup_info)

        logger.debug("Loading page", url=startup_info.url)

        web.authenticate_at(QUrl(startup_info.url), startup_info.credentials)

        web.show()
        app.exec()

        logger.debug("Exiting browser")

    async def wait(self) -> None:
        """Wait for the browser process to exit."""
        while self.is_alive():
            await asyncio.sleep(0.01)
        self.join()


def on_sigterm(signum: int, frame: Any) -> None:
    """Handle SIGTERM by gracefully shutting down the browser."""
    logger.debug("Terminate requested")
    # Force flush cookieStore to disk. Without this hack the cookieStore may
    # not be synced at all if the browser lives only for a short amount of
    # time. Something is off with the call order of destructors as there is no
    # such issue in C++.

    # See: https://github.com/qutebrowser/qutebrowser/commit/8d55d093f29008b268569cdec28b700a8c42d761
    cookie = QNetworkCookie()
    profile = QWebEngineProfile.defaultProfile()
    if profile is not None:
        cookie_store = profile.cookieStore()
        if cookie_store is not None:
            cookie_store.deleteCookie(cookie)

    # Give some time to actually save cookies
    exit_timer = QTimer(app)
    exit_timer.timeout.connect(QApplication.quit)
    exit_timer.start(1000)  # ms


class SilentWebEnginePage(QWebEnginePage):
    """Suppresses JavaScript console output from the browser."""

    def javaScriptConsoleMessage(
        self,
        level: QWebEnginePage.JavaScriptConsoleMessageLevel,
        message: str | None,
        lineNumber: int,
        sourceID: str | None,
    ) -> None:
        """Override to suppress console messages."""
        pass


class WebBrowser(QWebEngineView):
    """Qt WebEngine browser widget for SSO authentication.

    Handles page loading, cookie extraction, auto-fill, and client
    certificate selection.
    """

    _popup_window: WebPopupWindow | None

    def __init__(
        self,
        auto_fill_rules: dict[str, list[AutoFillRule | None]],
        on_update: Callable[[Url | SetCookie], None],
    ) -> None:
        """Initialize the web browser widget.

        Args:
            auto_fill_rules: Dictionary mapping URL patterns to auto-fill rules.
            on_update: Callback function for state updates.
        """
        super().__init__()
        self._on_update = on_update
        self._auto_fill_rules = auto_fill_rules
        self._cert_selected = False
        self._popup_window = None
        self.setPage(SilentWebEnginePage(self))
        page = self.page()
        if page is not None:
            profile = page.profile()
            if profile is not None:
                cookie_store = profile.cookieStore()
                if cookie_store is not None:
                    cookie_store.cookieAdded.connect(self._on_cookie_added)
            page.loadFinished.connect(self._on_load_finished)
            page.selectClientCertificate.connect(self._on_select_client_certificate)

    def createWindow(self, type: QWebEnginePage.WebWindowType) -> QWebEngineView | None:
        """Handle window.open() requests from JavaScript."""
        if type == QWebEnginePage.WebWindowType.WebDialog:
            page = self.page()
            if page is not None:
                self._popup_window = WebPopupWindow(page.profile())
                return self._popup_window.view()
        return None

    def authenticate_at(self, url: QUrl, credentials: ConfigCredentials | None) -> None:
        """Load a URL and set up auto-fill scripts.

        Args:
            url: The URL to navigate to.
            credentials: Optional credentials for auto-fill.
        """
        script_source = importlib.resources.files(__name__).joinpath("user.js").read_text()
        script = QWebEngineScript()
        script.setInjectionPoint(QWebEngineScript.InjectionPoint.DocumentCreation)
        script.setWorldId(QWebEngineScript.ScriptWorldId.ApplicationWorld)
        script.setSourceCode(script_source)
        page = self.page()
        if page is not None:
            page.scripts().insert(script)

            if credentials:
                logger.info("Initiating autologin", cred=credentials)
                for url_pattern, rules in self._auto_fill_rules.items():
                    script = QWebEngineScript()
                    script.setInjectionPoint(QWebEngineScript.InjectionPoint.DocumentReady)
                    script.setWorldId(QWebEngineScript.ScriptWorldId.ApplicationWorld)
                    script.setSourceCode(
                        f"""
// ==UserScript==
// @include {url_pattern}
// ==/UserScript==

function autoFill() {{
    {get_selectors(rules, credentials)}
    setTimeout(autoFill, 1000);
}}
autoFill();
"""
                    )
                    page.scripts().insert(script)

        self.load(url)

    def _on_cookie_added(self, cookie: QNetworkCookie) -> None:
        logger.debug("Cookie set", name=to_str(cookie.name()))
        self._on_update(SetCookie(to_str(cookie.name()), to_str(cookie.value())))

    def _on_load_finished(self, success: bool) -> None:
        page = self.page()
        if page is not None:
            url = page.url().toString()
            logger.debug("Page loaded", url=url)
            self._on_update(Url(url))

    def _on_select_client_certificate(self, selection: Any) -> None:
        certificate = selection.certificates()[0]
        if not self._cert_selected:
            subject = certificate.subjectDisplayName()
            issuer = certificate.issuerDisplayName()
            logger.info("Using client certificate", subject=subject, issuer=issuer)
            if len(selection.certificates()) > 1:
                logger.warning(
                    "Multiple matching client certificates found; using the first one",
                    count=len(selection.certificates()),
                )
            self._cert_selected = True
        selection.select(certificate)


class WebPopupWindow(QWidget):
    """Popup window widget for OAuth flows that open new windows."""

    def __init__(self, profile: QWebEngineProfile | None) -> None:
        """Initialize the popup window.

        Args:
            profile: QWebEngineProfile to use for the popup.
        """
        super().__init__()
        self._view = QWebEngineView(self)

        super().setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
        super().setSizePolicy(QSizePolicy.Policy.Minimum, QSizePolicy.Policy.Minimum)

        layout = QVBoxLayout()
        super().setLayout(layout)
        layout.addWidget(self._view)

        self._view.setPage(QWebEnginePage(profile, self._view))

        self._view.titleChanged.connect(super().setWindowTitle)
        view_page = self._view.page()
        if view_page is not None:
            view_page.geometryChangeRequested.connect(self.handleGeometryChangeRequested)
            view_page.windowCloseRequested.connect(super().close)

    def view(self) -> QWebEngineView:
        """Return the web view widget."""
        return self._view

    @pyqtSlot("const QRect")
    def handleGeometryChangeRequested(self, newGeometry: Any) -> None:
        """Handle geometry change requests from JavaScript."""
        self._view.setMinimumSize(newGeometry.width(), newGeometry.height())
        super().move(newGeometry.topLeft() - self._view.pos())
        super().resize(0, 0)
        super().show()


def to_str(qval: QByteArray) -> str:
    """Convert a Qt byte array to a Python string."""
    return qval.data().decode()


def get_selectors(rules: list[AutoFillRule | None], credentials: ConfigCredentials) -> str:
    """Generate JavaScript statements for auto-fill rules.

    Args:
        rules: List of AutoFillRule instances.
        credentials: User credentials instance.

    Returns:
        JavaScript code string implementing the auto-fill rules.
    """
    statements = []
    for rule in rules:
        if rule is None:
            continue
        selector = json.dumps(rule.selector)
        if rule.action == "stop":
            statements.append(f"""var elem = document.querySelector({selector}); if (elem) {{ return; }}""")
        elif rule.fill:
            value = json.dumps(getattr(credentials, rule.fill, None))
            if value:
                statements.append(
                    f"""var elem = document.querySelector({selector}); if (elem) {{ elem.dispatchEvent(new Event("focus")); elem.value = {value}; elem.dispatchEvent(new Event("blur")); }}"""
                )
            else:
                logger.warning(
                    "Credential info not available",
                    type=rule.fill,
                    possibilities=dir(credentials),
                )
        elif rule.action == "click":
            statements.append(
                f"""var elem = document.querySelector({selector}); if (elem) {{ elem.dispatchEvent(new Event("focus")); elem.click(); }}"""
            )
    return "\n".join(statements)

"""Main application module for OpenConnect SSO.

Orchestrates profile selection, SSO authentication, and hands off
to app.process to launch the VPN connection.
"""

from __future__ import annotations

import asyncio
import getpass
import json
import logging
import os
import shlex
import signal
from argparse import Namespace
from collections.abc import Coroutine
from pathlib import Path
from typing import TYPE_CHECKING
from typing import Any
from typing import cast

import structlog
from prompt_toolkit import HTML
from prompt_toolkit.shortcuts import radiolist_dialog
from requests.exceptions import HTTPError  # type: ignore[import-untyped]

from openconnect_sso import config
from openconnect_sso.app.process import handle_disconnect
from openconnect_sso.app.process import run_openconnect
from openconnect_sso.auth.authenticator import AuthCompleteResponse
from openconnect_sso.auth.authenticator import Authenticator
from openconnect_sso.auth.authenticator import AuthResponseError
from openconnect_sso.browser import TerminatedError
from openconnect_sso.config import Credentials
from openconnect_sso.config import HostProfile
from openconnect_sso.profile import get_profiles


if TYPE_CHECKING:
    from openconnect_sso.config import Config
    from openconnect_sso.config import DisplayMode

logger = structlog.get_logger()


def run(args: Namespace) -> int:
    """Run the main application flow.

    Args:
        args: Parsed command-line arguments.

    Returns:
        Exit code (0 for success, non-zero for errors).
    """
    configure_logger(logging.getLogger(), args.log_level)

    cfg = config.load()

    try:
        if os.name == "nt":
            asyncio.set_event_loop_policy(asyncio.WindowsProactorEventLoopPolicy())  # type: ignore[attr-defined]
        auth_response, selected_profile = asyncio.run(_run(args, cfg))
    except KeyboardInterrupt:
        logger.warn("CTRL-C pressed, exiting")
        return 130
    except ValueError as e:
        msg, retval = e.args
        if retval != 0:
            logger.error(msg)
        return cast(int, retval)
    except TerminatedError:
        logger.warn("Browser window terminated, exiting")
        return 2
    except AuthResponseError as exc:
        logger.error(f'Required attributes not found in response ("{exc}", does this endpoint do SSO?), exiting')
        return 3
    except HTTPError as exc:
        logger.error(f"Request error: {exc}")
        return 4

    config.save(cfg)

    if args.authenticate:
        logger.warn("Exiting after login, as requested")
        details = {
            "host": selected_profile.vpn_url,
            "cookie": auth_response.session_token,
            "fingerprint": auth_response.server_cert_hash,
        }
        if args.authenticate == "json":
            print(json.dumps(details, indent=4))
        elif args.authenticate == "shell":
            print("\n".join(f"{k.upper()}={shlex.quote(v)}" for k, v in details.items()))
        return 0

    try:
        return run_openconnect(
            auth_response,
            selected_profile,
            args.proxy,
            args.ac_version,
            args.openconnect_args,
            full_tunnel=args.full_tunnel,
        )
    except KeyboardInterrupt:
        logger.warn("CTRL-C pressed, exiting")
        return 0
    finally:
        handle_disconnect(cfg.on_disconnect)


def configure_logger(log: logging.Logger, level: int) -> None:
    """Configure structlog with the specified logging level.

    Args:
        log: The root logger to configure.
        level: Logging level (e.g., logging.DEBUG, logging.INFO).
    """
    structlog.configure(
        processors=[
            structlog.stdlib.add_log_level,
            structlog.processors.format_exc_info,
            structlog.stdlib.ProcessorFormatter.wrap_for_formatter,
        ],
        logger_factory=structlog.stdlib.LoggerFactory(),
    )

    formatter = structlog.stdlib.ProcessorFormatter(processor=structlog.dev.ConsoleRenderer())

    handler = logging.StreamHandler()
    handler.setFormatter(formatter)
    log.addHandler(handler)
    log.setLevel(level)


async def _run(args: Namespace, cfg: Config) -> tuple[AuthCompleteResponse, HostProfile]:
    credentials: Credentials | None = None
    if cfg.credentials:
        credentials = cfg.credentials
    elif args.user:
        credentials = Credentials(username=args.user)

    if credentials and not credentials.password:
        credentials.password = getpass.getpass(prompt=f"Password ({args.user}): ")
        cfg.credentials = credentials

    if credentials and not credentials.totp:
        credentials.totp = getpass.getpass(prompt=f"TOTP secret (leave blank if not required) ({args.user}): ")
        cfg.credentials = credentials

    authgroup_from_saved_config = False
    selected_profile: HostProfile
    if cfg.default_profile and not (args.use_profile_selector or args.server):
        selected_profile = cfg.default_profile
        authgroup_from_saved_config = True
    elif args.use_profile_selector or args.profile_path:
        profiles = get_profiles(Path(args.profile_path))
        if not profiles:
            raise ValueError("No profile found", 17)

        profile_selection = await select_profile(profiles)
        if not profile_selection:
            raise ValueError("No profile selected", 18)
        selected_profile = profile_selection
    elif args.server:
        selected_profile = config.HostProfile(address=args.server, user_group=args.usergroup, name="")
    else:
        raise ValueError("Cannot determine server address. Invalid arguments specified.", 19)

    # Handle --list-authgroups: print available groups and exit
    if args.list_authgroups:
        auth = Authenticator(selected_profile, args.proxy, None, args.ac_version, args.log_level)
        groups = auth.fetch_auth_groups()
        if not groups:
            logger.warn("No authentication groups available on this server")
        else:
            for value, label in groups:
                print(value if value == label else f"{value} ({label})")
        raise ValueError("Listed authentication groups", 0)

    # Override authgroup from CLI if provided, otherwise prompt if not saved
    if args.authgroup:
        selected_profile = config.HostProfile(
            address=selected_profile.address, user_group=selected_profile.user_group, name=args.authgroup
        )
    elif not authgroup_from_saved_config:
        selected_profile = await _select_authgroup(selected_profile, args.proxy, args.ac_version, args.log_level)

    cfg.default_profile = config.HostProfile(
        address=selected_profile.address, user_group=selected_profile.user_group, name=selected_profile.name
    )

    display_mode = config.DisplayMode[args.browser_display_mode.upper()]

    auth_response = await authenticate_to(
        selected_profile, args.proxy, credentials, display_mode, args.ac_version, args.log_level
    )

    if args.on_disconnect and not cfg.on_disconnect:
        cfg.on_disconnect = args.on_disconnect

    return auth_response, selected_profile


async def select_profile(profile_list: list[HostProfile]) -> HostProfile | None:
    """Display a dialog for selecting an AnyConnect profile.

    Args:
        profile_list: List of available profiles.

    Returns:
        The selected profile, or None if cancelled.
    """
    selection: HostProfile | None = await radiolist_dialog(
        title="Select AnyConnect profile",
        text=HTML(
            "The following AnyConnect profiles are detected.\n"
            "The selection will be <b>saved</b> and not asked again unless the <pre>--profile-selector</pre> command line option is used"
        ),
        values=[(p, p.name) for i, p in enumerate(profile_list)],
    ).run_async()
    # Somehow prompt_toolkit sets up a bogus signal handler upon exit
    # TODO: Report this issue upstream
    if hasattr(signal, "SIGWINCH"):
        asyncio.get_event_loop().remove_signal_handler(signal.SIGWINCH)
    if not selection:
        return None
    logger.info("Selected profile", profile=selection.name)
    return selection


async def _select_authgroup(profile: HostProfile, proxy: str | None, version: str, log_level: int) -> HostProfile:
    """Probe the server for available auth groups and present a selector dialog."""
    auth = Authenticator(profile, proxy, None, version, log_level)
    groups = auth.fetch_auth_groups()
    if not groups:
        logger.warn("No auth groups returned by server, proceeding without selection")
        return profile
    if len(groups) == 1:
        name = groups[0][0]
        logger.info("Auto-selecting only available auth group", group=name)
    else:
        selected_name: str | None = await radiolist_dialog(
            title="Select authentication group",
            text=HTML("The following authentication groups are available on this VPN server."),
            values=groups,
        ).run_async()
        if hasattr(signal, "SIGWINCH"):
            asyncio.get_event_loop().remove_signal_handler(signal.SIGWINCH)
        if selected_name is None:
            raise ValueError("No authentication group selected", 18)
        logger.info("Selected auth group", group=selected_name)
        name = selected_name
    return config.HostProfile(address=profile.address, user_group=profile.user_group, name=name)


def authenticate_to(
    host: HostProfile,
    proxy: str | None,
    credentials: Credentials | None,
    display_mode: DisplayMode,
    version: str,
    log_level: int = logging.INFO,
) -> Coroutine[Any, Any, AuthCompleteResponse]:
    """Authenticate to the VPN server via SSO.

    Args:
        host: Host profile containing server address.
        proxy: Optional proxy URL.
        credentials: Optional user credentials.
        display_mode: Browser display mode.
        version: AnyConnect version string.
        log_level: Logging level.

    Returns:
        Authentication response containing session token.
    """
    logger.info("Connecting to", server=host.address)
    return Authenticator(host, proxy, credentials, version, log_level).authenticate(display_mode)

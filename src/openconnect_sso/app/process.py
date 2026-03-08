"""OpenConnect subprocess management and VPN connection utilities."""

from __future__ import annotations

import os
import shlex
import shutil
import stat
import subprocess
import tempfile

import structlog

from openconnect_sso.auth.authenticator import AuthCompleteResponse
from openconnect_sso.config import HostProfile


logger = structlog.get_logger()

_VPNC_SCRIPT_PATHS = [
    "/opt/homebrew/etc/vpnc/vpnc-script",  # macOS Homebrew (Apple Silicon)
    "/usr/local/etc/vpnc/vpnc-script",  # macOS Homebrew (Intel)
    "/etc/vpnc/vpnc-script",  # Linux
    "/usr/share/vpnc-scripts/vpnc-script",  # Linux (some distros)
]


def _find_vpnc_script() -> str | None:
    for path in _VPNC_SCRIPT_PATHS:
        if os.path.exists(path):
            return path
    return shutil.which("vpnc-script")


def _create_vpnc_wrapper(vpnc_script: str, full_tunnel: bool = False) -> str:
    """Write a temp wrapper that redirects vpnc-script output to a log file.

    If full_tunnel is True, also unsets split-tunnel env vars.
    """
    fd, path = tempfile.mkstemp(prefix="openconnect-sso-vpnc-", suffix=".sh")
    try:
        with os.fdopen(fd, "w") as f:
            f.write("#!/bin/sh\n")
            if full_tunnel:
                f.write(
                    "unset CISCO_SPLIT_INC\n"
                    "unset CISCO_SPLIT_EXC\n"
                    "unset CISCO_IPV6_SPLIT_INC\n"
                    "unset CISCO_IPV6_SPLIT_EXC\n"
                )
            f.write(f'exec {shlex.quote(vpnc_script)} "$@" >> /tmp/openconnect-sso-vpnc.log 2>&1\n')
    except Exception:
        os.unlink(path)
        raise
    os.chmod(path, stat.S_IRWXU | stat.S_IRGRP | stat.S_IXGRP | stat.S_IROTH | stat.S_IXOTH)  # noqa: S103
    return path


def run_openconnect(
    auth_info: AuthCompleteResponse,
    host: HostProfile,
    proxy: str | None,
    version: str,
    args: list[str],
    full_tunnel: bool = False,
) -> int:
    """Launch OpenConnect with the authenticated session.

    Args:
        auth_info: Authentication response with session token.
        host: Target host profile.
        proxy: Optional proxy URL.
        version: AnyConnect version string.
        args: Additional arguments to pass to OpenConnect.
        full_tunnel: If True, disable split tunneling.

    Returns:
        OpenConnect process return code.
    """
    as_root = next(([prog] for prog in ("doas", "sudo") if shutil.which(prog)), [])
    try:
        if not as_root:
            if os.name == "nt":
                import ctypes

                if not ctypes.windll.shell32.IsUserAnAdmin():  # type: ignore[attr-defined]
                    raise PermissionError
            else:
                raise PermissionError
    except PermissionError:
        logger.error("Cannot find suitable program to execute as superuser (doas/sudo), exiting")
        return 20

    openconnect_args = list(args)
    wrapper_script: str | None = None

    if "--script" not in openconnect_args:
        vpnc_script = _find_vpnc_script()
        if vpnc_script is None:
            if full_tunnel:
                logger.error(
                    "--full-tunnel requires a vpnc-script but none was found. "
                    "Install vpnc-script (e.g. 'brew install vpnc-scripts') or pass "
                    "'-- --script /path/to/vpnc-script' manually."
                )
                return 21
            # No vpnc-script found, proceed without wrapper
        else:
            wrapper_script = _create_vpnc_wrapper(vpnc_script, full_tunnel=full_tunnel)
            logger.debug("vpnc-script wrapper created", wrapper=wrapper_script, vpnc_script=vpnc_script)
            openconnect_args = ["--script", wrapper_script] + openconnect_args

    command_line = as_root + [
        "openconnect",
        "--useragent",
        f"AnyConnect Linux_64 {version}",
        "--version-string",
        version,
        "--cookie-on-stdin",
        "--servercert",
        auth_info.server_cert_hash,
        *openconnect_args,
        host.vpn_url,
    ]
    if proxy:
        command_line.extend(["--proxy", proxy])

    session_token = auth_info.session_token.encode("utf-8")
    logger.debug("Starting OpenConnect", command_line=command_line)
    try:
        return subprocess.run(command_line, input=session_token).returncode  # noqa: S603
    finally:
        if wrapper_script:
            try:
                os.unlink(wrapper_script)
            except OSError:
                pass


def handle_disconnect(command: str) -> int | None:
    """Run a command when disconnecting from VPN.

    Args:
        command: Shell command to execute.

    Returns:
        Command return code, or None if no command specified.
    """
    if command:
        logger.info("Running command on disconnect", command_line=command)
        return subprocess.run(command, timeout=5, shell=True).returncode  # noqa: S602,S603
    return None

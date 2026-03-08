"""Command-line interface for OpenConnect SSO.

This module provides the argument parser and main entry point for the
openconnect-sso command.
"""

import argparse
import enum
import logging
import os
import sys

import openconnect_sso
from openconnect_sso import __version__
from openconnect_sso import app
from openconnect_sso import config


def create_argparser():
    """Create and configure the command-line argument parser.

    Returns:
        Configured argparse.ArgumentParser instance.
    """
    parser = argparse.ArgumentParser(prog="openconnect-sso", description=openconnect_sso.__description__)

    server_settings = parser.add_argument_group("Server connection")
    server_settings.add_argument(
        "-p",
        "--profile",
        dest="profile_path",
        help="Use a profile from this file or directory",
    )

    server_settings.add_argument(
        "-P",
        "--profile-selector",
        dest="use_profile_selector",
        help="Always display profile selector",
        action="store_true",
        default=False,
    )

    server_settings.add_argument("--proxy", help="Use a proxy server")

    server_settings.add_argument(
        "-s",
        "--server",
        help="VPN server to connect to. The following forms are accepted: "
        "vpn.server.com, vpn.server.com/usergroup, "
        "https://vpn.server.com, https.vpn.server.com.usergroup",
    )

    auth_settings = parser.add_argument_group(
        "Authentication",
        "Used for the same purpose as in OpenConnect. Refer to OpenConnect's documentation for further information",
    )

    auth_settings.add_argument(
        "--authgroup",
        help="Set authentication group, skipping the interactive selector.",
        default=None,
    )

    auth_settings.add_argument(
        "--list-authgroups",
        help="Query available authentication groups from the server and exit.",
        action="store_true",
        default=False,
    )

    auth_settings.add_argument(
        "-g",
        "--usergroup",
        help="Override usergroup setting from --server argument",
        default="",
    )

    auth_settings.add_argument(
        "--authenticate",
        help="Authenticate only, and output the information needed to make the connection. Output formatting choices: {%(choices)s}",
        choices=["shell", "json"],
        const="shell",
        metavar="OUTPUT-FORMAT",
        nargs="?",
        default=False,
    )

    parser.add_argument(
        "--browser-display-mode",
        help="Controls how the browser window is displayed. 'hidden' mode only works with saved credentials. Choices: {%(choices)s}",
        choices=["shown", "hidden"],
        metavar="DISPLAY-MODE",
        nargs="?",
        default="shown",
    )

    parser.add_argument(
        "--on-disconnect",
        help="Command to run when disconnecting from VPN server",
        default="",
    )

    parser.add_argument("-V", "--version", action="version", version=f"%(prog)s {__version__}")

    parser.add_argument(
        "--ac-version",
        help="AnyConnect Version used for authentication and for OpenConnect, defaults to %(default)s",
        default="4.7.00136",
    )

    parser.add_argument(
        "-l",
        "--log-level",
        help="Set the logging verbosity level.",
        type=LogLevel.parse,
        choices=LogLevel.choices(),
        default=LogLevel.INFO,
    )

    parser.add_argument(
        "--full-tunnel",
        help="Force full-tunnel mode by stripping all split-tunnel routes. "
        "Automatically wraps the vpnc-script to unset CISCO_SPLIT_INC/EXC variables.",
        action="store_true",
        default=False,
    )

    parser.add_argument(
        "openconnect_args",
        help="Arguments passed to openconnect",
        action=StoreOpenConnectArgs,
        nargs=argparse.REMAINDER,
    )

    credentials_group = parser.add_argument_group("Credentials for automatic login")
    credentials_group.add_argument("-u", "--user", help="Authenticate as the given user", default=None)
    return parser


class StoreOpenConnectArgs(argparse.Action):
    """Custom argparse action to store OpenConnect passthrough arguments."""

    def __call__(self, parser, namespace, values, option_string=None):
        """Store values, removing the '--' separator if present."""
        if "--" in values:
            values.remove("--")
        setattr(namespace, self.dest, values)


class LogLevel(enum.IntEnum):
    """Enumeration of supported logging levels."""

    ERROR = logging.ERROR
    WARNING = logging.WARNING
    INFO = logging.INFO
    DEBUG = logging.DEBUG

    def __str__(self):
        """Return the level name as a string."""
        return self.name

    @classmethod
    def parse(cls, name):
        """Parse a log level name string into a LogLevel value."""
        try:
            level = cls.__members__[name.upper()]
        except KeyError:
            print(f"unknown loglevel '{name}', setting to INFO", file=sys.stderr)
            level = logging.INFO
        return level

    @classmethod
    def choices(cls):
        """Return all valid log level choices."""
        return cls.__members__.values()


def main():
    """Main entry point for the openconnect-sso command."""
    parser = create_argparser()
    args = parser.parse_args()

    if (args.profile_path or args.use_profile_selector) and (args.server or args.usergroup):
        parser.error("--profile/--profile-selector and --server/--usergroup are mutually exclusive")

    if not args.profile_path and not args.server and not config.load().default_profile:
        if os.path.exists("/opt/cisco/anyconnect/profile"):
            args.profile_path = "/opt/cisco/anyconnect/profile"
        elif os.path.exists("/opt/cisco/secureclient/vpn/profile"):
            args.profile_path = "/opt/cisco/secureclient/vpn/profile"
        else:
            parser.error("No AnyConnect profile can be found. One of --profile or --server arguments required.")

    if args.use_profile_selector and not args.profile_path:
        parser.error("No AnyConnect profile can be found. --profile argument is required.")

    return app.run(args)


if __name__ == "__main__":
    sys.exit(main())

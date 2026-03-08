"""AnyConnect profile parser.

This module reads and parses Cisco AnyConnect XML profile files to extract
VPN server configurations.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING
from typing import Any

import structlog
from lxml import objectify  # type: ignore[import-untyped]

from openconnect_sso.config import HostProfile


if TYPE_CHECKING:
    from collections.abc import Iterable

logger = structlog.get_logger()

ns = {"enc": "http://schemas.xmlsoap.org/encoding/"}


def _get_profiles_from_one_file(path: Path) -> list[HostProfile]:
    logger.info("Loading profiles from file", path=path.name)

    with path.open() as f:
        xml: Any = objectify.parse(f)

    hostentries = xml.xpath("//enc:AnyConnectProfile/enc:ServerList/enc:HostEntry", namespaces=ns)

    profiles: list[HostProfile] = []
    for entry in hostentries:
        profiles.append(
            HostProfile(
                name=entry.HostName,
                address=entry.HostAddress,
                user_group=getattr(entry, "UserGroup", ""),
            )
        )

    logger.debug("AnyConnect profiles parsed", path=path.name, profiles=profiles)
    return profiles


def get_profiles(path: Path) -> list[HostProfile]:
    """Load VPN profiles from a file or directory of profile files.

    Args:
        path: Path to a profile XML file or directory containing profile files.

    Returns:
        List of HostProfile instances.

    Raises:
        ValueError: If the path does not exist.
    """
    profile_files: Iterable[Path]
    if path.is_file():
        profile_files = [path]
    elif path.is_dir():
        profile_files = path.glob("*.xml")
    else:
        raise ValueError("No profile file found", path.name)

    profiles: list[HostProfile] = []
    for p in profile_files:
        profiles.extend(_get_profiles_from_one_file(p))
    return profiles

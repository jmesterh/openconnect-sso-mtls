"""Authentication handler for Cisco AnyConnect VPN servers.

This module handles the authentication protocol with Cisco VPN servers,
including SSO authentication via browser and XML message exchange.
"""

from __future__ import annotations

from typing import TYPE_CHECKING
from typing import Any
from typing import cast

import attr
import requests  # type: ignore[import-untyped]
import structlog
from lxml import etree  # type: ignore[import-untyped]
from lxml import objectify

from openconnect_sso.auth.saml import authenticate_in_browser


if TYPE_CHECKING:
    from openconnect_sso.config import Credentials
    from openconnect_sso.config import DisplayMode
    from openconnect_sso.config import HostProfile

logger = structlog.get_logger()


class Authenticator:
    """Handles authentication with Cisco AnyConnect VPN servers.

    This class manages the full authentication flow including server detection,
    SAML SSO authentication via browser, and session token exchange.
    """

    def __init__(
        self,
        host: HostProfile,
        proxy: str | None = None,
        credentials: Credentials | None = None,
        version: str | None = None,
        log_level: int | None = None,
    ) -> None:
        """Initialize the authenticator.

        Args:
            host: Host profile containing server address and auth group.
            proxy: Optional proxy URL for HTTP requests.
            credentials: Optional credentials for auto-fill.
            version: AnyConnect version string.
            log_level: Optional logging level.
        """
        self.host = host
        self.proxy = proxy
        self.credentials = credentials
        self.version = version
        self.log_level = log_level
        self.session = create_http_session(proxy, version)

    async def authenticate(self, display_mode: DisplayMode) -> AuthCompleteResponse:
        """Perform full authentication flow.

        Args:
            display_mode: Browser display mode (shown or hidden).

        Returns:
            AuthCompleteResponse with session token and certificate hash.

        Raises:
            AuthenticationError: If authentication fails.
        """
        self._detect_authentication_target_url()

        response = self._start_authentication()
        if not isinstance(response, AuthRequestResponse):
            logger.error(
                "Could not start authentication. Invalid response type in current state",
                response=response,
            )
            raise AuthenticationError(response)

        if response.auth_error:
            logger.error(
                "Could not start authentication. Response contains error",
                error=response.auth_error,
                response=response,
            )
            raise AuthenticationError(response)

        auth_request_response = response

        sso_token = await self._authenticate_in_browser(auth_request_response, display_mode)

        complete_response = self._complete_authentication(auth_request_response, sso_token)
        if not isinstance(complete_response, AuthCompleteResponse):
            logger.error(
                "Could not finish authentication. Invalid response type in current state",
                response=complete_response,
            )
            raise AuthenticationError(complete_response)

        return complete_response

    def _detect_authentication_target_url(self) -> None:
        # Follow possible redirects in a GET request
        # Authentication will occur using a POST request on the final URL
        response = requests.get(self.host.vpn_url, timeout=30)
        response.raise_for_status()
        self.host.address = response.url
        logger.debug("Auth target url", url=self.host.vpn_url)

    def _start_authentication(self) -> AuthRequestResponse | AuthCompleteResponse | None:
        request = _create_auth_init_request(self.host, self.host.vpn_url, self.version)
        logger.debug("Sending auth init request", content=request)
        response = self.session.post(self.host.vpn_url, request)
        logger.debug("Auth init response received", content=response.content)
        result = parse_response(response)
        if isinstance(result, ClientCertRequestResponse):
            logger.debug("Server requested client cert, responding with no cert")
            request = _create_client_cert_request(self.host, self.host.vpn_url, self.version)
            response = self.session.post(self.host.vpn_url, request)
            logger.debug("Client cert response received", content=response.content)
            result = parse_response(response)
        if isinstance(result, ClientCertRequestResponse):
            return None
        return result

    async def _authenticate_in_browser(
        self, auth_request_response: AuthRequestResponse, display_mode: DisplayMode
    ) -> str:
        return await authenticate_in_browser(
            self.proxy, auth_request_response, self.credentials, display_mode, self.log_level
        )

    def _complete_authentication(
        self, auth_request_response: AuthRequestResponse, sso_token: str
    ) -> AuthRequestResponse | AuthCompleteResponse | None:
        request = _create_auth_finish_request(self.host, auth_request_response, sso_token, self.version)
        logger.debug("Sending auth finish request", content=request)
        response = self.session.post(self.host.vpn_url, request)
        logger.debug("Auth finish response received", content=response.content)
        result = parse_response(response)
        if isinstance(result, ClientCertRequestResponse):
            return None
        return result

    def fetch_auth_groups(self) -> list[tuple[str, str]]:
        """Query the server for available authentication groups.

        Returns a list of (value, label) tuples, or an empty list if the server
        does not advertise a group selection.
        """
        self._detect_authentication_target_url()
        request = _create_auth_init_request(self.host, self.host.vpn_url, self.version)
        logger.debug("Fetching available auth groups")
        response = self.session.post(self.host.vpn_url, request)
        response.raise_for_status()
        xml: Any = objectify.fromstring(response.content)
        if xml.xpath("client-cert-request"):
            logger.debug("Server requested client cert during group probe, responding with no cert")
            request = _create_client_cert_request(self.host, self.host.vpn_url, self.version)
            response = self.session.post(self.host.vpn_url, request)
            response.raise_for_status()
            xml = objectify.fromstring(response.content)
        logger.debug("Auth group probe response", content=etree.tostring(xml, pretty_print=True).decode())
        return _parse_group_list_from_xml(xml)


class AuthenticationError(Exception):
    """Raised when authentication fails."""

    pass


class AuthResponseError(AuthenticationError):
    """Raised when the server response is invalid or contains an error."""

    pass


def create_http_session(proxy: str | None, version: str | None) -> requests.Session:
    """Create an HTTP session configured for AnyConnect protocol.

    Args:
        proxy: Optional proxy URL.
        version: AnyConnect version string for User-Agent header.

    Returns:
        Configured requests.Session instance.
    """
    session = requests.Session()
    if proxy:
        session.proxies = {"http": proxy, "https": proxy}
    session.headers.update(
        {
            "User-Agent": f"AnyConnect Linux_64 {version}",
            "Accept": "*/*",
            "Accept-Encoding": "identity",
            "X-Transcend-Version": "1",
            "X-Aggregate-Auth": "1",
            "X-Support-HTTP-Auth": "true",
            "Content-Type": "application/x-www-form-urlencoded",
            # I know, it is invalid but that's what Anyconnect sends
        }
    )
    return session


E: Any = objectify.ElementMaker(annotate=False)


def _create_auth_init_request(host: HostProfile, url: str, version: str | None) -> bytes:
    ConfigAuth = getattr(E, "config-auth")
    Version = E.version
    DeviceId = getattr(E, "device-id")
    GroupSelect = getattr(E, "group-select")
    GroupAccess = getattr(E, "group-access")
    Capabilities = E.capabilities
    AuthMethod = getattr(E, "auth-method")

    root = ConfigAuth(
        {"client": "vpn", "type": "init", "aggregate-auth-version": "2"},
        Version({"who": "vpn"}, version),
        DeviceId("linux-64"),
        GroupSelect(host.name),
        GroupAccess(url),
        Capabilities(AuthMethod("single-sign-on-v2")),
    )
    return cast(bytes, etree.tostring(root, pretty_print=True, xml_declaration=True, encoding="UTF-8"))


def _parse_group_list_from_xml(xml: Any) -> list[tuple[str, str]]:
    """Extract group options from an auth-request XML response.

    Returns a list of (value, label) tuples, or an empty list when no
    group selector is present.
    """
    groups: list[tuple[str, str]] = []
    # Cisco ASA / FTD: <auth><form><select name="group_list"><option>Name</option>
    for opt in xml.xpath('.//select[@name="group_list"]/option'):
        value = (opt.text or "").strip()
        if value:
            groups.append((value, value))
    if groups:
        return groups
    # Fallback for servers that use <group-list><group name="..."> or <option value="...">
    for opt in xml.xpath(".//group-list/group | .//group-list/option"):
        value = opt.get("name") or opt.get("value") or (opt.text or "").strip()
        label = (opt.text or "").strip() or value
        if value:
            groups.append((value, label))
    return groups


def parse_response(
    resp: requests.Response,
) -> AuthRequestResponse | AuthCompleteResponse | ClientCertRequestResponse | None:
    """Parse an HTTP response from the VPN server.

    Args:
        resp: requests.Response object.

    Returns:
        Appropriate response object based on response type.
    """
    resp.raise_for_status()
    xml: Any = objectify.fromstring(resp.content)
    logger.debug("Parsed response XML", content=etree.tostring(xml, pretty_print=True).decode())
    t = xml.get("type")
    if t == "auth-request":
        # Check for client-cert-request (hyphenated tag needs xpath)
        if xml.xpath("client-cert-request"):
            return ClientCertRequestResponse()
        return parse_auth_request_response(xml)
    elif t == "complete":
        return parse_auth_complete_response(xml)
    else:
        logger.error(
            "Unexpected response type",
            type=t,
            content=etree.tostring(xml, pretty_print=True).decode(),
        )
    return None


class ClientCertRequestResponse:
    """Response indicating the server requested a client certificate."""

    pass


def _create_client_cert_request(host: HostProfile, url: str, version: str | None) -> bytes:
    ConfigAuth = getattr(E, "config-auth")
    Version = E.version
    DeviceId = getattr(E, "device-id")
    GroupSelect = getattr(E, "group-select")
    GroupAccess = getattr(E, "group-access")
    Capabilities = E.capabilities
    AuthMethod = getattr(E, "auth-method")
    ClientCertFail = getattr(E, "client-cert-fail")

    root = ConfigAuth(
        {"client": "vpn", "type": "init", "aggregate-auth-version": "2"},
        Version({"who": "vpn"}, version),
        DeviceId("linux-64"),
        GroupSelect(host.name),
        GroupAccess(url),
        Capabilities(AuthMethod("single-sign-on-v2")),
        ClientCertFail(),
    )
    return cast(bytes, etree.tostring(root, pretty_print=True, xml_declaration=True, encoding="UTF-8"))


def parse_auth_request_response(xml: Any) -> AuthRequestResponse:
    """Parse an authentication request response XML.

    Args:
        xml: Parsed XML object from server response.

    Returns:
        AuthRequestResponse with SSO login URLs.

    Raises:
        AuthResponseError: If required elements are missing.
    """
    try:
        auth_elem = xml.auth
    except AttributeError:
        raise AuthResponseError(
            f"Response is missing 'auth' element. Full response:\n{etree.tostring(xml, pretty_print=True).decode()}"
        ) from None

    if auth_elem.get("id") != "main":
        raise AuthResponseError(
            f"Unexpected auth id: {auth_elem.get('id')!r}. Full response:\n"
            f"{etree.tostring(xml, pretty_print=True).decode()}"
        )

    try:
        resp = AuthRequestResponse(
            auth_id=auth_elem.get("id"),
            auth_title=getattr(auth_elem, "title", ""),
            auth_message=auth_elem.message,
            auth_error=getattr(auth_elem, "error", ""),
            opaque=xml.opaque,
            login_url=auth_elem["sso-v2-login"],
            login_final_url=auth_elem["sso-v2-login-final"],
            token_cookie_name=auth_elem["sso-v2-token-cookie-name"],
        )
    except AttributeError as exc:
        raise AuthResponseError(f"{exc}. Full response:\n{etree.tostring(xml, pretty_print=True).decode()}") from exc

    logger.info(resp.auth_message)
    return resp


@attr.s
class AuthRequestResponse:
    """Response containing SSO authentication URLs and parameters."""

    auth_id: str = attr.ib(converter=str)
    auth_title: str = attr.ib(converter=str)
    auth_message: str = attr.ib(converter=str)
    auth_error: str = attr.ib(converter=str)
    login_url: str = attr.ib(converter=str)
    login_final_url: str = attr.ib(converter=str)
    token_cookie_name: str = attr.ib(converter=str)
    opaque: Any = attr.ib()


def parse_auth_complete_response(xml: Any) -> AuthCompleteResponse:
    """Parse an authentication complete response XML.

    Args:
        xml: Parsed XML object from server response.

    Returns:
        AuthCompleteResponse with session token.

    Raises:
        AuthResponseError: If authentication was not successful.
    """
    error_elems = xml.xpath("error")
    if error_elems:
        error_id = error_elems[0].get("id", "unknown")
        error_msg = error_elems[0].text or ""
        raise AuthResponseError(f"Server returned error (id={error_id}): {error_msg}")
    try:
        auth_id = xml.auth.get("id")
    except AttributeError:
        raise AuthResponseError(
            f"Unexpected complete response:\n{etree.tostring(xml, pretty_print=True).decode()}"
        ) from None
    if auth_id != "success":
        raise AuthResponseError(f"Unexpected auth id in complete response: {auth_id!r}")
    resp = AuthCompleteResponse(
        auth_id=auth_id,
        auth_message=xml.auth.message,
        session_token=xml["session-token"],
        server_cert_hash=xml.config["vpn-base-config"]["server-cert-hash"],
    )
    logger.debug("Response received", id=resp.auth_id, message=resp.auth_message)
    return resp


@attr.s
class AuthCompleteResponse:
    """Response containing session token for VPN connection."""

    auth_id: str = attr.ib(converter=str)
    auth_message: str = attr.ib(converter=str)
    session_token: str = attr.ib(converter=str)
    server_cert_hash: str = attr.ib(converter=str)


def _create_auth_finish_request(
    host: HostProfile, auth_info: AuthRequestResponse, sso_token: str, version: str | None
) -> bytes:
    ConfigAuth = getattr(E, "config-auth")
    Version = E.version
    DeviceId = getattr(E, "device-id")
    SessionToken = getattr(E, "session-token")
    SessionId = getattr(E, "session-id")
    Auth = E.auth
    SsoToken = getattr(E, "sso-token")

    root = ConfigAuth(
        {"client": "vpn", "type": "auth-reply", "aggregate-auth-version": "2"},
        Version({"who": "vpn"}, version),
        DeviceId("linux-64"),
        SessionToken(),
        SessionId(),
        auth_info.opaque,
        Auth(SsoToken(sso_token)),
    )
    return cast(bytes, etree.tostring(root, pretty_print=True, xml_declaration=True, encoding="UTF-8"))

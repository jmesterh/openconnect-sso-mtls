"""OpenConnect SSO - Single Sign-On wrapper for OpenConnect VPN client.

This package provides SSO authentication support for Cisco AnyConnect VPNs
using Azure AD (SAMLv2) or other identity providers.
"""

import importlib.metadata


_metadata = importlib.metadata.metadata("openconnect-sso-mtls")

__version__ = _metadata["Version"]
__description__ = _metadata["Summary"]

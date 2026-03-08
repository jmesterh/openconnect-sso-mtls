# openconnect-sso-mtls

[![Tests](https://github.com/jmesterh/openconnect-sso-mtls/workflows/Tests/badge.svg?branch=main&event=push)](https://github.com/jmesterh/openconnect-sso-mtls/actions?query=workflow%3ATests+branch%3Amain+event%3Apush)

A wrapper for [OpenConnect](https://www.infradead.org/openconnect/) that handles SAMLv2/SSO authentication to Cisco SSL-VPNs. Automates the browser-based login flow and passes the resulting session token to `openconnect`. Includes automatic client certificate (mTLS) selection from the system keystore.

Fork of [vlaci/openconnect-sso](https://github.com/vlaci/openconnect-sso).

## Requirements

- Linux or macOS
- `openconnect` installed and on `$PATH`
- Python 3.12+

## Installation

```shell
pipx install git+https://github.com/jmesterh/openconnect-sso-mtls
```

## Usage

If Cisco AnyConnect or Secure Client is installed, existing VPN profiles are detected automatically. In most cases, just run:

```shell
openconnect-sso
```

The server address and credentials are saved between sessions, so subsequent runs require no arguments.

To connect to a specific server for the first time:

```shell
openconnect-sso --server vpn.server.com/group --user user@domain.com
```

### Passing arguments to openconnect

Additional `openconnect` arguments can be appended after `--`:

```shell
openconnect-sso -- --base-mtu=1370
```

### Client certificates (mTLS)

When the VPN server requests a client certificate during the SSO flow, the tool selects the first matching certificate from the system keystore automatically. No configuration is required.

### Authentication groups

Use `--list-authgroups` to discover what groups your VPN server exposes:

```shell
$ openconnect-sso --list-authgroups
CardinalKey
CardinalKey-Full
```

Then connect directly to a group:

```shell
openconnect-sso --authgroup "CardinalKey-Full"
```

### Authenticate only (no tunnel)

Output session credentials without starting the tunnel — useful for scripting:

```shell
openconnect-sso --authenticate shell
openconnect-sso --authenticate json
```

## Configuration

Configuration is stored at `$XDG_CONFIG_HOME/openconnect-sso/config.toml` (typically `~/.config/openconnect-sso/config.toml`).

### TOTP / push-based MFA

For environments where the SSO page requires a TOTP code, adjust `config.toml` to fill and submit it:

```toml
[[auto_fill_rules."https://*"]]
selector = "input[data-report-event=Signin_Submit]"
action = "click"

[[auto_fill_rules."https://*"]]
selector = "input[type=tel]"
fill = "totp"
```

## CLI reference

```
openconnect-sso [OPTIONS] [-- OPENCONNECT_ARGS]

Server connection:
  -s, --server SERVER              VPN server address (host, host/group, or full URL)
  -p, --profile PROFILE_PATH       Load profiles from file or directory
  -P, --profile-selector           Always display profile selector
      --proxy PROXY                Use a proxy server

Authentication:
      --authgroup AUTHGROUP        Set authentication group, skipping the interactive selector
      --list-authgroups            Query available authentication groups and exit
  -g, --usergroup USERGROUP        Override usergroup from --server
      --authenticate [FORMAT]      Authenticate only; output credentials as shell or json

Credentials:
  -u, --user USER                  Authenticate as the given user

Options:
      --browser-display-mode       shown (default) or hidden
      --full-tunnel                Strip split-tunnel routes, force full tunnel
      --on-disconnect CMD          Command to run on disconnect
      --ac-version VERSION         AnyConnect version string (default: 4.7.00136)
  -l, --log-level LEVEL            ERROR, WARNING, INFO, or DEBUG
  -V, --version                    Print version and exit
```

## Development

```shell
uv run pytest
```

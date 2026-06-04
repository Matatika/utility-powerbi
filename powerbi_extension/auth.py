"""PowerBI authentication module.

Two authentication modes are supported:

* **Service principal** (`get_credential` / `get_token`) — Azure AD client
  credentials, sourced from ``POWERBI_TENANT_ID`` / ``POWERBI_CLIENT_ID`` /
  ``POWERBI_CLIENT_SECRET``. Used for unattended/scheduled refreshes.
* **Delegated OAuth** (`get_oauth_token` / `_oauth_refresh`) — the platform's
  "Connect with Microsoft" flow. The catalog stores the user's tokens and, at
  pipeline runtime, injects the ``oauth_credentials.*`` settings as env vars
  (``POWERBI_OAUTH_CREDENTIALS_*``). When the access token is missing or
  expired, ``_oauth_refresh`` exchanges the refresh token via the catalog's
  refresh proxy — mirroring the Matatika tap-spreadsheets-anywhere pattern.

``resolve_token`` picks OAuth when its env vars are present, otherwise falls
back to the service principal.
"""

import os
import typing as t

import requests
from azure.identity import ClientSecretCredential

SCOPE = "https://analysis.windows.net/powerbi/api/.default"

# Env vars derived (by Meltano / the catalog runtime) from the
# `oauth_credentials.*` settings on the powerbi utility (namespace `powerbi`).
OAUTH_ACCESS_TOKEN_ENV = "POWERBI_OAUTH_CREDENTIALS_ACCESS_TOKEN"
OAUTH_REFRESH_TOKEN_ENV = "POWERBI_OAUTH_CREDENTIALS_REFRESH_TOKEN"
OAUTH_REFRESH_PROXY_URL_ENV = "POWERBI_OAUTH_CREDENTIALS_REFRESH_PROXY_URL"
OAUTH_REFRESH_PROXY_URL_AUTH_ENV = "POWERBI_OAUTH_CREDENTIALS_REFRESH_PROXY_URL_AUTH"

# Timeout (seconds) for the refresh-proxy token exchange.
REFRESH_TIMEOUT = 30


def get_credential(
    tenant_id: t.Optional[str] = None,
    client_id: t.Optional[str] = None,
    client_secret: t.Optional[str] = None,
):
    """Get Azure ClientSecretCredential using Meltano env variables"""
    if not tenant_id:
        tenant_id = os.environ["POWERBI_TENANT_ID"]
    if not client_id:
        client_id = os.environ["POWERBI_CLIENT_ID"]
    if not client_secret:
        client_secret = os.environ["POWERBI_CLIENT_SECRET"]

    credential = ClientSecretCredential(
        tenant_id=tenant_id,
        client_id=client_id,
        client_secret=client_secret,
    )

    return credential


def get_token(
    tenant_id: t.Optional[str] = None,
    client_id: t.Optional[str] = None,
    client_secret: t.Optional[str] = None,
):
    """Get Azure token"""
    credential = get_credential(
        tenant_id=tenant_id,
        client_id=client_id,
        client_secret=client_secret,
    )

    access_token = credential.get_token(SCOPE)
    token = access_token.token
    return token


def can_refresh() -> bool:
    """Whether a delegated access token can be (re)obtained via the proxy.

    Requires the refresh token plus the catalog-injected refresh-proxy URL and
    auth header. The proxy keeps the OAuth client secret server-side.
    """
    return all(
        os.environ.get(env)
        for env in (
            OAUTH_REFRESH_TOKEN_ENV,
            OAUTH_REFRESH_PROXY_URL_ENV,
            OAUTH_REFRESH_PROXY_URL_AUTH_ENV,
        )
    )


def _oauth_refresh() -> str:
    """Exchange the delegated refresh token for a fresh access token.

    POSTs to the catalog refresh proxy, which performs the Microsoft token
    exchange and (when run against a pipeline) persists the rotated tokens.
    """
    response = requests.post(
        os.environ[OAUTH_REFRESH_PROXY_URL_ENV],
        headers={"Authorization": os.environ[OAUTH_REFRESH_PROXY_URL_AUTH_ENV]},
        json={
            "grant_type": "refresh_token",
            "refresh_token": os.environ[OAUTH_REFRESH_TOKEN_ENV],
        },
        timeout=REFRESH_TIMEOUT,
    )
    response.raise_for_status()
    return response.json()["access_token"]


def get_oauth_token() -> t.Optional[str]:
    """Return a delegated access token, or None if OAuth is not configured.

    Uses the catalog-supplied access token if present, otherwise refreshes it
    via the proxy. Returns None when neither is available, signalling the
    caller to fall back to the service principal.
    """
    access_token = os.environ.get(OAUTH_ACCESS_TOKEN_ENV)
    if access_token:
        return access_token
    if can_refresh():
        return _oauth_refresh()
    return None


def resolve_token() -> str:
    """Resolve an access token, preferring delegated OAuth over the principal."""
    return get_oauth_token() or get_token()

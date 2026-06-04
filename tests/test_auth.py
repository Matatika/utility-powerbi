import os
from unittest.mock import MagicMock, patch

import pytest
from azure.identity import ClientSecretCredential

from powerbi_extension import auth
from powerbi_extension.auth import (
    OAUTH_ACCESS_TOKEN_ENV,
    OAUTH_REFRESH_PROXY_URL_AUTH_ENV,
    OAUTH_REFRESH_PROXY_URL_ENV,
    OAUTH_REFRESH_TOKEN_ENV,
    SCOPE,
    can_refresh,
    get_credential,
    get_oauth_token,
    get_token,
    resolve_token,
)

TOKEN = "token"
TENANT_ID, CLIENT_ID, CLIENT_SECRET = "tenant_id", "client_id", "client_secret"

OAUTH_ENVS = (
    OAUTH_ACCESS_TOKEN_ENV,
    OAUTH_REFRESH_TOKEN_ENV,
    OAUTH_REFRESH_PROXY_URL_ENV,
    OAUTH_REFRESH_PROXY_URL_AUTH_ENV,
)


@pytest.fixture(autouse=True)
def _clear_oauth_env(monkeypatch):
    """Keep OAuth env vars from leaking between tests (and into other modules)."""
    for env in OAUTH_ENVS:
        monkeypatch.delenv(env, raising=False)


def test_get_credential_with_args():
    with patch.object(ClientSecretCredential, "__new__") as mock_ClientSecretCredential:
        get_credential(
            tenant_id=TENANT_ID,
            client_id=CLIENT_ID,
            client_secret=CLIENT_SECRET,
        )

        mock_ClientSecretCredential.assert_called_once_with(
            ClientSecretCredential,
            tenant_id=TENANT_ID,
            client_id=CLIENT_ID,
            client_secret=CLIENT_SECRET,
        )


def test_get_credential_without_args_missing_envvar_tenant_id():
    if os.getenv("POWERBI_TENANT_ID"):
        del os.environ["POWERBI_TENANT_ID"]
    os.environ["POWERBI_CLIENT_ID"] = CLIENT_ID
    os.environ["POWERBI_CLIENT_SECRET"] = CLIENT_SECRET

    with pytest.raises(KeyError, match="POWERBI_TENANT_ID"):
        get_credential()


def test_get_credential_without_args_missing_envvar_client_id():
    os.environ["POWERBI_TENANT_ID"] = TENANT_ID
    if os.getenv("POWERBI_CLIENT_ID"):
        del os.environ["POWERBI_CLIENT_ID"]
    os.environ["POWERBI_CLIENT_SECRET"] = CLIENT_SECRET

    with pytest.raises(KeyError, match="POWERBI_CLIENT_ID"):
        get_credential()


def test_get_credential_without_args_missing_envvar_client_secret():
    os.environ["POWERBI_TENANT_ID"] = TENANT_ID
    os.environ["POWERBI_CLIENT_ID"] = CLIENT_ID
    if os.getenv("POWERBI_CLIENT_SECRET"):
        del os.environ["POWERBI_CLIENT_SECRET"]

    with pytest.raises(KeyError, match="POWERBI_CLIENT_SECRET"):
        get_credential()


def test_get_token():
    mock_access_token = MagicMock(token=TOKEN)
    mock_get_token = MagicMock(return_value=mock_access_token)
    mock_credential = MagicMock(get_token=mock_get_token)
    with patch(
        "powerbi_extension.auth.get_credential", return_value=mock_credential
    ) as mock_get_credential:
        result = get_token(
            tenant_id=TENANT_ID,
            client_id=CLIENT_ID,
            client_secret=CLIENT_SECRET,
        )

        mock_get_credential.assert_called_once_with(
            tenant_id=TENANT_ID,
            client_id=CLIENT_ID,
            client_secret=CLIENT_SECRET,
        )
        mock_get_token.assert_called_once_with(SCOPE)

        assert result == TOKEN


# --- Delegated OAuth -------------------------------------------------------


def _set_oauth_env(monkeypatch, **overrides):
    values = {
        OAUTH_REFRESH_TOKEN_ENV: "refresh-token",
        OAUTH_REFRESH_PROXY_URL_ENV: "https://catalog/api/tokens/oauth2-microsoft/token",
        OAUTH_REFRESH_PROXY_URL_AUTH_ENV: "Bearer matatika-token",
    }
    values.update(overrides)
    for env, value in values.items():
        if value is None:
            monkeypatch.delenv(env, raising=False)
        else:
            monkeypatch.setenv(env, value)


def test_can_refresh_true_when_proxy_env_present(monkeypatch):
    _set_oauth_env(monkeypatch)
    assert can_refresh() is True


def test_can_refresh_false_when_missing_proxy(monkeypatch):
    _set_oauth_env(monkeypatch, **{OAUTH_REFRESH_PROXY_URL_ENV: None})
    assert can_refresh() is False


def test_oauth_refresh_posts_to_proxy(monkeypatch):
    _set_oauth_env(monkeypatch)
    mock_res = MagicMock()
    mock_res.json.return_value = {"access_token": "fresh-token"}
    with patch(
        "powerbi_extension.auth.requests.post", return_value=mock_res
    ) as mock_post:
        result = auth._oauth_refresh()

    assert result == "fresh-token"
    mock_post.assert_called_once_with(
        "https://catalog/api/tokens/oauth2-microsoft/token",
        headers={"Authorization": "Bearer matatika-token"},
        json={"grant_type": "refresh_token", "refresh_token": "refresh-token"},
        timeout=auth.REFRESH_TIMEOUT,
    )
    mock_res.raise_for_status.assert_called_once()


def test_get_oauth_token_uses_access_token_when_present(monkeypatch):
    monkeypatch.setenv(OAUTH_ACCESS_TOKEN_ENV, "stored-access-token")
    with patch("powerbi_extension.auth._oauth_refresh") as mock_refresh:
        assert get_oauth_token() == "stored-access-token"
    mock_refresh.assert_not_called()


def test_get_oauth_token_refreshes_when_no_access_token(monkeypatch):
    _set_oauth_env(monkeypatch)
    with patch(
        "powerbi_extension.auth._oauth_refresh", return_value="refreshed"
    ) as mock_refresh:
        assert get_oauth_token() == "refreshed"
    mock_refresh.assert_called_once()


def test_get_oauth_token_none_when_not_configured(monkeypatch):
    assert get_oauth_token() is None


def test_resolve_token_prefers_oauth(monkeypatch):
    monkeypatch.setenv(OAUTH_ACCESS_TOKEN_ENV, "oauth-token")
    with patch("powerbi_extension.auth.get_token") as mock_get_token:
        assert resolve_token() == "oauth-token"
    mock_get_token.assert_not_called()


def test_resolve_token_falls_back_to_service_principal(monkeypatch):
    with patch(
        "powerbi_extension.auth.get_token", return_value="sp-token"
    ) as mock_get_token:
        assert resolve_token() == "sp-token"
    mock_get_token.assert_called_once_with()

import os
from unittest.mock import MagicMock, patch

import pytest
from meltano.edk.models import Describe, ExtensionCommand
from requests import ConnectionError as RequestsConnectionError
from requests import HTTPError, RequestException

from powerbi_extension.extension import (
    BASE_URL,
    TIMEOUT,
    PowerBIExtension,
    PowerBIRefreshTimeout,
)


def _http_response(
    status_code: int, headers: dict | None = None, body: dict | None = None
):
    """Build a MagicMock response whose `raise_for_status()` mirrors requests' behaviour."""
    res = MagicMock(status_code=status_code, headers=headers or {})
    if body is not None:
        res.json.return_value = body
    if status_code >= 400:
        err = HTTPError(f"HTTP {status_code}", response=res)
        res.raise_for_status.side_effect = err
    else:
        res.raise_for_status.return_value = None
    return res


TOKEN = "token"
WORKSPACE_ID = "workspace_id"
DATASET_ID = "dataset_id"

# Meltano-populated env vars must be present for PowerBIExtension to construct.
os.environ.setdefault("POWERBI_WORKSPACE_ID", WORKSPACE_ID)
os.environ.setdefault("POWERBI_DATASET_ID", DATASET_ID)


@patch("powerbi_extension.extension.auth.resolve_token", return_value=TOKEN)
def test_init_not_token(mock_resolve_token: MagicMock):
    ext = PowerBIExtension()
    mock_resolve_token.assert_called_once()
    assert ext.log
    assert ext.headers == {"Authorization": f"Bearer {TOKEN}"}
    assert ext.workspace_id == WORKSPACE_ID
    assert ext.dataset_id == DATASET_ID
    assert ext.api_url == BASE_URL


class TestExtension:
    ext = PowerBIExtension(token=TOKEN)

    def test_invoke(self):
        with pytest.raises(NotImplementedError):
            self.ext.invoke()

    @patch("requests.post")
    def test_refresh_ok(self, mock_post: MagicMock):
        request_id = "abcd-1234"
        mock_res = _http_response(
            202,
            headers={
                "Location": (
                    f"https://api.powerbi.com/v1.0/myorg/groups/{WORKSPACE_ID}"
                    f"/datasets/{DATASET_ID}/refreshes/{request_id}"
                ),
                "x-ms-request-id": request_id,
            },
        )
        url = f"{BASE_URL}/groups/{WORKSPACE_ID}/datasets/{DATASET_ID}/refreshes"
        body = {
            "notifyOption": "NoNotification",
            "type": "Full",
        }
        mock_post.return_value = mock_res
        res = self.ext.refresh()
        mock_post.assert_called_once_with(
            url, json=body, headers=self.ext.headers, timeout=TIMEOUT
        )
        assert res == request_id

    @patch("tenacity.nap.time.sleep")
    @patch("requests.post")
    def test_refresh_not_ok(self, mock_post: MagicMock, _mock_sleep: MagicMock):
        mock_post.return_value = _http_response(400)
        with pytest.raises(RequestException):
            self.ext.refresh()
        # 400 is not retryable.
        assert mock_post.call_count == 1

    @patch("tenacity.nap.time.sleep")
    @patch("requests.post")
    def test_refresh_retries_on_500_then_succeeds(
        self, mock_post: MagicMock, _mock_sleep: MagicMock
    ):
        request_id = "retry-after-500"
        mock_post.side_effect = [
            _http_response(500),
            _http_response(500),
            _http_response(
                202,
                headers={
                    "Location": (
                        f"https://api.powerbi.com/v1.0/myorg/groups/{WORKSPACE_ID}"
                        f"/datasets/{DATASET_ID}/refreshes/{request_id}"
                    ),
                    "x-ms-request-id": request_id,
                },
            ),
        ]
        assert self.ext.refresh() == request_id
        assert mock_post.call_count == 3

    @patch("tenacity.nap.time.sleep")
    @patch("requests.post")
    def test_refresh_retries_on_429_then_succeeds(
        self, mock_post: MagicMock, _mock_sleep: MagicMock
    ):
        request_id = "retry-after-429"
        mock_post.side_effect = [
            _http_response(429),
            _http_response(
                202,
                headers={
                    "Location": (
                        f"https://api.powerbi.com/v1.0/myorg/groups/{WORKSPACE_ID}"
                        f"/datasets/{DATASET_ID}/refreshes/{request_id}"
                    ),
                    "x-ms-request-id": request_id,
                },
            ),
        ]
        assert self.ext.refresh() == request_id
        assert mock_post.call_count == 2

    @patch("tenacity.nap.time.sleep")
    @patch("requests.post")
    def test_refresh_exhausts_retries_on_persistent_500(
        self, mock_post: MagicMock, _mock_sleep: MagicMock
    ):
        mock_post.return_value = _http_response(500)
        with pytest.raises(HTTPError):
            self.ext.refresh()
        # Stops after 3 attempts.
        assert mock_post.call_count == 3

    @patch("tenacity.nap.time.sleep")
    @patch("requests.post")
    def test_refresh_does_not_retry_on_401(
        self, mock_post: MagicMock, _mock_sleep: MagicMock
    ):
        mock_post.return_value = _http_response(401)
        with pytest.raises(HTTPError):
            self.ext.refresh()
        assert mock_post.call_count == 1

    @patch("powerbi_extension.extension.auth._oauth_refresh", return_value="new-token")
    @patch("requests.post")
    def test_refresh_reauths_on_401_in_oauth_mode(
        self, mock_post: MagicMock, mock_refresh: MagicMock
    ):
        ext = PowerBIExtension(token=TOKEN)
        ext._can_reauth = True  # simulate delegated OAuth mode
        request_id = "after-reauth"
        mock_post.side_effect = [
            _http_response(401),
            _http_response(
                202,
                headers={
                    "Location": (
                        f"https://api.powerbi.com/v1.0/myorg/groups/{WORKSPACE_ID}"
                        f"/datasets/{DATASET_ID}/refreshes/{request_id}"
                    ),
                    "x-ms-request-id": request_id,
                },
            ),
        ]
        assert ext.refresh() == request_id
        # 401 triggered a single token refresh and one re-issue (no tenacity retry).
        assert mock_post.call_count == 2
        mock_refresh.assert_called_once()
        assert ext.headers == {"Authorization": "Bearer new-token"}

    @patch("powerbi_extension.extension.auth._oauth_refresh")
    @patch("tenacity.nap.time.sleep")
    @patch("requests.post")
    def test_refresh_does_not_reauth_on_401_in_principal_mode(
        self, mock_post: MagicMock, _mock_sleep: MagicMock, mock_refresh: MagicMock
    ):
        ext = PowerBIExtension(token=TOKEN)
        ext._can_reauth = False  # service-principal mode
        mock_post.return_value = _http_response(401)
        with pytest.raises(HTTPError):
            ext.refresh()
        assert mock_post.call_count == 1
        mock_refresh.assert_not_called()

    @patch("tenacity.nap.time.sleep")
    @patch("requests.post")
    def test_refresh_retries_on_connection_error(
        self, mock_post: MagicMock, _mock_sleep: MagicMock
    ):
        request_id = "retry-after-netfail"
        mock_post.side_effect = [
            RequestsConnectionError("dns blip"),
            _http_response(
                202,
                headers={
                    "Location": (
                        f"https://api.powerbi.com/v1.0/myorg/groups/{WORKSPACE_ID}"
                        f"/datasets/{DATASET_ID}/refreshes/{request_id}"
                    ),
                    "x-ms-request-id": request_id,
                },
            ),
        ]
        assert self.ext.refresh() == request_id
        assert mock_post.call_count == 2

    @patch("requests.get")
    def test_get_refresh_status(self, mock_get: MagicMock):
        request_id = "abcd-1234"
        mock_get.return_value = _http_response(
            200, body={"requestId": request_id, "status": "Completed"}
        )

        result = self.ext.get_refresh_status(request_id)

        url = (
            f"{BASE_URL}/groups/{WORKSPACE_ID}/datasets/{DATASET_ID}"
            f"/refreshes/{request_id}"
        )
        mock_get.assert_called_once_with(url, headers=self.ext.headers, timeout=TIMEOUT)
        assert result["requestId"] == request_id
        assert result["status"] == "Completed"

    @patch("tenacity.nap.time.sleep")
    @patch("requests.get")
    def test_get_refresh_status_retries_on_503(
        self, mock_get: MagicMock, _mock_sleep: MagicMock
    ):
        request_id = "abc"
        mock_get.side_effect = [
            _http_response(503),
            _http_response(200, body={"requestId": request_id, "status": "Completed"}),
        ]
        result = self.ext.get_refresh_status(request_id)
        assert result["status"] == "Completed"
        assert mock_get.call_count == 2

    @patch("tenacity.nap.time.sleep")
    @patch("requests.get")
    def test_get_refresh_status_retries_on_timeout(
        self, mock_get: MagicMock, _mock_sleep: MagicMock
    ):
        from requests import Timeout

        request_id = "abc"
        mock_get.side_effect = [
            Timeout("read timeout"),
            _http_response(200, body={"requestId": request_id, "status": "Completed"}),
        ]
        result = self.ext.get_refresh_status(request_id)
        assert result["status"] == "Completed"
        assert mock_get.call_count == 2

    @patch("requests.get")
    def test_list_refresh_history(self, mock_get: MagicMock):
        mock_res = MagicMock(status_code=200)
        mock_res.json.return_value = {
            "value": [
                {"requestId": "a", "status": "Completed"},
                {"requestId": "b", "status": "Failed"},
            ]
        }
        mock_get.return_value = mock_res

        result = self.ext.list_refresh_history(top=2)

        url = f"{BASE_URL}/groups/{WORKSPACE_ID}/datasets/{DATASET_ID}/refreshes"
        mock_get.assert_called_once_with(
            url, headers=self.ext.headers, params={"$top": 2}, timeout=TIMEOUT
        )
        assert len(result) == 2
        assert result[0]["requestId"] == "a"

    @patch("powerbi_extension.extension.time.sleep")
    @patch.object(PowerBIExtension, "get_refresh_status")
    def test_wait_for_refresh_completed(
        self, mock_status: MagicMock, mock_sleep: MagicMock
    ):
        mock_status.side_effect = [
            {"status": "Unknown"},
            {"status": "Unknown"},
            {"status": "Completed", "requestId": "abc"},
        ]
        result = self.ext.wait_for_refresh("abc", poll_interval=1, timeout=60)
        assert result["status"] == "Completed"
        assert mock_status.call_count == 3
        assert mock_sleep.call_count == 2  # slept between the two Unknown polls

    @patch("powerbi_extension.extension.time.sleep")
    @patch.object(PowerBIExtension, "get_refresh_status")
    def test_wait_for_refresh_failed(
        self, mock_status: MagicMock, _mock_sleep: MagicMock
    ):
        mock_status.return_value = {
            "status": "Failed",
            "serviceExceptionJson": "{...}",
        }
        result = self.ext.wait_for_refresh("abc", poll_interval=1, timeout=60)
        assert result["status"] == "Failed"

    @patch("powerbi_extension.extension.time.sleep")
    @patch("powerbi_extension.extension.time.monotonic")
    @patch.object(PowerBIExtension, "get_refresh_status")
    def test_wait_for_refresh_timeout(
        self,
        mock_status: MagicMock,
        mock_monotonic: MagicMock,
        _mock_sleep: MagicMock,
    ):
        # First call sets the deadline, subsequent calls exceed it.
        mock_monotonic.side_effect = [0, 1, 1000]
        mock_status.return_value = {"status": "Unknown"}
        with pytest.raises(PowerBIRefreshTimeout) as exc_info:
            self.ext.wait_for_refresh("abc", poll_interval=1, timeout=60)
        assert exc_info.value.request_id == "abc"
        assert exc_info.value.last_status == "Unknown"

    def test_describe(self):
        result = self.ext.describe()
        assert isinstance(result, Describe)
        command_names = {cmd.name for cmd in result.commands}
        assert command_names == {"refresh", "status", "history"}
        for cmd in result.commands:
            assert isinstance(cmd, ExtensionCommand)
            assert cmd.description  # non-empty

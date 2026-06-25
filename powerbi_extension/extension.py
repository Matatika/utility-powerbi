"""Meltano PowerBI extension."""

from __future__ import annotations

import os
import time
import typing as t

import requests
import structlog
from meltano.edk import models
from meltano.edk.extension import ExtensionBase

from powerbi_extension import auth
from powerbi_extension.retry import powerbi_retry

BASE_URL = "https://api.powerbi.com/v1.0/myorg"
TIMEOUT = 30
TERMINAL_STATUSES = frozenset({"Completed", "Failed", "Disabled"})


class PowerBIRefreshTimeout(Exception):
    """Raised when wait_for_refresh exceeds its timeout before reaching a terminal state."""

    def __init__(self, request_id: str, last_status: str):
        super().__init__(
            f"refresh {request_id} did not reach a terminal state in time "
            f"(last status: {last_status})"
        )
        self.request_id = request_id
        self.last_status = last_status


class PowerBIExtension(ExtensionBase):
    """Extension implementing the ExtensionBase interface."""

    def __init__(self, token: t.Optional[str] = None) -> None:
        """Initialize the extension.

        Workspace, dataset, and API URL are sourced from Meltano-populated
        env vars (POWERBI_WORKSPACE_ID, POWERBI_DATASET_ID, POWERBI_API_URL).
        """
        self.log = structlog.get_logger(name=self.__class__.__name__)
        self.workspace_id = os.environ["POWERBI_WORKSPACE_ID"]
        self.dataset_id = os.environ["POWERBI_DATASET_ID"]
        self.api_url = os.environ.get("POWERBI_API_URL", BASE_URL)
        # Whether a delegated access token can be refreshed mid-run (OAuth mode).
        # Power BI access tokens are short-lived (~1h), so a long-running
        # wait_for_refresh poll can outlast the token; see _request.
        self._can_reauth = auth.can_refresh()
        if not token:
            token = auth.resolve_token()
        self.log.info("Bearer token accessed.")
        self._apply_token(token)

    def _apply_token(self, token: str) -> None:
        """Set the Authorization header from a bearer token."""
        self.headers = {"Authorization": f"Bearer {token}"}

    def _reauth(self) -> bool:
        """Refresh the access token via the OAuth proxy, if possible.

        Returns True if the header was refreshed (OAuth mode only), so callers
        can retry the request once. Service-principal mode is left unchanged.
        """
        if not self._can_reauth:
            return False
        self.log.info("access token rejected; refreshing via OAuth proxy")
        self._apply_token(auth._oauth_refresh())
        return True

    def _request(self, requester: t.Callable, url: str, **kwargs: t.Any):
        """Issue a request, refreshing the token once on a 401/403 in OAuth mode.

        Power BI returns 403 (not 401) with code "TokenExpired" when the access
        token has expired, so both status codes trigger a reauth attempt.
        """
        res = requester(url, headers=self.headers, **kwargs)
        if (res.status_code == 401 or (res.status_code == 403 and res.json()["error"]["code"] == "TokenExpired")) and self._reauth():
            res = requester(url, headers=self.headers, **kwargs)
        return res

    def invoke(self, *args: t.Any, **kwargs: t.Any) -> None:
        """Invoke the underlying CLI that is being wrapped by this extension.

        Args:
            args: Ignored positional arguments.
            kwargs: Ignored keyword arguments.

        Raises:
            NotImplementedError: There is no underlying CLI for this extension.
        """
        raise NotImplementedError

    @powerbi_retry
    def refresh(
        self,
        notify_option: t.Literal[
            "MailOnCompletion", "MailOnFailure", "NoNotification"
        ] = "NoNotification",
        type: str | None = None,
    ):
        """Trigger a refresh of the configured dataset."""
        body = {
            "notifyOption": notify_option,
            "type": type or "Full",
        }

        url = (
            f"{self.api_url}/groups/{self.workspace_id}"
            f"/datasets/{self.dataset_id}/refreshes"
        )
        res = self._request(requests.post, url, json=body, timeout=TIMEOUT)
        self.log.info("refresh trigger response", status_code=res.status_code)
        # Surface 4xx/5xx as HTTPError so the retry policy can inspect status.
        res.raise_for_status()
        # Power BI's enhanced refresh API returns 202 Accepted on success, not 200.
        if res.status_code != 202:
            raise requests.HTTPError(
                f"unexpected status {res.status_code} (expected 202)", response=res
            )
        # The requestId is exposed in the Location header (path tail) and mirrored
        # in x-ms-request-id; the upstream `RequestId` header is not a real header.
        location = res.headers.get("Location", "")
        return location.rsplit("/", 1)[-1] or res.headers.get("x-ms-request-id")

    @powerbi_retry
    def get_refresh_status(self, request_id: str) -> dict:
        """Fetch the status of a single refresh by requestId.

        Returns the full refresh record (requestId, status, startTime, endTime,
        refreshType, serviceExceptionJson, ...). Status is one of Unknown,
        Completed, Failed, or Disabled.
        """
        url = (
            f"{self.api_url}/groups/{self.workspace_id}"
            f"/datasets/{self.dataset_id}/refreshes/{request_id}"
        )
        res = self._request(requests.get, url, timeout=TIMEOUT)
        res.raise_for_status()
        return res.json()

    def list_refresh_history(self, top: int = 10) -> list[dict]:
        """List the most recent refreshes for the configured dataset.

        `top` caps the result count (Power BI accepts $top up to 200).
        """
        url = (
            f"{self.api_url}/groups/{self.workspace_id}"
            f"/datasets/{self.dataset_id}/refreshes"
        )
        res = self._request(requests.get, url, params={"$top": top}, timeout=TIMEOUT)
        res.raise_for_status()
        return res.json().get("value", [])

    def wait_for_refresh(
        self,
        request_id: str,
        poll_interval: int = 30,
        timeout: int = 3600,
    ) -> dict:
        """Poll a refresh until it reaches a terminal status or timeout elapses.

        Returns the final refresh record. Raises PowerBIRefreshTimeout if the
        refresh has not reached a terminal status within `timeout` seconds.
        """
        deadline = time.monotonic() + timeout
        result: dict = {"status": "Unknown"}
        while time.monotonic() < deadline:
            result = self.get_refresh_status(request_id)
            status = result.get("status", "Unknown")
            self.log.info("polled refresh status", request_id=request_id, status=status)
            if status in TERMINAL_STATUSES:
                return result
            time.sleep(poll_interval)
        raise PowerBIRefreshTimeout(request_id, result.get("status", "Unknown"))

    def describe(self) -> models.Describe:
        """Describe the extension's available commands."""
        return models.Describe(
            commands=[
                models.ExtensionCommand(
                    name="refresh",
                    description="Trigger a Power BI dataset refresh and (by default) wait for completion.",
                ),
                models.ExtensionCommand(
                    name="status",
                    description="Get the status of the most recent (or a specific) refresh.",
                ),
                models.ExtensionCommand(
                    name="history",
                    description="List recent refresh history for the configured dataset.",
                ),
            ]
        )

"""Shared authenticated HTTP client for PixlStash nodes."""

from __future__ import annotations

import requests
import urllib3
from requests.exceptions import (
    SSLError,
    Timeout,
    ConnectionError as RequestsConnectionError,
)

VERSION = "1.1.2"
_USER_AGENT = f"ComfyUI-PixlStash/{VERSION}"


def make_client(url: str, token: str, verify_ssl: bool = True) -> "PixlStashClient":
    """Build a PixlStashClient from individual credential arguments."""
    return PixlStashClient(
        base_url=url,
        api_token=token,
        verify_ssl=verify_ssl,
    )


class PixlStashClient:
    """Authenticated HTTP client for the PixlStash REST API.

    Handles Bearer-token auth, SSL verification toggle, a fixed timeout,
    and converts all unsuccessful responses into descriptive RuntimeErrors.
    The raw token value is used only in the Authorization header and is
    never logged or stored beyond the lifetime of this object.

    Pass ``is_write=True`` to POST/PATCH calls so that HTTP 403 responses
    produce a message that specifically names the write-scope requirement.
    """

    def __init__(
        self,
        base_url: str,
        api_token: str,
        verify_ssl: bool = True,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.verify_ssl = verify_ssl
        if not verify_ssl:
            urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
        self._session = requests.Session()
        self._session.headers.update(
            {
                "Authorization": f"Bearer {api_token}",
                "User-Agent": _USER_AGENT,
            }
        )

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    def _url(self, path: str) -> str:
        return f"{self.base_url}{path}"

    def _check(
        self,
        response: requests.Response,
        url: str,
        *,
        is_write: bool = False,
    ) -> None:
        """Raise a descriptive RuntimeError for any unsuccessful response."""
        if response.status_code == 400:
            detail = ""
            try:
                detail = response.json().get("detail", response.text[:500])
            except Exception:
                detail = response.text[:500]
            raise RuntimeError(f"PixlStash: bad request — {detail}")

        if response.status_code == 401:
            raise RuntimeError("PixlStash: invalid or expired API token.")

        if response.status_code == 403:
            if is_write:
                raise RuntimeError(
                    "PixlStash: the token does not have write access. "
                    "The Saver requires a token with ALL (read-write) scope. "
                    "Update the API Token in ComfyUI Settings › PixlStash."
                )
            raise RuntimeError(
                "PixlStash: token does not have access to this resource."
            )

        if response.status_code == 404:
            raise RuntimeError(f"PixlStash: not found — {url}")

        if not response.ok:
            excerpt = response.text[:500] if response.text else "(empty body)"
            raise RuntimeError(
                f"PixlStash: HTTP {response.status_code} from {url}. "
                f"Response: {excerpt}"
            )

    def _request(
        self,
        method: str,
        path: str,
        *,
        is_write: bool = False,
        **kwargs,
    ) -> requests.Response:
        url = self._url(path)
        try:
            response = self._session.request(
                method,
                url,
                verify=self.verify_ssl,
                timeout=30,
                **kwargs,
            )
        except SSLError as exc:
            raise RuntimeError(
                "PixlStash: SSL certificate verification failed. "
                "Set verify_ssl=false if using a self-signed certificate."
            ) from exc
        except Timeout:
            raise RuntimeError(f"PixlStash: request timed out for {url}.")
        except RequestsConnectionError as exc:
            raise RuntimeError(f"PixlStash: connection error for {url}: {exc}") from exc
        self._check(response, url, is_write=is_write)
        return response

    # ------------------------------------------------------------------
    # Public API surface
    # ------------------------------------------------------------------

    def get(self, path: str, **kwargs) -> requests.Response:
        return self._request("GET", path, **kwargs)

    def post(self, path: str, *, is_write: bool = False, **kwargs) -> requests.Response:
        return self._request("POST", path, is_write=is_write, **kwargs)

    def patch(
        self, path: str, *, is_write: bool = False, **kwargs
    ) -> requests.Response:
        return self._request("PATCH", path, is_write=is_write, **kwargs)

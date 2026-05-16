"""Shared authenticated HTTP client for PixlStash nodes."""
from __future__ import annotations

import requests
from requests.exceptions import (
    SSLError,
    Timeout,
    ConnectionError as RequestsConnectionError,
)

VERSION = "1.0.0"
_USER_AGENT = f"ComfyUI-PixlStash/{VERSION}"


class PixlStashClient:
    """Authenticated HTTP client for the PixlStash REST API.

    Handles Bearer-token auth, SSL verification toggle, a fixed timeout,
    and converts all unsuccessful responses into descriptive RuntimeErrors.
    The raw token value is used only in the Authorization header and is
    never logged or stored beyond the lifetime of this object.
    """

    def __init__(
        self,
        base_url: str,
        api_token: str,
        verify_ssl: bool = True,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.verify_ssl = verify_ssl
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

    def _check(self, response: requests.Response, url: str) -> None:
        """Raise a descriptive RuntimeError for any unsuccessful response."""
        if response.status_code == 401:
            raise RuntimeError("PixlStash: invalid or expired API token.")
        if response.status_code == 403:
            raise RuntimeError(
                "PixlStash: token does not have access to this resource."
            )
        if response.status_code == 404:
            raise RuntimeError("PixlStash: picture/set not found.")
        if not response.ok:
            excerpt = response.text[:500] if response.text else "(empty body)"
            raise RuntimeError(
                f"PixlStash: HTTP {response.status_code} from {url}. "
                f"Response: {excerpt}"
            )

    def _request(self, method: str, path: str, **kwargs) -> requests.Response:
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
            raise RuntimeError(f"PixlStash: Request timed out for {url}.")
        except RequestsConnectionError as exc:
            raise RuntimeError(
                f"PixlStash: Connection error for {url}: {exc}"
            ) from exc
        self._check(response, url)
        return response

    # ------------------------------------------------------------------
    # Public API surface
    # ------------------------------------------------------------------

    def get(self, path: str, **kwargs) -> requests.Response:
        return self._request("GET", path, **kwargs)

    def post(self, path: str, **kwargs) -> requests.Response:
        return self._request("POST", path, **kwargs)

    def patch(self, path: str, **kwargs) -> requests.Response:
        return self._request("PATCH", path, **kwargs)

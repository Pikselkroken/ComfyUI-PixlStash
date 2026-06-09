"""Shared authenticated HTTP client for PixlStash nodes."""

from __future__ import annotations

import json
import os

import requests
import urllib3
from requests.exceptions import (
    SSLError,
    Timeout,
    ConnectionError as RequestsConnectionError,
)

VERSION = "1.3.0"
_USER_AGENT = f"ComfyUI-PixlStash/{VERSION}"

# ComfyUI Settings keys (must match the IDs registered in web/js/combo_widgets.js).
_SETTING_URL = "PixlStash.ServerURL"
_SETTING_TOKEN = "PixlStash.APIToken"
_SETTING_SSL = "PixlStash.VerifySSL"

# Shown when a node or the proxy refuses to run under ComfyUI multi-user mode.
MULTI_USER_MESSAGE = (
    "PixlStash doesn't support ComfyUI multi-user mode (--multi-user). "
    "Run a separate single-user instance for each user."
)


def make_client(url: str, token: str, verify_ssl: bool = True) -> "PixlStashClient":
    """Build a PixlStashClient from individual credential arguments."""
    return PixlStashClient(
        base_url=url,
        api_token=token,
        verify_ssl=verify_ssl,
    )


def _as_bool(value, default: bool = True) -> bool:
    """Coerce a settings value to bool (accepts JSON bools and strings)."""
    if isinstance(value, bool):
        return value
    if value is None:
        return default
    if isinstance(value, str):
        return value.strip().lower() not in ("false", "0", "no", "off", "")
    return bool(value)


def _comfy_settings() -> dict:
    """Read ComfyUI's persisted frontend settings (a flat key→value dict).

    ComfyUI writes the values set in its Settings panel to
    ``<user_directory>/default/comfy.settings.json`` server-side, so node
    execution can read them directly without the browser injecting anything
    into the prompt.  Returns ``{}`` if the file cannot be located or parsed.
    """
    try:
        import folder_paths  # noqa: PLC0415 — only available inside ComfyUI
    except Exception:
        return {}

    try:
        base = folder_paths.get_user_directory()
    except Exception:
        base = os.path.join(getattr(folder_paths, "base_path", "."), "user")

    candidates = [
        os.path.join(base, "default", "comfy.settings.json"),
        os.path.join(base, "comfy.settings.json"),
    ]
    for path in candidates:
        try:
            if os.path.isfile(path):
                with open(path, encoding="utf-8") as fh:
                    data = json.load(fh)
                if isinstance(data, dict):
                    return data
        except Exception:
            continue
    return {}


def multi_user_active() -> bool:
    """True if ComfyUI was started with --multi-user.

    A node can't tell which user submitted the running prompt, so it can't
    pick that user's stored token and must refuse rather than risk using
    someone else's.
    """
    try:
        from comfy.cli_args import args  # noqa: PLC0415, only inside ComfyUI
    except Exception:
        return False
    return bool(getattr(args, "multi_user", False))


def read_credentials(
    url: str = "",
    token: str = "",
    verify_ssl: bool = True,
) -> tuple[str, str, bool]:
    """Resolve PixlStash credentials for server-side node execution.

    Resolution order (first non-empty wins): explicit arguments →
    ComfyUI Settings (``PixlStash.*``).

    Credentials are configured in ComfyUI Settings -> PixlStash, which ComfyUI
    persists server-side, so the token never travels through the prompt or the
    saved workflow JSON.

    Raises ``RuntimeError`` under ComfyUI multi-user mode, which PixlStash
    can't support safely (a node has no way to know which user is running it).
    """
    if multi_user_active():
        raise RuntimeError(MULTI_USER_MESSAGE)

    url = (url or "").strip()
    token = (token or "").strip()

    settings = _comfy_settings() if (not url or not token) else {}

    if not url:
        url = str(settings.get(_SETTING_URL, "") or "").strip()
    if not token:
        token = str(settings.get(_SETTING_TOKEN, "") or "").strip()

    if _SETTING_SSL in settings:
        verify_ssl = _as_bool(settings.get(_SETTING_SSL))

    return url, token, verify_ssl


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

    def delete(
        self, path: str, *, is_write: bool = False, **kwargs
    ) -> requests.Response:
        return self._request("DELETE", path, is_write=is_write, **kwargs)

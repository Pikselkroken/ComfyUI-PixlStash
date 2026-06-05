"""ComfyUI server-side proxy routes for PixlStash API calls.

The browser cannot reach a PixlStash instance directly in all cases
(CORS restrictions, self-signed TLS certificates, private network
addresses).  These thin aiohttp routes act as authenticated proxies:

* The target server URL and SSL setting are resolved server-side from
  ComfyUI's persisted settings (the same ``comfy.settings.json`` the nodes
  read), NOT from the request, so these routes cannot be pointed at an
  arbitrary host (SSRF).  Any ``url`` / ``verify_ssl`` query params sent by
  older clients are ignored.
* The bearer token travels in the ``Authorization: Bearer <token>``
  header and is never echoed back or logged.

All routes are registered on ``PromptServer.instance.routes`` so they
are served by the same aiohttp application that powers the ComfyUI
backend.

The synchronous ``requests`` calls are executed in a thread-pool
executor via ``asyncio.to_thread`` so they don't block the event loop.
"""

from __future__ import annotations

import asyncio
import json
import logging

from aiohttp import web

from .connection import (
    MULTI_USER_MESSAGE,
    PixlStashClient,
    multi_user_active,
    read_credentials,
)

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _build_client(request: web.Request) -> PixlStashClient:
    """Build a client for the *configured* PixlStash server.

    The target URL and SSL setting come from ComfyUI's persisted settings,
    never from the request, so these proxy routes cannot be aimed at an
    attacker-chosen host (SSRF).  The caller must still present the API token
    in the Authorization header.

    Raises ``web.HTTPBadRequest`` on a missing token or unconfigured server.
    """
    if multi_user_active():
        raise web.HTTPBadRequest(reason=MULTI_USER_MESSAGE)

    auth = request.headers.get("Authorization", "")
    if not auth.startswith("Bearer "):
        raise web.HTTPBadRequest(
            reason="Missing or invalid Authorization header (expected 'Bearer <token>')."
        )
    token = auth[len("Bearer ") :]

    # Resolve URL + verify_ssl from ComfyUI Settings and ignore any
    # client-supplied values, so the proxy can only ever reach the user's own
    # configured instance.
    url, _settings_token, verify_ssl = read_credentials()
    if not url:
        raise web.HTTPBadRequest(
            reason="PixlStash Server URL is not configured in ComfyUI Settings › PixlStash."
        )
    if not (url.startswith("http://") or url.startswith("https://")):
        raise web.HTTPBadRequest(
            reason="Configured PixlStash Server URL must start with http:// or https://."
        )

    return PixlStashClient(base_url=url, api_token=token, verify_ssl=verify_ssl)


def _ok(data) -> web.Response:
    return web.Response(
        body=json.dumps(data).encode(),
        content_type="application/json",
    )


def _err(message: str, status: int = 502) -> web.Response:
    return web.Response(
        body=json.dumps({"error": message}).encode(),
        content_type="application/json",
        status=status,
    )


async def _proxy_get(
    request: web.Request,
    path: str,
) -> web.Response:
    """Generic proxy: forward a GET to PixlStash and return the JSON.

    Query params other than ``url`` and ``verify_ssl`` are forwarded
    as-is (e.g. ``project_id`` for filtered set / character lists).
    """
    try:
        client = _build_client(request)
    except web.HTTPBadRequest as exc:
        return _err(exc.reason, status=400)

    # Forward all query params except the ones we consumed.
    forward_params = {
        k: v for k, v in request.rel_url.query.items() if k not in ("url", "verify_ssl")
    }

    try:
        resp = await asyncio.to_thread(client.get, path, params=forward_params or None)
        return _ok(resp.json())
    except RuntimeError as exc:
        log.warning("[PixlStash proxy] %s: %s", path, exc)
        return _err(str(exc))


# ---------------------------------------------------------------------------
# Route handlers
# ---------------------------------------------------------------------------


async def proxy_projects(request: web.Request) -> web.Response:
    return await _proxy_get(request, "/api/v1/projects")


async def proxy_picture_sets(request: web.Request) -> web.Response:
    return await _proxy_get(request, "/api/v1/picture_sets")


async def proxy_characters(request: web.Request) -> web.Response:
    return await _proxy_get(request, "/api/v1/characters")


async def proxy_sort_mechanisms(request: web.Request) -> web.Response:
    return await _proxy_get(request, "/api/v1/sort_mechanisms")


async def proxy_pictures(request: web.Request) -> web.Response:
    return await _proxy_get(request, "/api/v1/pictures")


async def proxy_version(request: web.Request) -> web.Response:
    try:
        client = _build_client(request)
    except web.HTTPBadRequest as exc:
        return _err(exc.reason, status=400)
    try:
        resp = await asyncio.to_thread(client.get, "/version")
        # The endpoint may return plain text ("1.4.0") or JSON ({"version":"1.4.0"}).
        # Normalise to {"version": "..."} so the JS side always receives JSON.
        text = resp.text.strip()
        try:
            data = resp.json()
            version = data if isinstance(data, str) else data.get("version", text)
        except Exception:
            version = text
        # Sanity-check: must look like a version number, not HTML or an error page.
        import re as _re

        if not _re.match(r"^\d+\.\d+", str(version or "")):
            return _err(
                f"Server did not return a valid version string (got: {str(version)[:80]})",
                status=502,
            )
        return _ok({"version": version})
    except RuntimeError as exc:
        log.warning("[PixlStash proxy] /api/v1/version: %s", exc)
        return _err(str(exc))


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------


def register_routes() -> None:
    """Register all proxy routes on the ComfyUI PromptServer.

    Called once from ``__init__.py`` at package load time.  If ComfyUI's
    PromptServer is not available (e.g. unit-test environment), the
    registration is skipped with a warning.
    """
    try:
        from server import PromptServer  # noqa: PLC0415

        r = PromptServer.instance.routes
        r.get("/pixlstash/projects")(proxy_projects)
        r.get("/pixlstash/picture_sets")(proxy_picture_sets)
        r.get("/pixlstash/characters")(proxy_characters)
        r.get("/pixlstash/sort_mechanisms")(proxy_sort_mechanisms)
        r.get("/pixlstash/pictures")(proxy_pictures)
        r.get("/pixlstash/version")(proxy_version)
        log.info("[PixlStash] Proxy routes registered.")
    except (ImportError, AttributeError) as exc:
        log.warning("[PixlStash] Could not register proxy routes: %s", exc)

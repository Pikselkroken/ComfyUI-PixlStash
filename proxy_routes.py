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
import re

from aiohttp import web

from .connection import (
    MULTI_USER_MESSAGE,
    PixlStashClient,
    multi_user_active,
    read_credentials,
)

log = logging.getLogger(__name__)

# A model / icon is addressed by its full-file SHA-256, lowercase hex.  Anything
# else is refused before it can be interpolated into an upstream path — the same
# guard ``proxy_thumbnail`` applies with ``picture_id.isdigit()``.
_SHA256_RE = re.compile(r"[0-9a-f]{64}\Z")


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


async def proxy_thumbnail(request: web.Request) -> web.Response:
    """Proxy a picture thumbnail (binary WebP) from PixlStash.

    The browser cannot fetch thumbnails directly when PixlStash uses a
    self-signed certificate or a private address, so the picker streams them
    through this route, which reuses the same authenticated, verify-aware
    client as the JSON routes.
    """
    try:
        client = _build_client(request)
    except web.HTTPBadRequest as exc:
        return _err(exc.reason, status=400)

    raw_id = request.rel_url.query.get("picture_id", "")
    if not raw_id.isdigit():
        return _err("picture_id query param must be a positive integer.", status=400)

    path = f"/api/v1/pictures/thumbnails/{int(raw_id)}.webp"
    try:
        resp = await asyncio.to_thread(client.get, path)
    except RuntimeError as exc:
        log.warning("[PixlStash proxy] %s: %s", path, exc)
        return _err(str(exc))

    return web.Response(
        body=resp.content,
        content_type=resp.headers.get("Content-Type", "image/webp"),
    )


async def proxy_adapters(request: web.Request) -> web.Response:
    """Proxy the model shelf's adapter list.

    Filters ride through ``_proxy_get``'s query forwarding rather than being
    enumerated here, so this route needs no changes when the caller starts
    sending a different set.  The picker currently sends ``file_kind``,
    ``kind``, ``base_model`` and one of ``character_id`` / ``set_id``; it
    searches client-side over the one response rather than sending ``q``.
    """
    return await _proxy_get(request, "/api/v1/adapters")


async def proxy_model_icon(request: web.Request) -> web.Response:
    """Proxy a model-shelf icon (binary) from PixlStash.

    Same reason as ``proxy_thumbnail``: the browser can't reach a self-signed
    or private PixlStash directly.
    """
    icon_sha256 = request.rel_url.query.get("icon_sha256", "")
    # Validated before a client is even built: the value is interpolated into
    # the upstream path, so a non-digest is a malformed request, not a lookup.
    if not _SHA256_RE.match(icon_sha256):
        return _err(
            "icon_sha256 query param must be a 64-character lowercase hex digest.",
            status=400,
        )

    try:
        client = _build_client(request)
    except web.HTTPBadRequest as exc:
        return _err(exc.reason, status=400)

    path = f"/api/v1/model-icons/{icon_sha256}"
    try:
        resp = await asyncio.to_thread(client.get, path)
    except RuntimeError as exc:
        log.warning("[PixlStash proxy] %s: %s", path, exc)
        return _err(str(exc))

    return web.Response(
        body=resp.content,
        content_type=resp.headers.get("Content-Type", "image/webp"),
    )


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
        if not re.match(r"^\d+\.\d+", str(version or "")):
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
        r.get("/pixlstash/thumbnail")(proxy_thumbnail)
        r.get("/pixlstash/adapters")(proxy_adapters)
        r.get("/pixlstash/model_icon")(proxy_model_icon)
        r.get("/pixlstash/version")(proxy_version)
        log.info("[PixlStash] Proxy routes registered.")
    except (ImportError, AttributeError) as exc:
        log.warning("[PixlStash] Could not register proxy routes: %s", exc)

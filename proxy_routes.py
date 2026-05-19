"""ComfyUI server-side proxy routes for PixlStash API calls.

The browser cannot reach a PixlStash instance directly in all cases
(CORS restrictions, self-signed TLS certificates, private network
addresses).  These thin aiohttp routes act as authenticated proxies:

* ``url`` and ``verify_ssl`` travel as query parameters (not sensitive).
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

from .connection import PixlStashClient

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _build_client(request: web.Request) -> PixlStashClient:
    """Extract connection params from *request* and return a client.

    Raises ``web.HTTPBadRequest`` on missing / malformed parameters.
    """
    url = request.rel_url.query.get("url", "").strip()
    if not url:
        raise web.HTTPBadRequest(reason="Missing 'url' query parameter.")

    verify_ssl_str = request.rel_url.query.get("verify_ssl", "true").lower()
    verify_ssl = verify_ssl_str not in ("false", "0", "no")

    auth = request.headers.get("Authorization", "")
    if not auth.startswith("Bearer "):
        raise web.HTTPBadRequest(
            reason="Missing or invalid Authorization header (expected 'Bearer <token>')."
        )
    token = auth[len("Bearer "):]

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
        k: v
        for k, v in request.rel_url.query.items()
        if k not in ("url", "verify_ssl")
    }

    try:
        resp = await asyncio.to_thread(
            client.get, path, params=forward_params or None
        )
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
        log.info("[PixlStash] Proxy routes registered.")
    except (ImportError, AttributeError) as exc:
        log.warning("[PixlStash] Could not register proxy routes: %s", exc)

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
# guard ``_positive_id`` is for the routes addressed by row id.
_SHA256_RE = re.compile(r"[0-9a-f]{64}\Z")


# A row id as it is actually written: no sign, no padding, no separators, and
# ASCII.  ``isdigit()`` was the guard here and it is true of all of ``"0"``,
# ``"007"`` and ``"٧"`` — so a route documenting "positive integer" forwarded
# ids no row can have, and ``int()`` then *renumbered* two of those three into
# a row that does exist.  Refusing beats guessing which row the caller meant.
_ID_RE = re.compile(r"[1-9][0-9]*\Z")


def _positive_id(raw: str) -> int | None:
    """``raw`` as a positive row id, or ``None`` if it is not written as one."""
    return int(raw) if _ID_RE.match(raw) else None


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

    picture_id = _positive_id(request.rel_url.query.get("picture_id", ""))
    if picture_id is None:
        return _err("picture_id query param must be a positive integer.", status=400)

    path = f"/api/v1/pictures/thumbnails/{picture_id}.webp"
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


async def proxy_adapter(request: web.Request) -> web.Response:
    """Proxy one shelf record by hash.

    What the node's Browse button needs to draw a *name* on a workflow that
    was saved with only a hash in it. Fetching the whole filtered shelf to find
    one row would work and would be absurd — this is one small request per
    node.

    Validated before a client is built, like the icon route: the digest is
    interpolated into the upstream path, so a non-digest is a malformed
    request rather than a lookup that happens to miss.
    """
    sha256 = request.rel_url.query.get("sha256", "")
    if not _SHA256_RE.match(sha256):
        return _err(
            "sha256 query param must be a 64-character lowercase hex digest.",
            status=400,
        )
    return await _proxy_get(request, f"/api/v1/adapters/{sha256}")


async def proxy_checkpoints(request: web.Request) -> web.Response:
    """Proxy the model shelf's checkpoint list.

    A separate route because checkpoints are a separate one upstream: they are
    listed by ``id`` (``sha256`` is null until the background hasher gets to
    the file) and are refused by the hash-addressed adapter routes.
    """
    return await _proxy_get(request, "/api/v1/checkpoints")


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


async def proxy_entity_thumbnail(request: web.Request) -> web.Response:
    """Proxy the thumbnail of the character / set a model is attached to.

    Most adapters carry no icon of their own — on a real shelf almost none do —
    so the picker borrows the face of whoever the model is attached to, which is
    what PixlStash's own shelf draws (``ModelMark``'s fallback chain).  A LoRA of
    a person is far better identified by that person's face than by two letters.

    Two entity types, because ``attachments[].entity_type`` has two values.  The
    type picks the upstream path from a fixed table rather than being
    interpolated into one, so an unknown value is a 400 here and never a request.
    """
    upstream = {
        "character": "/api/v1/characters/{}/thumbnail",
        "set": "/api/v1/picture_sets/{}/thumbnail",
    }.get(request.rel_url.query.get("entity_type", ""))
    if upstream is None:
        return _err("entity_type query param must be 'character' or 'set'.", status=400)

    entity_id = _positive_id(request.rel_url.query.get("entity_id", ""))
    if entity_id is None:
        return _err("entity_id query param must be a positive integer.", status=400)

    try:
        client = _build_client(request)
    except web.HTTPBadRequest as exc:
        return _err(exc.reason, status=400)

    path = upstream.format(entity_id)
    try:
        resp = await asyncio.to_thread(client.get, path)
    except RuntimeError as exc:
        # An entity with no picture 404s, which is ordinary rather than broken —
        # the caller falls back to the generated mark. Logged at debug so a
        # cast of faceless characters doesn't fill the console on every grid.
        log.debug("[PixlStash proxy] %s: %s", path, exc)
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
        r.get("/pixlstash/adapter")(proxy_adapter)
        r.get("/pixlstash/checkpoints")(proxy_checkpoints)
        r.get("/pixlstash/model_icon")(proxy_model_icon)
        r.get("/pixlstash/entity_thumbnail")(proxy_entity_thumbnail)
        r.get("/pixlstash/version")(proxy_version)
        log.info("[PixlStash] Proxy routes registered.")
    except (ImportError, AttributeError) as exc:
        log.warning("[PixlStash] Could not register proxy routes: %s", exc)

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
    MIN_SERVER_VERSION,
    MULTI_USER_MESSAGE,
    PixlStashClient,
    multi_user_active,
    read_credentials,
)

# A model / icon is addressed by its full-file SHA-256, lowercase hex.  Anything
# else is refused before it can be interpolated into an upstream path — the same
# guard ``_positive_id`` is for the routes addressed by row id.  One definition
# for the whole package, in ``nodes.lock`` because that is the leaf that has no
# imports of its own; what counts as a digest is a thing to change once.
from .nodes.lock import SHA256_RE as _SHA256_RE

log = logging.getLogger(__name__)


# A row id as it is actually written: no sign, no padding, no separators, and
# ASCII.  ``isdigit()`` was the guard here and it is true of all of ``"0"``,
# ``"007"`` and ``"٧"`` — so a route documenting "positive integer" forwarded
# ids no row can have, and ``int()`` then *renumbered* two of those three into
# a row that does exist.  Refusing beats guessing which row the caller meant.
_ID_RE = re.compile(r"[1-9][0-9]*\Z")

# A PixlStash workflow id: ``auto:`` and the core hash of an automatic group,
# or ``manual:`` and the uuid hex of a manual workflow. ASCII classes, so ``fullmatch``
# cannot be satisfied by a trailing newline or a non-ASCII digit.
_WORKFLOW_ID_RE = re.compile(r"auto:[0-9a-f]{64}|manual:[0-9a-f]{32}")


def _positive_id(raw: str) -> int | None:
    """``raw`` as a positive row id, or ``None`` if it is not written as one."""
    return int(raw) if _ID_RE.match(raw) else None


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _build_client(
    request: web.Request, min_server_version: str = MIN_SERVER_VERSION
) -> PixlStashClient:
    """Build a client for the *configured* PixlStash server.

    *min_server_version* is the PixlStash a route's upstream path first
    shipped in, so an older server is refused in plain words and not with the
    404 of a path it does not have.

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

    return PixlStashClient(
        base_url=url,
        api_token=token,
        verify_ssl=verify_ssl,
        min_server_version=min_server_version,
    )


def _ok(data, headers: dict | None = None) -> web.Response:
    return web.Response(
        body=json.dumps(data).encode(),
        content_type="application/json",
        headers=headers or None,
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
    min_server_version: str = MIN_SERVER_VERSION,
    pass_on: tuple[str, ...] = (),
) -> web.Response:
    """Generic proxy: forward a GET to PixlStash and return the JSON.

    Query params other than ``url`` and ``verify_ssl`` are forwarded
    as-is (e.g. ``project_id`` for filtered set / character lists).
    *pass_on* names response headers of PixlStash's to hand on as they are.
    """
    try:
        client = _build_client(request, min_server_version)
    except web.HTTPBadRequest as exc:
        return _err(exc.reason, status=400)

    # Forward all query params except the ones we consumed.
    forward_params = {
        k: v for k, v in request.rel_url.query.items() if k not in ("url", "verify_ssl")
    }

    try:
        resp = await asyncio.to_thread(client.get, path, params=forward_params or None)
        kept = {name: resp.headers[name] for name in pass_on if name in resp.headers}
        return _ok(resp.json(), kept)
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


# The header of PixlStash's picture list that says which library its ids are
# of. The picture picker writes it beside the ids it picks.
LIBRARY_HEADER = "X-PixlStash-Library"


async def proxy_pictures(request: web.Request) -> web.Response:
    return await _proxy_get(request, "/api/v1/pictures", pass_on=(LIBRARY_HEADER,))


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


async def proxy_workflow_sets(request: web.Request) -> web.Response:
    """Proxy the model shelf's workflow sets (the hand-made ones ship in 1.12)."""
    return await _proxy_get(request, "/api/v1/models/workflow-sets")


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


def _workflow_key(raw) -> str | None:
    """*raw* as a PixlStash workflow id, or ``None``.

    The id is interpolated into the upstream path, so anything else is refused
    before a client is built.
    """
    return raw if isinstance(raw, str) and _WORKFLOW_ID_RE.fullmatch(raw) else None


_NOT_A_WORKFLOW_ID = "workflow_key must be a PixlStash workflow id."

# PixlStash's workflow routes (a workflow's graph and its people, the
# whole-graph save, the export) first ship in 1.12.0.
_WORKFLOWS_MIN_VERSION = "1.12.0"

# An opening's id, as ``workflow_link.js`` makes it: 32 hex characters. What
# PixlStash holds a run from its Run popup as (``run``) has the same shape.
_OPENING_RE = re.compile(r"[0-9a-f]{32}")

_NOT_A_RUN = "run must be what PixlStash holds a run as: 32 hex characters."


async def proxy_workflow_graph(request: web.Request) -> web.Response:
    """Proxy one PixlStash workflow's runnable graph, for *Open in ComfyUI*.

    PixlStash's Workflow tab opens ComfyUI with ``?pixlstash_workflow=<id>``;
    ``open_workflow.js`` reads it and fetches the graph through here, with
    the layout it was last saved with and where the loader for people goes.
    The id is ``auto:`` and a core hash, or ``manual:`` and a 32-character
    uuid hex. A link from the Run popup (``open_run.js``) adds ``run``, which
    goes on with the rest of the query: PixlStash then answers the graph it
    holds for that run. Anything else in its place is refused here.
    """
    workflow_key = _workflow_key(request.rel_url.query.get("workflow_key"))
    if workflow_key is None:
        return _err(_NOT_A_WORKFLOW_ID, status=400)
    run = request.rel_url.query.get("run")
    if run is not None and not _OPENING_RE.fullmatch(run):
        return _err(_NOT_A_RUN, status=400)
    return await _proxy_get(
        request, f"/api/v1/workflows/{workflow_key}/graph", _WORKFLOWS_MIN_VERSION
    )


async def proxy_workflow_people(request: web.Request) -> web.Response:
    """Proxy who a PixlStash workflow can make pictures of.

    The people the Run popup offers for the workflow: the Multi Adapter
    Loader's people view asks it on a canvas opened from PixlStash, so the two
    offer the same people by the same rule.
    """
    workflow_key = _workflow_key(request.rel_url.query.get("workflow_key"))
    if workflow_key is None:
        return _err(_NOT_A_WORKFLOW_ID, status=400)
    return await _proxy_get(
        request, f"/api/v1/workflows/{workflow_key}/people", _WORKFLOWS_MIN_VERSION
    )


async def _proxy_write(
    request: web.Request, method: str, path: str, payload: dict
) -> web.Response:
    """Send *payload* to PixlStash with the client's *method* (``post``,
    ``put``) and hand its answer back as it is.

    PixlStash validates what it is sent, and its status and ``detail`` come
    back unchanged, so a 400, 403, 404 or 413 reaches the toast word for word.
    Both writes there are (the export, the save) are workflow routes.
    """
    try:
        client = _build_client(request, _WORKFLOWS_MIN_VERSION)
    except web.HTTPBadRequest as exc:
        return _err(exc.reason, status=400)
    try:
        resp = await asyncio.to_thread(
            getattr(client, method), path, is_write=True, json=payload
        )
        return _ok(resp.json())
    except RuntimeError as exc:
        log.warning("[PixlStash proxy] %s: %s", path, exc)
        return _err(
            getattr(exc, "detail", "") or str(exc), status=getattr(exc, "status", 502)
        )


async def _both_documents(request: web.Request) -> dict | None:
    """The request's JSON when it holds a ``workflow`` and an ``output`` object."""
    try:
        body = await request.json()
    except ValueError:
        return None
    if (
        isinstance(body, dict)
        and isinstance(body.get("workflow"), dict)
        and isinstance(body.get("output"), dict)
    ):
        return body
    return None


_NEEDS_BOTH = "workflow and output must both be JSON objects."


async def proxy_workflow_convert(request: web.Request) -> web.Response:
    """Forward *Save as new workflow in PixlStash*'s ``{name, workflow, output}`` to PixlStash."""
    body = await _both_documents(request)
    if body is None:
        return _err(_NEEDS_BOTH, status=400)
    return await _proxy_write(
        request, "post", "/api/v1/comfyui/workflows/convert", body
    )


async def proxy_workflow_save(
    request: web.Request, as_new: bool = False
) -> web.Response:
    """Forward a save of a workflow opened from PixlStash, as its next version.

    ``save_workflow.js`` posts ``{workflow_key, workflow, output}`` when
    ComfyUI saves a canvas linked to a PixlStash workflow: ``output`` is the
    runnable graph PixlStash stores, and ``workflow`` the editor document it
    keeps beside it as the layout. ``read_version``, ``waiting_applied`` and
    ``opening`` are what the canvas was opened with. *as_new* is
    :func:`proxy_workflow_save_new`'s: the graph is stored as a new workflow.

    The one upstream call is PixlStash's whole-graph save. It adds a version
    or a new workflow, keeps the layout, and with ``waiting_applied`` clears
    the changes that waited in PixlStash's Workflow tab, which the graph was
    opened with. It deletes no workflow and changes no recipe; PixlStash
    decides what the caller's key may do.
    """
    body = await _both_documents(request)
    if body is None:
        return _err(_NEEDS_BOTH, status=400)
    workflow_key = _workflow_key(body.get("workflow_key"))
    if workflow_key is None:
        return _err(_NOT_A_WORKFLOW_ID, status=400)
    payload = {
        "workflow": body["output"],
        "editor": body["workflow"],
        "source": "comfyui",
        "waiting_applied": body.get("waiting_applied") is True,
    }
    # Sent only when known: its absence is what says "not read at a version".
    read_version = body.get("read_version")
    if isinstance(read_version, int) and not isinstance(read_version, bool):
        payload["read_version"] = read_version
    # Which opening the save is of, so PixlStash keeps an opening's saves as
    # one version. Made by the browser; anything else is not forwarded.
    opening = body.get("opening")
    if isinstance(opening, str) and _OPENING_RE.fullmatch(opening):
        payload["opening"] = opening
    # Said by the route and never by the body: see `proxy_workflow_save_new`.
    if as_new:
        payload["as_new"] = True
    return await _proxy_write(
        request, "put", f"/api/v1/workflows/{workflow_key}/graph", payload
    )


async def proxy_workflow_save_new(request: web.Request) -> web.Response:
    """Forward a save of a canvas opened from PixlStash's Run popup, which is
    stored as a NEW workflow and never as a version of the one it names.

    A route of its own and not a flag in the save's body, so that it fails
    closed: this file is loaded when ComfyUI starts and the script that calls
    it when the page does. A ComfyUI not restarted since the pack was updated
    has the new script and the old routes, and a flag the old save route does
    not know would be dropped, saving the run over the workflow it came from.
    Here that ComfyUI answers 404 and nothing is stored.
    """
    return await proxy_workflow_save(request, as_new=True)


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
        r.get("/pixlstash/workflow_sets")(proxy_workflow_sets)
        r.get("/pixlstash/model_icon")(proxy_model_icon)
        r.get("/pixlstash/entity_thumbnail")(proxy_entity_thumbnail)
        r.get("/pixlstash/version")(proxy_version)
        r.get("/pixlstash/workflow_graph")(proxy_workflow_graph)
        r.get("/pixlstash/workflow_people")(proxy_workflow_people)
        r.post("/pixlstash/workflows/convert")(proxy_workflow_convert)
        r.post("/pixlstash/workflows/save")(proxy_workflow_save)
        r.post("/pixlstash/workflows/save_new")(proxy_workflow_save_new)
        log.info("[PixlStash] Proxy routes registered.")
    except (ImportError, AttributeError) as exc:
        log.warning("[PixlStash] Could not register proxy routes: %s", exc)

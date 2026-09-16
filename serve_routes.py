"""Routes this package *serves to* PixlStash, rather than proxies for it.

Everything in ``proxy_routes`` points outward: the browser asks ComfyUI, and
ComfyUI asks the user's own PixlStash.  These two point the other way.
PixlStash asks ComfyUI what it has (``GET /pixlstash/inventory``) and hands it a
file to work on (``POST /pixlstash/assets``) — what a PixlStash that renders
through someone's ComfyUI needs before it can submit a prompt at all.  The
answers come back in ComfyUI's own vocabulary, so the caller can go straight on
to ``POST /prompt`` without translating anything.

**Nothing in this package calls either of them.**  They ship dormant, for a
PixlStash release that has not shipped yet; the contract is here so both ends
can be written against it.

Unlike the proxy routes, these *are* an authentication boundary.  A proxy route
forwards whatever token the caller presents and lets PixlStash decide about it;
these run locally, and one of them writes a file.  ComfyUI's HTTP server is
routinely bound to a LAN address with no auth in front of it, so the bearer
token is compared here against the API token in ComfyUI Settings — the secret
PixlStash issued and both ends already hold.  Nobody who cannot already read
the user's vault can list their models or drop a file in their input directory.
"""

from __future__ import annotations

import contextlib
import hmac
import logging
import os
import re

from aiohttp import web

from .connection import (
    MULTI_USER_MESSAGE,
    VERSION,
    multi_user_active,
    read_credentials,
)

# Same JSON envelope the proxy routes answer in, so a caller parses one shape
# across the whole ``/pixlstash/*`` namespace. Private to the package, not to
# the module.
from .proxy_routes import _err, _ok

log = logging.getLogger(__name__)

# Uploaded assets land in a subfolder of ComfyUI's input directory rather than
# its root: a workflow refers to an input image as "<subfolder>/<name>", so the
# prefix is free, and what PixlStash pushed stays distinguishable from what the
# user dropped in by hand.
ASSET_SUBFOLDER = "pixlstash"

# The model folders this package's own loaders can fill. ComfyUI's ``/object_info``
# already enumerates every node and every other folder, so this reports the four
# that PixlStash has a shelf for and leaves the rest to ComfyUI's own route.
#
# ``clip`` is what a ComfyUI older than the rename calls the text-encoder
# folder; ``clip_loader`` has to know that too.
_MODEL_KINDS: dict[str, tuple[str, ...]] = {
    "checkpoints": ("checkpoints",),
    "loras": ("loras",),
    "vae": ("vae",),
    "text_encoders": ("text_encoders", "clip"),
}

# A filename we are willing to create. No directory separators, no drive
# letter, no leading dot, and bounded — the name arrives on the wire and is
# joined to a directory, so this is the containment check and not a nicety.
# Deliberately a whitelist: a blacklist of "../" and friends is the version of
# this that keeps being bypassed.
_NAME_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}\Z")

# ponytail: a flat ceiling rather than a configurable one. It is here so a
# stuck or hostile upload cannot fill the disk; make it a setting if anyone
# ever has a legitimate input image bigger than this.
MAX_ASSET_BYTES = 64 * 1024 * 1024


def _discard(path: str) -> None:
    with contextlib.suppress(OSError):
        os.remove(path)


def _refusal(request: web.Request) -> web.Response | None:
    """``None`` if the caller holds the configured API token, else the refusal.

    Returned rather than raised: both handlers are the only callers, and a
    response reads better at the call site than a second exception type.

    ``compare_digest`` rather than ``==`` because the right-hand side is a
    secret, and a comparison that short-circuits on the first wrong byte hands
    it over one byte at a time.
    """
    if multi_user_active():
        return _err(MULTI_USER_MESSAGE, status=400)

    _url, token, _verify_ssl = read_credentials()
    if not token:
        # 503 and not 401: there is nothing the caller could have sent that
        # would have worked, and the fix is in the user's ComfyUI Settings.
        return _err(
            "No PixlStash API Token is configured in ComfyUI Settings › "
            "PixlStash, so this ComfyUI cannot authenticate anyone.",
            status=503,
        )

    auth = request.headers.get("Authorization", "")
    presented = auth[len("Bearer ") :] if auth.startswith("Bearer ") else ""
    if not hmac.compare_digest(presented.encode(), token.encode()):
        return _err(
            "This route requires the PixlStash API token configured in ComfyUI "
            "Settings, as 'Authorization: Bearer <token>'.",
            status=401,
        )
    return None


def _filenames(kinds: tuple[str, ...]) -> list[str]:
    """Model filenames for the first of ``kinds`` this ComfyUI knows about.

    Names relative to the models directory, which is what a node's combo widget
    holds — the absolute paths they sit at are this machine's business and are
    deliberately not reported.
    """
    import folder_paths  # noqa: PLC0415 — only available inside ComfyUI

    for kind in kinds:
        try:
            names = folder_paths.get_filename_list(kind)
        except Exception:  # noqa: BLE001 — an unregistered folder is not an error
            continue
        if names:
            return sorted(names)
    return []


async def inventory(request: web.Request) -> web.Response:
    """What this ComfyUI could run, as far as PixlStash's shelf is concerned.

    ``{"package_version": "1.4.0", "models": {"checkpoints": [...], ...}}``.

    The point is the gap: a shelf row whose file is not in this list has to be
    fetched (or, for a checkpoint, cannot be used here at all), and PixlStash
    can see that before it submits a prompt rather than after it fails.
    """
    problem = _refusal(request)
    if problem is not None:
        return problem

    return _ok(
        {
            "package_version": VERSION,
            "models": {name: _filenames(kinds) for name, kinds in _MODEL_KINDS.items()},
        }
    )


async def upload_asset(request: web.Request) -> web.Response:
    """Take one file into ComfyUI's input directory and say what to call it.

    Multipart, in a field named ``file``.  The reply is the shape ComfyUI's own
    ``/upload/image`` answers with — ``{"name", "subfolder", "type"}`` — because
    a workflow refers to an input image as ``<subfolder>/<name>`` and the caller
    has to build that string to put in the prompt it submits next.
    """
    problem = _refusal(request)
    if problem is not None:
        return problem

    try:
        reader = await request.multipart()
    except Exception:  # noqa: BLE001 — a non-multipart body is a 400, not a 500
        return _err("Send the file as a multipart/form-data body.", status=400)

    field = await reader.next()
    while field is not None and getattr(field, "name", None) != "file":
        field = await reader.next()
    if field is None:
        return _err("No multipart field named 'file' in the body.", status=400)

    # No basename() ahead of this. Stripping the directory part would turn
    # "../../etc/cron.d/pwn" into the perfectly acceptable "pwn" and store it,
    # which is contained but silent — a caller sending a path it thinks is
    # meaningful should hear that it is not, rather than find its file under a
    # name it never chose.
    name = str(field.filename or "")
    if not _NAME_RE.match(name):
        return _err(
            "The file's name must be 1-128 characters of A-Z, a-z, 0-9, dot, "
            "dash or underscore, starting with a letter or digit.",
            status=400,
        )

    import folder_paths  # noqa: PLC0415 — only available inside ComfyUI

    directory = os.path.join(folder_paths.get_input_directory(), ASSET_SUBFOLDER)
    os.makedirs(directory, exist_ok=True)
    final = os.path.join(directory, name)

    # Written under .part and renamed only once the whole body is on disk, as
    # the shelf download does: a dropped connection must not leave a truncated
    # file under a name a submitted workflow is about to load.
    part = f"{final}.part"
    written = 0
    try:
        with open(part, "wb") as fh:
            while True:
                chunk = await field.read_chunk()
                if not chunk:
                    break
                written += len(chunk)
                if written > MAX_ASSET_BYTES:
                    raise ValueError("too large")
                fh.write(chunk)
    except ValueError:
        _discard(part)
        return _err(
            f"The file is larger than the {MAX_ASSET_BYTES} byte limit.",
            status=413,
        )
    except BaseException:
        _discard(part)
        raise

    os.replace(part, final)
    log.info("[PixlStash] Stored asset %s (%d bytes).", name, written)
    return _ok({"name": name, "subfolder": ASSET_SUBFOLDER, "type": "input"})


def register_routes() -> None:
    """Register the served routes on the ComfyUI PromptServer.

    Called once from ``__init__.py``, beside the proxy registration. Skipped
    with a warning where PromptServer is absent (the unit-test environment).
    """
    try:
        from server import PromptServer  # noqa: PLC0415

        r = PromptServer.instance.routes
        r.get("/pixlstash/inventory")(inventory)
        r.post("/pixlstash/assets")(upload_asset)
        log.info("[PixlStash] Served routes registered.")
    except (ImportError, AttributeError) as exc:
        log.warning("[PixlStash] Could not register served routes: %s", exc)

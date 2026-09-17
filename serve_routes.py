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

import asyncio
import contextlib
import errno
import hmac
import logging
import os
import re

from aiohttp import web

from .connection import (
    MIN_SERVER_VERSION,
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
# this that keeps being bypassed. Spaces are in it because "my photo.png" is an
# ordinary filename and refusing it would be a bug of its own; non-ASCII is
# not, so a caller holding a "café.png" renames it — this route is
# machine-to-machine and the caller chooses the name it sends.
_NAME_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9 ._-]{0,127}\Z")

# Windows opens these as *devices* whatever the extension, so "NUL.png" is not
# a file that can be created: every write succeeds against the device and the
# rename afterwards fails with an OSError nobody expected. A trailing dot or
# space is silently stripped there too, which would make the name reported back
# not the name on disk — and the caller builds a workflow reference out of it.
_RESERVED_STEMS = frozenset(
    ["CON", "PRN", "AUX", "NUL"]
    + [f"{port}{n}" for port in ("COM", "LPT") for n in range(1, 10)]
)

# What ComfyUI can actually load as an input image — and not a nicety either.
# ComfyUI's own ``GET /view`` serves anything under the input directory back
# over ComfyUI's origin, with a content type guessed from the extension and no
# attachment disposition. An ``.html`` or ``.svg`` dropped in here is therefore
# stored XSS on that origin, with read access to ``comfy.settings.json`` — which
# is where the PixlStash URL and API token live. The whole point of this route
# being an auth boundary is undone if what gets through it can read the secret
# that guards it.
_ALLOWED_SUFFIXES = frozenset(
    (".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp", ".tif", ".tiff")
)

# ponytail: a flat ceiling rather than a configurable one. It is here so a
# stuck or hostile upload cannot fill the disk; make it a setting if anyone
# ever has a legitimate input image bigger than this.
MAX_ASSET_BYTES = 64 * 1024 * 1024


def _discard(path: str) -> None:
    with contextlib.suppress(OSError):
        os.remove(path)


def _asset_name(raw) -> str | None:
    """``raw`` if it is a filename this route will create, else ``None``.

    Split out of the handler so the containment rule can be exercised on its
    own — the alternative is a test that proves a traversal was refused by
    checking that a directory it was never going to reach is still empty, which
    passes just as happily when the traversal succeeded somewhere else.
    """
    name = str(raw or "")
    if not _NAME_RE.match(name):
        return None
    # match() guarantees at least one character, so [-1] is safe.
    if name[-1] in ". ":
        return None
    if name.split(".")[0].upper() in _RESERVED_STEMS:
        return None
    if os.path.splitext(name)[1].lower() not in _ALLOWED_SUFFIXES:
        return None
    return name


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

    # The scheme is matched case-insensitively (RFC 7235 says it is), and only
    # the scheme: the token itself stays on compare_digest.
    scheme, _space, presented = request.headers.get("Authorization", "").partition(" ")
    if scheme.lower() != "bearer":
        presented = ""
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
        except KeyError:
            # The only case that is not an error: a folder this ComfyUI never
            # registered. Anything else — a dead NFS mount, a permission
            # change — is a real failure, and answering [] for it would tell
            # PixlStash "no models of that kind are here", which is the one
            # sentence this route must not say by accident.
            continue
        if names:
            return sorted(names)
    return []


async def inventory(request: web.Request) -> web.Response:
    """What this ComfyUI could run, as far as PixlStash's shelf is concerned.

    ``{"package_version": …, "min_server_version": …, "models": {…}}``.

    ``package_version`` is ``connection.VERSION``, which
    ``tests/test_server_version`` keeps equal to ``pyproject.toml``.

    ``min_server_version`` is the other half of the handshake the client does in
    the opposite direction: the nodes refuse a PixlStash older than this, so a
    PixlStash asking what it can drive here wants to know the floor before it
    submits a prompt that would be refused — and computing it from a table of
    package versions is the version of that which goes stale.

    The ``models`` gap is the point of the rest: a shelf row whose file is not
    in this list has to be fetched (or, for a checkpoint, cannot be used here at
    all), and PixlStash can see that before it submits rather than after it
    fails.
    """
    problem = _refusal(request)
    if problem is not None:
        return problem

    # to_thread, for the reason proxy_routes gives for its own: this walks the
    # models directories whenever ComfyUI's cache is cold or a mtime moved, and
    # that is not a thing to do on the event loop that carries every other
    # client's progress updates.
    named = list(_MODEL_KINDS.items())
    try:
        scanned = await asyncio.gather(
            *(asyncio.to_thread(_filenames, kinds) for _name, kinds in named)
        )
    except OSError as exc:
        # Reported rather than flattened into empty lists — see _filenames.
        return _err(
            f"Could not read this ComfyUI's model folders: {exc.strerror}.",
            status=500,
        )
    models = {name: names for (name, _kinds), names in zip(named, scanned)}
    return _ok(
        {
            "package_version": VERSION,
            "min_server_version": MIN_SERVER_VERSION,
            "models": models,
        }
    )


async def upload_asset(request: web.Request) -> web.Response:
    """Take one file into ComfyUI's input directory and say what to call it.

    Multipart, the **first** field, named ``file``, an image type ComfyUI can
    load.  The reply is the shape ComfyUI's own ``/upload/image`` answers with —
    ``{"name", "subfolder", "type"}`` — because a workflow refers to an input
    image as ``<subfolder>/<name>`` and the caller has to build that string to
    put in the prompt it submits next.

    A name already in use is a 409 and never a replacement.  Every other reason
    the write cannot start is a 500 naming it: a full disk reported as a name
    conflict sends the caller off retrying under new names forever.
    """
    problem = _refusal(request)
    if problem is not None:
        return problem

    try:
        reader = await request.multipart()
    except Exception:  # noqa: BLE001 — a non-multipart body is a 400, not a 500
        return _err("Send the file as a multipart/form-data body.", status=400)

    # The FIRST field, and it has to be the file. Skipping forward to find it
    # means reader.next() drains whatever precedes it — and it drains by
    # reading, with no limit, so a 20 GB field named anything at all would be
    # swallowed whole before MAX_ASSET_BYTES was ever consulted. aiohttp's
    # client_max_size is not applied to streaming multipart reads either. One
    # field is also all this route was ever specified to take.
    field = await reader.next()
    if field is None or getattr(field, "name", None) != "file":
        return _err(
            "Send the file as the first multipart field, named 'file'. "
            "Nothing before it is read.",
            status=400,
        )

    # No basename() ahead of this. Stripping the directory part would turn
    # "../../etc/cron.d/pwn" into the perfectly acceptable "pwn" and store it,
    # which is contained but silent — a caller sending a path it thinks is
    # meaningful should hear that it is not, rather than find its file under a
    # name it never chose.
    name = _asset_name(field.filename)
    if name is None:
        return _err(
            "The file's name must be 1-128 characters of A-Z, a-z, 0-9, space, "
            "dot, dash or underscore, must start with a letter or digit, must "
            "not end with a dot or a space, and must not be a Windows device "
            "name (CON, NUL, COM1 …).",
            status=400,
        )

    import folder_paths  # noqa: PLC0415 — only available inside ComfyUI

    directory = os.path.join(folder_paths.get_input_directory(), ASSET_SUBFOLDER)
    os.makedirs(directory, exist_ok=True)
    final = os.path.join(directory, name)
    # lexists, not exists: the latter is False for a symlink whose target is
    # gone, and os.replace would then quietly consume the link — so a prompt
    # already queued against this name would start resolving to other bytes,
    # which is the exact thing the 409 below promises cannot happen.
    if os.path.lexists(final):
        return _err(
            f"An asset named {name!r} is already here. Assets are never "
            "replaced: a prompt already sitting in ComfyUI's queue against "
            "this name would start loading bytes it was not submitted "
            "against. Send it under another name.",
            status=409,
        )

    # Written under .part and renamed only once the whole body is on disk, as
    # the shelf download does: a dropped connection must not leave a truncated
    # file under a name a submitted workflow is about to load.
    #
    # os.open with O_CREAT|O_EXCL|O_NOFOLLOW rather than open(part, "wb"), and
    # it is doing three jobs. It refuses to follow a symlink someone pre-created
    # at this path, where "wb" would happily write through it. It makes two
    # concurrent uploads of one name a refusal for the second rather than two
    # writers interleaving chunks into a single corrupt file — the shelf
    # download can argue that ComfyUI runs one prompt at a time, and an aiohttp
    # handler cannot borrow that argument. And it does not silently adopt the
    # leftovers of a crashed upload.
    #
    # ponytail: the exists() check above and this open are not one atomic step,
    # so two callers can still both pass the check and the later one win. The
    # window is microseconds and the loser gets its own file back; a lock or an
    # O_EXCL on `final` itself is the fix if anyone ever uploads concurrently
    # under one name on purpose.
    # O_BINARY is not optional. Windows leaves an os.open fd in the CRT's text
    # mode, where every 0x0A written is expanded to 0x0D 0x0A — the file lands
    # longer than `written`, its digest is not the digest of what was sent, and
    # LoadImage cannot decode it. os.fdopen(..., "wb") does not undo that; the
    # flag does. CPython's own tempfile carries the same line for the same
    # reason, and builtin open(..., "wb") was safe, so this arrived with the
    # switch to os.open.
    part = f"{final}.part"
    flags = (
        os.O_WRONLY
        | os.O_CREAT
        | os.O_EXCL
        | getattr(os, "O_NOFOLLOW", 0)
        | getattr(os, "O_BINARY", 0)
    )
    try:
        handle = os.open(part, flags, 0o600)
    except OSError as exc:
        # Only these two mean "someone else is here". A full disk, a read-only
        # filesystem or a permission change answered 409 "another upload may be
        # in flight", which sends the caller off to retry under a new name and
        # the operator off looking for an upload that does not exist.
        if exc.errno in (errno.EEXIST, errno.ELOOP):
            return _err(
                f"Could not start writing {name!r}: another upload of this "
                "name is in flight, or something is already at its path.",
                status=409,
            )
        return _err(f"Could not start writing {name!r}: {exc.strerror}.", status=500)

    written = 0
    oversize = False
    try:
        with os.fdopen(handle, "wb") as fh:
            while True:
                chunk = await field.read_chunk()
                if not chunk:
                    break
                written += len(chunk)
                if written > MAX_ASSET_BYTES:
                    # A flag and a break, not a raise: aiohttp raises its own
                    # ValueError for a malformed or truncated multipart body,
                    # and an `except ValueError` around this loop told the
                    # sender of a truncated 200 kB PNG that it exceeded a 64 MB
                    # limit.
                    oversize = True
                    break
                # ponytail: a synchronous write on the event loop. Local disk,
                # so it is bounded by the chunk and not by the client, and
                # to_thread per 8 kB chunk would cost more hops than it saves.
                # Revisit if a slow filesystem (a network mount) ever shows up
                # as a stalled progress bar.
                fh.write(chunk)
        if oversize:
            _discard(part)
            return _err(
                f"The file is larger than the {MAX_ASSET_BYTES} byte limit.",
                status=413,
            )
        # Inside the try, so a failure here cleans up too — the promise above
        # is that a failed upload leaves nothing behind, and a rename is as
        # capable of failing as a write.
        os.replace(part, final)
    except ValueError as exc:
        _discard(part)
        return _err(f"The multipart body could not be read: {exc}", status=400)
    except BaseException:
        _discard(part)
        raise

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

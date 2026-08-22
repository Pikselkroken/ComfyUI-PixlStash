"""Getting a file off the PixlStash model shelf onto this machine.

Shared by every loader in this package that names a shelf file — adapters,
VAEs, text encoders, checkpoints.  Resolution is the half of those nodes with
all the failure modes (a bad digest, a token without shelf scope, a copy that
is on the shelf but not on this disk) and none of them needs a MODEL to
exercise, so it lives here rather than in any one node.

Resolution order:

1. A copy the shelf records as ``present`` whose path also resolves, *here*, to
   a file of the size the shelf recorded.  See ``local_path`` for why the size
   is not optional: the shelf's paths are the **PixlStash host's** paths, and
   ComfyUI is not always on that host.
2. Otherwise a download into ``<folder>/pixlstash/<sha256>.safetensors``, whose
   digest is verified before anything is cached.  ``folder`` is the ComfyUI
   models directory for the kind — ``loras``, ``vae``, ``text_encoders``.

Step 2 is not available to every caller: the shelf serves bytes for
hash-addressed files only, and refuses checkpoints by design (``GET
/adapters/{sha256}/file`` answers "that hash is a checkpoint").  The checkpoint
loader therefore passes ``download=False`` and gets step 1 or an error.

``label`` threads the calling node's display name through every message, so a
failure names the node the user is looking at rather than this module.
"""

from __future__ import annotations

import contextlib
import hashlib
import logging
import os
import re

from ..connection import make_client, read_credentials

log = logging.getLogger(__name__)

_SHA256_RE = re.compile(r"[0-9a-f]{64}\Z")

# The shelf is a catalogue of safetensors and nothing else: PixlStash's folder
# scanner skips every other extension outright (``MODEL_SUFFIX`` in
# ``pixlstash/services/model_folder_scanner.py``), and its adapter kinds are
# read out of a safetensors header.  So this is defence in depth against a
# server sending something unexpected, not a filter that hides a real class of
# adapter — and it is why the download cache can name every file it writes
# ``.safetensors`` without inspecting the body.  If the shelf ever learns to
# catalogue ``.pt`` / ``.ckpt``, both uses have to change together.
_SUFFIX = ".safetensors"

_CHUNK = 1 << 20


def _discard(path: str) -> None:
    with contextlib.suppress(OSError):
        os.remove(path)


def _expected_size(record: dict) -> int | None:
    """``file_size`` as an int, or ``None`` if the shelf recorded nothing usable.

    Deliberately not ``isinstance(x, int)``: JSON decodes ``1.2e9`` to a float
    and some clients send the field as a string, and a type check that quietly
    fails open on either turns the one guard in ``_local_path`` off without
    saying so.  ``bool`` is excluded because ``True`` is an ``int`` in Python
    and a size of 1 is not what a ``true`` on the wire meant.
    """
    value = record.get("file_size")
    if isinstance(value, bool) or value is None:
        return None
    try:
        size = int(value)
    except (TypeError, ValueError):
        return None
    # Zero is refused, not accepted: the guard's whole argument is that two
    # different files don't collide on exact byte length by accident, and at
    # length 0 that is simply untrue — an interrupted download, a touched
    # placeholder and a failed copy are all 0 bytes and all match.
    return size if size > 0 else None


def local_path(record: dict, *, label: str = "PixlStash") -> str | None:
    """Absolute path of a usable local copy of ``record``, or ``None``.

    ``folder_path`` / ``relpath`` describe the machine **PixlStash** runs on.
    When ComfyUI runs on that same machine (or on one sharing the filesystem)
    they are usable directly, which is the common case and saves copying
    gigabytes for nothing.  When it does not, the same path may well exist here
    holding a *different* file, and loading that silently would be the worst
    failure this node could have.

    ``file_size`` is what rules that out without re-hashing gigabytes on every
    queue: a path collision that also collides on exact byte length is not a
    case that happens by accident.  **A record with no usable size is therefore
    refused, not trusted** — an unverifiable path is exactly the case this
    guard exists for, so failing open there would be a guard in name only.
    Either way the caller falls through to ``_cached_download``, which verifies
    the digest, so a refusal costs a copy and never correctness.

    A location is used only if the shelf last saw it ``present``, the joined
    path stays inside the folder that was registered, it is a ``.safetensors``
    file, it is on disk, and its size matches.

    The containment check is *not* a boundary against a hostile server —
    ``folder_path`` and ``relpath`` arrive on the same wire, so a server that
    wanted to name someone's private key would simply register its directory as
    the folder.  What it catches is the ordinary bug where a ``relpath`` escapes
    its own folder; what limits the rest is the extension and size checks.
    """
    expected_size = _expected_size(record)

    locations = record.get("locations")
    if not isinstance(locations, list):
        if locations:
            log.warning(
                "[PixlStash] Shelf record has a %s where its locations list "
                "should be — treating it as having no local copy.",
                type(locations).__name__,
            )
        return None

    for loc in locations:
        if not isinstance(loc, dict):
            continue
        state = loc.get("state")
        if state != "present":
            # Logged, because the alternative is a user staring at "no usable
            # copy on this machine" while looking straight at the file: a
            # stale scan or a flapping mount on the PixlStash host lands here,
            # and nothing else would say so.
            log.info(
                "[PixlStash] Skipping a copy the shelf last saw as %r (%s).",
                state,
                loc.get("relpath"),
            )
            continue
        folder = str(loc.get("folder_path") or "")
        relpath = str(loc.get("relpath") or "")
        if not folder or not relpath:
            continue

        # normpath, not realpath: this collapses the ``..`` that the check
        # exists to catch, without resolving symlinks. Symlinking a big model
        # into a models directory is ordinary ComfyUI practice, and realpath
        # would refuse every one of them as if it were a traversal attempt.
        root = os.path.normpath(folder)
        path = os.path.normpath(os.path.join(folder, relpath))
        if not path.startswith(root + os.sep):
            log.error(
                "[PixlStash] Refusing a shelf location that escapes its folder: "
                "%r is not inside %r",
                relpath,
                folder,
            )
            continue
        if not path.lower().endswith(_SUFFIX):
            log.warning("[PixlStash] Ignoring non-safetensors copy: %s", path)
            continue

        if expected_size is None:
            # Redundant for control flow — the comparison below would `continue`
            # on `!= None` anyway — and kept for the message, because "we could
            # not check" and "it did not match" send the user to different
            # places.
            log.warning(
                "[PixlStash] The shelf records no file size for this file, so "
                "the copy at %s cannot be told apart from an unrelated file of "
                "the same name — %s will not use it.",
                path,
                label,
            )
            continue

        # isfile before getsize: getsize succeeds on a directory, so on its own
        # it would let one through as a "file of the right size".
        if not os.path.isfile(path):
            continue
        try:
            actual_size = os.path.getsize(path)
        except OSError:
            # Vanished or unmounted between the two calls.
            continue
        if actual_size != expected_size:
            log.warning(
                "[PixlStash] %s is not the file the shelf describes "
                "(%d bytes here, %d on the shelf) — ignoring it and fetching "
                "the file instead. This usually means ComfyUI and PixlStash "
                "are on different machines.",
                path,
                actual_size,
                expected_size,
            )
            continue
        return path
    return None


def _cache_dir(folder_key: str, label: str) -> str:
    import folder_paths  # noqa: PLC0415 — only available inside ComfyUI

    roots = folder_paths.get_folder_paths(folder_key)
    if not roots:
        raise RuntimeError(
            f"{label}: ComfyUI has no {folder_key!r} model directory "
            "configured, so there is nowhere to cache a downloaded file."
        )
    # Under the real loras root rather than somewhere of our own, so nothing
    # has to teach ComfyUI about a second models directory.
    #
    # ponytail: the cost of that is real — these files are named by their hash,
    # they do appear in ComfyUI's stock LoRA dropdown as 64 hex characters, and
    # nothing evicts them. A models/pixlstash_loras root registered separately
    # would keep the dropdown clean, at the price of a config change the user
    # has to make. Revisit if anyone complains about either.
    return os.path.join(roots[0], "pixlstash")


def cached_download(client, sha256: str, *, folder_key: str, label: str) -> str:
    """Return a locally cached copy of the adapter, downloading it if needed.

    Named by content hash, so existence *is* the validity check on later runs
    and two shelf rows of the same weights dedupe — which is only true because
    nothing is ever put under that name until its digest has been checked.
    """
    cache_dir = _cache_dir(folder_key, label)
    os.makedirs(cache_dir, exist_ok=True)
    # ponytail: the cache never evicts. Add an LRU sweep if anyone fills a disk.
    final = os.path.join(cache_dir, f"{sha256}{_SUFFIX}")
    if os.path.isfile(final):
        return final

    # Written under .part and renamed only on a verified complete body: a
    # killed ComfyUI, a dropped connection or a truncated response must not
    # leave a file that the existence check above then trusts forever.
    # ponytail: no lock — ComfyUI runs one prompt at a time. Two processes
    # sharing a models dir would race on the .part; add a lockfile if that ever
    # becomes real.
    part = f"{final}.part"
    log.info("[PixlStash] Downloading %s → %s", sha256[:12], final)

    digest = hashlib.sha256()
    written = 0
    try:
        # The client's 30s timeout is per socket read, not for the whole
        # transfer, so a multi-gigabyte body is fine. Don't "fix" it.
        with contextlib.closing(
            client.get(f"/api/v1/adapters/{sha256}/file", stream=True)
        ) as resp:
            with open(part, "wb") as fh:
                for chunk in resp.iter_content(chunk_size=_CHUNK):
                    if not chunk:
                        continue
                    fh.write(chunk)
                    digest.update(chunk)
                    written += len(chunk)
    except BaseException as exc:
        # One cleanup path for every way this can fail: the client's own
        # RuntimeError before a byte is written, and — the case a bare
        # `except RuntimeError` would miss — a dropped connection or a full
        # disk coming out of iter_content / write as an OSError, with a
        # half-written .part on disk.
        _discard(part)
        if not isinstance(exc, RuntimeError):
            raise
        # Only a 404 is evidence about the route; a 401, an SSL failure or a
        # timeout says nothing about it, and telling the user "your server is
        # too old" when their token expired sends them looking in the wrong
        # place. The hash is known to be a real adapter by now — the record
        # fetch above already 404'd otherwise — and a server that has the route
        # but no readable copy answers 409, whose own detail text comes through
        # verbatim. So a 404 *here* means the route isn't there at all.
        hint = (
            " That route serves model bytes and first ships in PixlStash "
            "1.10; this server is older than that."
            if "not found" in str(exc).lower()
            else ""
        )
        raise RuntimeError(
            f"{label}: no usable copy of this file is on this machine and "
            f"fetching one failed — {exc}.{hint}"
        ) from exc

    actual = digest.hexdigest()
    if actual != sha256:
        _discard(part)
        raise RuntimeError(
            f"{label}: the server served {written} bytes whose SHA-256 is "
            f"{actual[:12]}…, not the {sha256[:12]}… that was requested. "
            "Nothing was cached."
        )

    os.replace(part, final)
    log.info("[PixlStash] %s cached (%d bytes).", sha256[:12], written)
    return final


def client_for(label: str):
    """An authenticated client from the ComfyUI settings, or a clear error."""
    url, token, verify_ssl = read_credentials()
    if not url or not token:
        raise RuntimeError(
            f"{label}: URL and API Token are required. "
            "Configure them in ComfyUI Settings › PixlStash."
        )
    return make_client(url, token, verify_ssl)


def fetch_record(client, sha256: str, *, label: str) -> dict:
    """The shelf's record for one hash-addressed file."""
    try:
        record = client.get(f"/api/v1/adapters/{sha256}").json()
    except RuntimeError as exc:
        # The shelf routes are OWNER_ONLY and library-pinned, unlike the
        # picture routes, so a token that works everywhere else in this
        # package is refused here. The client only has the generic 403
        # message to go on, hence the substring test.
        if "does not have access" in str(exc):
            raise RuntimeError(
                f"{label}: the model shelf is owner-only and pinned to a "
                "library. A resource-scoped share token is refused here even "
                "though it works for pictures — use an owner token in ComfyUI "
                "Settings › PixlStash."
            ) from exc
        raise
    if not isinstance(record, dict):
        raise RuntimeError(
            f"{label}: the server did not return a record for "
            f"{sha256[:12]}… (got {type(record).__name__})."
        )
    return record


def resolve(sha256: str, *, label: str, folder_key: str, download: bool = True):
    """``(record, path)`` for a hash-addressed shelf file.

    The one entry point the nodes call: everything above is reachable on its
    own for the checkpoint loader, which has no hash to address by.
    """
    sha256 = (sha256 or "").strip().lower()
    if not sha256:
        raise RuntimeError(
            f"{label}: nothing selected. Click the Browse button on the node "
            "and pick a file."
        )
    if not _SHA256_RE.match(sha256):
        raise RuntimeError(
            f"{label}: the selected hash must be a 64-character lowercase hex "
            f"digest (got {sha256!r})."
        )

    client = client_for(label)
    record = fetch_record(client, sha256, label=label)
    path = local_path(record, label=label)
    if path is None:
        if not download:
            raise RuntimeError(
                f"{label}: no usable copy of this file is on this machine, and "
                "PixlStash does not serve checkpoint bytes — the shelf's paths "
                "are its own host's. Put ComfyUI on the same filesystem, or "
                "copy the file into a ComfyUI models folder and rescan."
            )
        path = cached_download(client, sha256, folder_key=folder_key, label=label)
    log.info("[PixlStash] %s resolved to %s", sha256[:12], path)
    return record, path

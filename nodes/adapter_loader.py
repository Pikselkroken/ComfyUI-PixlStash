"""PixlStash Adapter Loader node.

Applies one adapter (LoRA / LoKr / …) from the PixlStash model shelf to a
MODEL, resolving or fetching the file first.

Shaped like ComfyUI's built-in ``LoraLoader`` on purpose. An earlier version
emitted the file *path* and left the applying to another node; that could not
be wired to anything, because every LoRA loader in the ecosystem takes a name
off a combo widget and a combo input will not accept a STRING link. A loader
that cannot be connected to a loader has to be one.

The node itself filters nothing: the ``adapter_kind`` and ``base_model``
widgets exist only so the JS Browse modal (``web/js/adapter_picker.js``) can
narrow the grid, and the ``pixlstash_set`` / ``pixlstash_character`` wires are
read there too.  **Character wins when both are wired** — ``GET /adapters``
rejects being given ``character_id`` and ``set_id`` together — and that rule
lives in the picker, next to the request that would 400.  All Python gets is
one ``adapter_sha256``.

Resolution order:

1. A copy the shelf records as ``present`` whose path also resolves, *here*, to
   a file of the size the shelf recorded.  See ``_local_path`` for why the size
   is not optional: the shelf's paths are the **PixlStash host's** paths, and
   ComfyUI is not always on that host.
2. Otherwise a download into ``<loras>/pixlstash/<sha256>.safetensors``, whose
   digest is verified before anything is cached.

No ``IS_CHANGED``: the node is cached on its inputs like every other loader in
this package.  A NaN would re-resolve on every queue, but at the price of
invalidating every node downstream of it — a KSampler re-running on each press
of Queue is a far worse bug than the one it would fix (a file moved on disk
mid-session, which a restart or any graph edit already clears).
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import logging
import os
import re

from ..connection import make_client, read_credentials
from .adapter_applier import AdapterApplier

log = logging.getLogger(__name__)

# The algorithms ``pixlstash/utils/adapter_header.py`` can detect from a file's
# tensor markers.  There is no ``lycoris`` literal — LyCORIS formats are
# recorded as ``lokr`` / ``loha``.
ANY_KIND = "— Any —"
ADAPTER_KINDS = [ANY_KIND, "lora", "lokr", "loha", "oft", "dora", "unknown"]

_SHA256_RE = re.compile(r"[0-9a-f]{64}\Z")

# The shelf is a catalogue of safetensors and nothing else: PixlStash's folder
# scanner skips every other extension outright (``MODEL_SUFFIX`` in
# ``pixlstash/services/model_folder_scanner.py``), and its adapter kinds are
# read out of a safetensors header.  So this is defence in depth against a
# server sending something unexpected, not a filter that hides a real class of
# adapter — and it is why the download cache can name every file it writes
# ``.safetensors`` without inspecting the body.  If the shelf ever learns to
# catalogue ``.pt`` / ``.ckpt``, both uses have to change together.
_ADAPTER_SUFFIX = ".safetensors"

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


def _local_path(record: dict) -> str | None:
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
                "[PixlStash] Adapter record has a %s where its locations list "
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
                "[PixlStash] Refusing adapter location that escapes its folder: "
                "%r is not inside %r",
                relpath,
                folder,
            )
            continue
        if not path.lower().endswith(_ADAPTER_SUFFIX):
            log.warning("[PixlStash] Ignoring non-safetensors adapter copy: %s", path)
            continue

        if expected_size is None:
            # Redundant for control flow — the comparison below would `continue`
            # on `!= None` anyway — and kept for the message, because "we could
            # not check" and "it did not match" send the user to different
            # places.
            log.warning(
                "[PixlStash] The shelf records no file size for this adapter, "
                "so the copy at %s cannot be told apart from an unrelated file "
                "of the same name — fetching a verified copy instead.",
                path,
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
                "[PixlStash] %s is not the adapter the shelf describes "
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


def _cache_dir() -> str:
    import folder_paths  # noqa: PLC0415 — only available inside ComfyUI

    roots = folder_paths.get_folder_paths("loras")
    if not roots:
        raise RuntimeError(
            "PixlStash Adapter Loader: ComfyUI has no 'loras' model directory "
            "configured, so there is nowhere to cache a downloaded adapter."
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


def _cached_download(client, sha256: str) -> str:
    """Return a locally cached copy of the adapter, downloading it if needed.

    Named by content hash, so existence *is* the validity check on later runs
    and two shelf rows of the same weights dedupe — which is only true because
    nothing is ever put under that name until its digest has been checked.
    """
    cache_dir = _cache_dir()
    os.makedirs(cache_dir, exist_ok=True)
    # ponytail: the cache never evicts. Add an LRU sweep if anyone fills a disk.
    final = os.path.join(cache_dir, f"{sha256}{_ADAPTER_SUFFIX}")
    if os.path.isfile(final):
        return final

    # Written under .part and renamed only on a verified complete body: a
    # killed ComfyUI, a dropped connection or a truncated response must not
    # leave a file that the existence check above then trusts forever.
    # ponytail: no lock — ComfyUI runs one prompt at a time. Two processes
    # sharing a models dir would race on the .part; add a lockfile if that ever
    # becomes real.
    part = f"{final}.part"
    log.info("[PixlStash] Downloading adapter %s → %s", sha256[:12], final)

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
            " That route serves adapter bytes and first ships in PixlStash "
            "1.10; this server is older than that."
            if "not found" in str(exc).lower()
            else ""
        )
        raise RuntimeError(
            "PixlStash Adapter Loader: no usable copy of this adapter is on "
            f"this machine and fetching one failed — {exc}.{hint}"
        ) from exc

    actual = digest.hexdigest()
    if actual != sha256:
        _discard(part)
        raise RuntimeError(
            "PixlStash Adapter Loader: the server served "
            f"{written} bytes whose SHA-256 is {actual[:12]}…, not the "
            f"{sha256[:12]}… that was requested. Nothing was cached."
        )

    os.replace(part, final)
    log.info("[PixlStash] Adapter %s cached (%d bytes).", sha256[:12], written)
    return final


def _trigger_words(record: dict) -> str:
    """``trigger_words`` as text you can paste into a prompt.

    The field is declared ``Optional[str]`` on the server but what it actually
    holds is a JSON array — every adapter on the shelf this was checked against
    stores ``'["Clementine"]'`` rather than ``Clementine``. Returning that
    verbatim puts brackets and quotes into the prompt, so a string that parses
    as a JSON list is unwrapped and joined.

    Anything else is passed through: a plain ``knight, plate armour`` is already
    what we want, and a bare word is not JSON at all. Only a *list* is unwrapped
    — ``json.loads`` would also happily turn the trigger word ``"1girl"`` into
    the number 1.
    """
    value = record.get("trigger_words")
    if isinstance(value, str):
        text = value.strip()
        if text.startswith("["):
            try:
                decoded = json.loads(text)
            except ValueError:
                decoded = None
            if isinstance(decoded, list):
                value = decoded
    if isinstance(value, (list, tuple)):
        return ", ".join(str(v).strip() for v in value if str(v).strip())
    return str(value).strip() if value else ""


class PixlStashAdapterLoader:
    """A LoRA loader whose file comes off the PixlStash shelf.

    Same shape as ComfyUI's built-in ``LoraLoader`` — MODEL and CLIP in, MODEL
    and CLIP out, two strengths — with the ``lora_name`` dropdown replaced by
    the Browse grid, and the file resolved (or fetched) from the shelf instead
    of read out of a local folder.
    """

    CATEGORY = "PixlStash"
    # Deliberately the built-in LoraLoader's signature: MODEL + CLIP in, MODEL
    # + CLIP out. Emitting a path instead was the original design and it did
    # not work — a path connects to nothing, because every LoRA loader in the
    # ecosystem takes a NAME off a combo widget, and a combo input will not
    # accept a STRING link. The only way to be usable is to be the loader.
    RETURN_TYPES = ("MODEL", "CLIP", "STRING")
    RETURN_NAMES = ("model", "clip", "trigger_words")
    FUNCTION = "load_lora"

    DESCRIPTION = (
        "Applies a LoRA from your PixlStash model shelf to a model, chosen by "
        "browsing a grid of them instead of hunting for a filename in a "
        "dropdown of hundreds.\n\n"
        "Drop-in for the built-in LoRA loader: same model / clip inputs, same "
        "two strengths, same two outputs, chain as many as you like. Click "
        "“Browse adapters…” to pick one. Wire a Character Loader or Set Loader "
        "in to see only that person's or that set's adapters.\n\n"
        "The file is used where it lies when PixlStash is on this machine, and "
        "otherwise fetched once and cached under your loras folder, verified "
        "against its SHA-256 before anything is written. LoKr, LoHa, OFT and "
        "DoRA work here too."
    )
    OUTPUT_TOOLTIPS = (
        "The model with the adapter applied. Chain into another of these for a "
        "second adapter.",
        "The CLIP with the adapter applied — carries nothing if you left the "
        "clip input unwired, so leave this unwired too in that case.",
        "The trigger words recorded on the shelf, comma-separated. Wire into a "
        "text encode. Empty when the shelf has none for this adapter.",
    )

    def __init__(self) -> None:
        self._applier = AdapterApplier()

    @classmethod
    def INPUT_TYPES(cls):
        strength = {
            "default": 1.0,
            "min": -100.0,
            "max": 100.0,
            "step": 0.01,
        }
        return {
            "required": {
                "model": (
                    "MODEL",
                    {"tooltip": "The diffusion model the adapter is applied to."},
                ),
                "adapter_kind": (
                    ADAPTER_KINDS,
                    {
                        "default": ANY_KIND,
                        "tooltip": (
                            "Narrows the Browse grid to one adapter algorithm. "
                            "Affects the grid only, not what is loaded."
                        ),
                    },
                ),
                "base_model": (
                    ["(loading…)"],
                    {
                        "tooltip": (
                            "Narrows the Browse grid to adapters trained against "
                            "one base model. Populated live from your shelf; "
                            "affects the grid only, not what is loaded."
                        ),
                    },
                ),
                "adapter_sha256": (
                    "STRING",
                    {
                        "default": "",
                        "multiline": False,
                        "tooltip": (
                            "SHA-256 of the chosen adapter. Written by the "
                            "Browse button — you don't normally type here."
                        ),
                    },
                ),
            },
            "optional": {
                "clip": (
                    "CLIP",
                    {
                        "tooltip": (
                            "Optional. Leave unwired for a model-only adapter — "
                            "but then leave the CLIP output unwired too, as it "
                            "has nothing to carry."
                        )
                    },
                ),
                "strength_model": (
                    "FLOAT",
                    {
                        **strength,
                        "tooltip": (
                            "How strongly to patch the model. Can be negative."
                        ),
                    },
                ),
                "strength_clip": (
                    "FLOAT",
                    {
                        **strength,
                        "tooltip": (
                            "How strongly to patch CLIP. Ignored with no clip "
                            "wired. Can be negative."
                        ),
                    },
                ),
                "pixlstash_set": (
                    "PIXLSTASH_SET",
                    {
                        "forceInput": True,
                        "tooltip": "Wire from a Set Loader to list only that set's adapters.",
                    },
                ),
                "pixlstash_character": (
                    "PIXLSTASH_CHARACTER",
                    {
                        "forceInput": True,
                        "tooltip": (
                            "Wire from a Character Loader to list only that "
                            "character's adapters. Takes precedence over a set."
                        ),
                    },
                ),
            },
        }

    def load_lora(
        self,
        model,
        adapter_kind: str,
        base_model: str,
        adapter_sha256: str,
        clip=None,
        strength_model: float = 1.0,
        strength_clip: float = 1.0,
        pixlstash_set: str = "",
        pixlstash_character: str = "",
    ):
        # adapter_kind / base_model / the two wires are read by the Browse
        # modal, not here — see the module docstring.
        record, path = self._resolve(adapter_sha256)
        triggers = _trigger_words(record)

        # AFTER the resolve, not before. The built-in returns early on a pair of
        # zero strengths and never touches the disk; here the shelf lookup is
        # also what produces trigger_words, and short-circuiting first would
        # emit an empty string for them whenever someone parked a strength at 0.
        model, clip = self._applier.apply(
            model, clip, path, strength_model, strength_clip
        )
        return (model, clip, triggers)

    def _resolve(self, adapter_sha256: str):
        """The shelf record and a local file path for it.

        Split out of ``load_lora`` because it is the half with all the failure
        modes — a bad digest, a token without shelf scope, a copy that is on the
        shelf but not on this disk — and none of them need a MODEL to exercise.
        """
        sha256 = (adapter_sha256 or "").strip().lower()
        if not sha256:
            raise RuntimeError(
                "PixlStash Adapter Loader: no adapter selected. "
                "Click “Browse adapters…” on the node and pick one."
            )
        if not _SHA256_RE.match(sha256):
            raise RuntimeError(
                "PixlStash Adapter Loader: adapter_sha256 must be a 64-character "
                f"lowercase hex digest (got {adapter_sha256!r})."
            )

        url, token, verify_ssl = read_credentials()
        if not url or not token:
            raise RuntimeError(
                "PixlStash Adapter Loader: URL and API Token are required. "
                "Configure them in ComfyUI Settings › PixlStash."
            )
        client = make_client(url, token, verify_ssl)

        record = self._fetch_record(client, sha256)
        path = _local_path(record) or _cached_download(client, sha256)
        log.info("[PixlStash] Adapter %s resolved to %s", sha256[:12], path)
        return record, path

    @staticmethod
    def _fetch_record(client, sha256: str) -> dict:
        try:
            record = client.get(f"/api/v1/adapters/{sha256}").json()
        except RuntimeError as exc:
            # The shelf routes are OWNER_ONLY and library-pinned, unlike the
            # picture routes, so a token that works everywhere else in this
            # package is refused here. The client only has the generic 403
            # message to go on, hence the substring test.
            if "does not have access" in str(exc):
                raise RuntimeError(
                    "PixlStash Adapter Loader: the model shelf is owner-only "
                    "and pinned to a library. A resource-scoped share token is "
                    "refused here even though it works for pictures — use an "
                    "owner token in ComfyUI Settings › PixlStash."
                ) from exc
            raise
        if not isinstance(record, dict):
            raise RuntimeError(
                "PixlStash Adapter Loader: the server did not return an adapter "
                f"record for {sha256[:12]}… (got {type(record).__name__})."
            )
        return record

    @classmethod
    def VALIDATE_INPUTS(cls, base_model):
        # base_model's real option list is injected client-side, so accept any
        # runtime value rather than validating against the placeholder.
        return True

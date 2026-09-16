"""What a run was locked to, reported for PixlStash to record.

A workflow says what to *look for*; a run resolves that to what was actually
*used*.  The difference is real and it is nowhere in the saved JSON: a Picture
Loader with an empty ``picture_ids`` picks its batch off a live query, a search
node picks its batch off a score, and a shelf hash or checkpoint id becomes one
particular file on one particular disk.  Re-running the same workflow tomorrow
can legitimately resolve to something else, so "which pictures and which models
went into this render" is only knowable at the moment it ran.

Every node that *resolves* something therefore attaches it to its own return
value as ComfyUI UI output.  Resolving is the operative word: a gate that drops
frames from a batch has filtered, not resolved, and the frames it drops are
generations with no vault identity to report — so the gates carry no lock and
the loaders' locks describe the inputs, which is what they are for.

ComfyUI puts the payload in the ``executed`` websocket message and keeps it in
``GET /history/{prompt_id}`` under the node's id, so PixlStash reads a finished
run's locks without this package having to call anything.  On current ComfyUI a
node whose outputs came from cache has its UI payload replayed rather than
dropped, so a second queue of an unchanged graph still reports — that is
ComfyUI's behaviour and not a promise this module can keep on its own.

The payload, under the ``pixlstash_lock`` key, is a one-element list (the shape
every ComfyUI UI value has) holding::

    {"pictures": [12, 15],
     "models": [{"kind": "adapter", "sha256": "…", "id": None}]}

``kind`` is ``adapter`` / ``checkpoint`` / ``vae`` / ``clip``.  Both identifiers
are always present and either may be null: adapters, VAEs and text encoders are
hash-addressed and have no row id here, and a checkpoint is addressed by id
precisely because its ``sha256`` stays null until the shelf's background hasher
reaches the file.
"""

from __future__ import annotations

import re

UI_KEY = "pixlstash_lock"

_SHA256_RE = re.compile(r"[0-9a-f]{64}\Z")


def shelf_model(kind: str, *, sha256=None, row_id=None) -> dict:
    """One ``models`` entry: both ways the shelf addresses a file, either null.

    The digest is normalised **here** and not at the four call sites, which is
    the whole point of the helper.  Three loaders agreeing on
    ``str(...).strip().lower()`` while a fourth passes the server's field
    through raw is not a contract — PixlStash matches these against its own
    rows, which are lowercase hex, and an uppercase or whitespace-padded digest
    silently matches nothing.

    Anything that is not a 64-character hex digest becomes null rather than
    being reported as an identifier: an unhashed checkpoint legitimately has
    none, and a server field of some other shape is not one either.  Null says
    "address this by id"; a junk string says "address it by this", which is
    worse than saying nothing.

    ``row_id`` rather than ``id`` because the key on the wire is ``id`` and the
    keyword is not — shadowing the builtin in every loader's call to save four
    characters is a poor trade.
    """
    digest = str(sha256 or "").strip().lower()
    return {
        "kind": kind,
        "sha256": digest if _SHA256_RE.match(digest) else None,
        "id": row_id,
    }


def report(result, *, pictures=(), models=()) -> dict:
    """``result``, plus the lock, in the dict ComfyUI reads UI output from.

    Returned in place of the bare tuple.  ComfyUI accepts either from any node,
    output node or not, so this changes nothing about how the node is wired.
    """
    return {
        "ui": {UI_KEY: [{"pictures": list(pictures), "models": list(models)}]},
        "result": tuple(result),
    }

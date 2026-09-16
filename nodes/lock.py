"""What a run was locked to, reported for PixlStash to record.

A workflow says what to *look for*; a run resolves that to what was actually
*used*.  The difference is real and it is nowhere in the saved JSON: a Picture
Loader with an empty ``picture_ids`` picks its batch off a live query, a search
node picks its batch off a score, and a shelf hash or checkpoint id becomes one
particular file on one particular disk.  Re-running the same workflow tomorrow
can legitimately resolve to something else, so "which pictures and which models
went into this render" is only knowable at the moment it ran.

Every node that resolves something therefore attaches it to its own return
value as ComfyUI UI output.  ComfyUI puts that in the ``executed`` websocket
message and keeps it in ``GET /history/{prompt_id}`` under the node's id, so
PixlStash reads a finished run's locks without this package having to call
anything — and gets them for a node whose outputs were cached too, because
ComfyUI replays the cached UI payload rather than dropping it.

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

UI_KEY = "pixlstash_lock"


def shelf_model(kind: str, *, sha256=None, row_id=None) -> dict:
    """One ``models`` entry: both ways the shelf addresses a file, either null.

    ``row_id`` rather than ``id`` because the key on the wire is ``id`` and the
    keyword is not — shadowing the builtin in every loader's call to save four
    characters is a poor trade.
    """
    return {"kind": kind, "sha256": (sha256 or None), "id": row_id}


def report(result, *, pictures=(), models=()) -> dict:
    """``result``, plus the lock, in the dict ComfyUI reads UI output from.

    Returned in place of the bare tuple.  ComfyUI accepts either from any node,
    output node or not, so this changes nothing about how the node is wired.
    """
    return {
        "ui": {UI_KEY: [{"pictures": list(pictures), "models": list(models)}]},
        "result": tuple(result),
    }

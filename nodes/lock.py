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

The payload, under the ``pixlstash_lock`` key, is a list (the shape every
ComfyUI UI value has) whose entries look like::

    {"pictures": [12, 15],
     "models": [{"kind": "adapter", "sha256": "…", "id": None,
                 "filename": "…", "display_name": None}]}

**One entry per execution of the node, and usually that means one.**  It is not
reliably one: wire something with ``OUTPUT_IS_LIST`` into a loader's widget and
ComfyUI's ``_map_node_over_list`` runs the node once per element, merging the UI
dicts with ``ui.setdefault(k, []).extend(v)`` — so the key comes back holding N
entries and this module cannot fold them, because each call only ever sees its
own.  A consumer therefore folds every entry rather than indexing ``[0]``:
taking the first would silently record one resolution out of N.

``kind`` is ``adapter`` / ``checkpoint`` / ``vae`` / ``clip``.  Both identifiers
are always present and either may be null: adapters, VAEs and text encoders are
hash-addressed and have no row id here, and a checkpoint is addressed by id
precisely because its ``sha256`` stays null until the shelf's background hasher
reaches the file.

``filename`` and ``display_name`` are the shelf's readable names for the file,
either null.  They are not identifiers and nothing resolves by them: they are
there so a run still says which model it loaded after the shelf row behind the
id or digest has been merged, forgotten or deleted.  The browser writes the
same names into the saved workflow (``pixlstash_models`` in the node's
``properties``); this is the copy a run submitted as a bare API ``prompt``,
which has no editor graph, still carries.
"""

from __future__ import annotations

import re

UI_KEY = "pixlstash_lock"

# What a shelf digest looks like. Lives here rather than in ``shelf_file``,
# which imports it: this module is the leaf, and the shape of an identifier
# belongs beside the thing that puts identifiers on the wire.
SHA256_RE = re.compile(r"[0-9a-f]{64}\Z")

# A row id as it is actually written: no sign, no padding, no separators. The
# same rule (and the same reasoning) as ``proxy_routes._ID_RE`` — which cannot
# be imported here, since it would pull aiohttp into every node import.
_ID_RE = re.compile(r"[1-9][0-9]*\Z")


def _row_id(value):
    """``value`` as a positive row id, or ``None`` if it is not written as one.

    The digest's argument applies verbatim to the id, and this is where it was
    missing. ``checkpoint_loader._fetch_record`` matches with
    ``str(row.get("id")) == wanted``, so a server serialising ids as strings is
    already supported — and passing the field through raw would then put
    ``{"id": "7"}`` on the wire where another server puts ``{"id": 7}``, which
    PixlStash matching against an integer primary key matches in one case only.
    """
    if isinstance(value, bool) or value is None:
        return None
    return int(value) if _ID_RE.match(str(value).strip()) else None


def _name(value):
    """A readable name off the shelf record, or ``None`` for anything else."""
    return (value.strip() or None) if isinstance(value, str) else None


def shelf_model(kind: str, *, sha256=None, row_id=None, record=None) -> dict:
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

    ``row_id`` gets the same treatment, for the same reason — see ``_row_id``.

    ``record`` is the shelf record the file was resolved from, read for its
    ``filename`` and ``display_name`` only.
    It is named that rather than ``id`` because the key on the wire is ``id``
    and the keyword is not: shadowing the builtin in every loader's call to save
    four characters is a poor trade.
    """
    digest = str(sha256 or "").strip().lower()
    record = record if isinstance(record, dict) else {}
    return {
        "kind": kind,
        "sha256": digest if SHA256_RE.match(digest) else None,
        "id": _row_id(row_id),
        "filename": _name(record.get("filename")),
        "display_name": _name(record.get("display_name")),
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

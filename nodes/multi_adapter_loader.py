"""PixlStash Multi Adapter Loader node.

The Adapter Loader for several adapters at once: one node, a list of rows,
each applied to the result of the one before, as a chain of Adapter Loaders
would be.  The same MODEL and CLIP in and MODEL, CLIP and ``trigger_words``
out, so it drops into the same place in a graph; it has none of the Adapter
Loader's set, character and workflow-set wires, which only narrow that node's
grid.

**The rows are flat, numbered inputs** — ``adapter_sha256`` / ``strength_model``
/ ``strength_clip`` for the first, then ``adapter_sha256_2`` /
``strength_model_2`` / ``strength_clip_2``, up to ``ROWS``.  That spelling is a
contract with PixlStash, which finds LoRA slots *by field name*: a list held
as a dict or a JSON string under one key would be invisible to it.

An empty digest is a row that loads nothing, wherever it sits.  The browser
keeps the rows packed, but a graph written by something else may hold a gap,
and a node with every row empty hands MODEL and CLIP back untouched — PixlStash
places it on the canvas with nobody picked, and a workflow queued like that has
to run as the workflow alone.

``show``, ``adapter_kind`` and ``base_model`` are read by the Browse grid
(``web/js/adapter_picker.js``) and by nothing here.
"""

from __future__ import annotations

import logging

from . import lock, shelf_file
from .adapter_applier import AdapterApplier
from .adapter_loader import ADAPTER_KINDS, ANY_KIND, _trigger_words

log = logging.getLogger(__name__)

LABEL = "PixlStash Multi Adapter Loader"

# Declared, not grown: ComfyUI wants every input in INPUT_TYPES, so the browser
# hides the unused rows rather than adding inputs.
ROWS = 8

SHOW_ALL = "All adapters"
SHOW_PEOPLE = "People who fit"

ROW_FIELDS = ("adapter_sha256", "strength_model", "strength_clip")


def row_field(field: str, row: int) -> str:
    """The input name of *field* on row *row* (1-based): bare, then ``_2`` on."""
    return field if row == 1 else f"{field}_{row}"


class PixlStashMultiAdapterLoader:
    """Several adapters off the PixlStash shelf, applied top to bottom."""

    CATEGORY = "PixlStash"
    RETURN_TYPES = ("MODEL", "CLIP", "STRING")
    RETURN_NAMES = ("model", "clip", "trigger_words")
    FUNCTION = "load_loras"

    DESCRIPTION = (
        "Applies several LoRAs from your PixlStash model shelf to a model, one "
        "row each, top to bottom — the same as chaining that many PixlStash "
        "Adapter (LoRA) Loaders, in one node.\n\n"
        "Click “Browse adapters…” and tick the ones you want; each becomes a "
        "row with its own two strengths and its trigger word. Set “show” to "
        "“People who fit” to pick by person instead: everyone with an adapter "
        "for the chosen base model.\n\n"
        "An empty row loads nothing, and with every row empty the model and "
        "clip pass straight through."
    )
    OUTPUT_TOOLTIPS = (
        "The model with every row's adapter applied, in row order.",
        "The CLIP with every row's adapter applied — carries nothing if you "
        "left the clip input unwired, so leave this unwired too in that case.",
        "Every picked adapter's trigger words, in row order, comma-separated. "
        "Wire into a text encode. Empty when no row has any.",
    )

    def __init__(self) -> None:
        # One applier per digest in use, so each adapter's file stays read
        # between runs. Keyed by digest rather than by row: removing a row
        # moves the ones below it up, and that must not re-read their files.
        self._appliers: dict[str, AdapterApplier] = {}

    @classmethod
    def INPUT_TYPES(cls):
        strength = {
            "default": 1.0,
            "min": -100.0,
            "max": 100.0,
            "step": 0.01,
        }
        rows = {}
        for row in range(1, ROWS + 1):
            rows[row_field("adapter_sha256", row)] = (
                "STRING",
                {
                    "default": "",
                    "multiline": False,
                    "tooltip": (
                        f"SHA-256 of the adapter on row {row}; empty loads "
                        "nothing. Written by the Browse button."
                    ),
                },
            )
            rows[row_field("strength_model", row)] = (
                "FLOAT",
                {
                    **strength,
                    "tooltip": f"How strongly row {row} patches the model.",
                },
            )
            rows[row_field("strength_clip", row)] = (
                "FLOAT",
                {
                    **strength,
                    "tooltip": (
                        f"How strongly row {row} patches CLIP. Ignored with no "
                        "clip wired."
                    ),
                },
            )
        return {
            "required": {
                "model": (
                    "MODEL",
                    {"tooltip": "The diffusion model the adapters are applied to."},
                ),
                "show": (
                    [SHOW_ALL, SHOW_PEOPLE],
                    {
                        "default": SHOW_ALL,
                        "tooltip": (
                            "What the Browse grid lists: every adapter, or the "
                            "people who have one for the chosen base model. "
                            "Affects the grid only, not what is loaded."
                        ),
                    },
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
            },
            # The rows are optional so that a prompt written without all of
            # them still validates: a row that is not there is an empty row.
            "optional": {
                "clip": (
                    "CLIP",
                    {
                        "tooltip": (
                            "Optional. Leave unwired for model-only adapters — "
                            "but then leave the CLIP output unwired too, as it "
                            "has nothing to carry."
                        )
                    },
                ),
                **rows,
            },
        }

    def load_loras(
        self, model, show: str, adapter_kind: str, base_model: str, clip=None, **rows
    ):
        picked = []
        for row in range(1, ROWS + 1):
            digest = str(rows.get(row_field("adapter_sha256", row)) or "")
            digest = digest.strip().lower()
            if digest:
                picked.append((row, digest))

        # Dropped before anything is read, so an adapter taken off the node is
        # freeable while its replacement loads.
        self._appliers = {
            digest: self._appliers[digest]
            for _, digest in picked
            if digest in self._appliers
        }

        words = []
        models = []
        for row, digest in picked:
            record, path = self._resolve(digest, row)
            triggers = _trigger_words(record)
            if triggers:
                words.append(triggers)
            # After the resolve, as on the Adapter Loader: a strength parked at
            # 0 still gives its trigger words — and is still reported to the
            # lock, as that loader reports its one.
            applier = self._appliers.setdefault(digest, AdapterApplier())
            model, clip = applier.apply(
                model,
                clip,
                path,
                rows.get(row_field("strength_model", row), 1.0),
                rows.get(row_field("strength_clip", row), 1.0),
            )
            models.append(
                lock.shelf_model(
                    "adapter", sha256=record.get("sha256") or digest, record=record
                )
            )
        return lock.report((model, clip, ", ".join(words)), models=models)

    @staticmethod
    def _resolve(adapter_sha256: str, row: int):
        label = f"{LABEL}, row {row}"
        try:
            return shelf_file.resolve(adapter_sha256, label=label, folder_key="loras")
        except RuntimeError as exc:
            # Not every failure carries the label: a digest the shelf does not
            # have is the client's bare 404. With one adapter on a node that
            # still says which; with eight rows it has to say the row.
            if label in str(exc):
                raise
            raise RuntimeError(f"{label}: {exc}") from exc

    @classmethod
    def VALIDATE_INPUTS(cls, base_model):
        # base_model's real option list is injected client-side, so accept any
        # runtime value rather than validating against the placeholder.
        return True

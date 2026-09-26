"""PixlStash Workflow Set Loader node.

A workflow set is one the owner made on PixlStash's model shelf (1.12): a
checkpoint plus the text encoders, VAEs and LoRAs they say go with it, held by
sha256.  This node loads the base of one — the checkpoint, its encoders and its
VAE — so a graph needs one node where it needed three.

What it does not do, and why:

* **LoRAs are not applied.** A set is a menu, not a recipe: it records no order
  and no strengths.  The ``pixlstash_workflow_set`` output goes to the Adapter
  Loader instead, whose Browse grid then shows only the set's LoRAs.
* **``other`` members are ignored.** The slot is for files the owner wanted
  kept with the set that are not part of a generation graph's base.
* **Text encoders are one combination, not alternatives.** Every encoder in
  the set goes into one CLIP, as clip-l + t5 does for Flux. A set holding two
  precisions of the same T5 builds the wrong CLIP, so keep alternatives in
  separate sets. More than ComfyUI's four is refused.
* **More than one VAE**: the first, in the order the server lists them (by
  name). A set may hold several as alternatives and nothing says which one is
  meant, so the choice is logged rather than guessed silently.

The CLIP type is a widget, as on the CLIP Loader: the shelf records a base
model as free text, and mapping that onto ``comfy.sd.CLIPType`` would be a
guess that goes wrong exactly when it matters.
"""

from __future__ import annotations

import logging
import re

from . import checkpoint_loader, clip_loader, lock, shelf_file, vae_loader

log = logging.getLogger(__name__)

LABEL = "PixlStash Workflow Set Loader"

# Hand-made workflow sets (``GET /models/workflow-sets`` → ``hand_made``).
MIN_SERVER_VERSION = "1.12.0"

# The most files ``comfy.sd.load_clip`` combines into one CLIP (HiDream's four).
MAX_ENCODERS = 4

_ID_RE = re.compile(r"#(\d+)\s*$")


def _extract_id(value: str) -> str:
    m = _ID_RE.search(value or "")
    return m.group(1) if m else ""


def fetch_set(set_id: str) -> dict:
    """The hand-made set with this id, or an error naming why there is none."""
    if not set_id:
        raise RuntimeError(f"{LABEL}: no workflow set selected. Pick one on the node.")
    client = shelf_file.client_for(LABEL, min_server_version=MIN_SERVER_VERSION)
    payload = client.get("/api/v1/models/workflow-sets").json()
    sets = payload.get("hand_made") if isinstance(payload, dict) else None
    if not isinstance(sets, list):
        raise RuntimeError(
            f"{LABEL}: the server did not return a workflow set list (got "
            f"{type(payload).__name__})."
        )
    for entry in sets:
        if isinstance(entry, dict) and str(entry.get("id")) == set_id:
            return entry
    raise RuntimeError(
        f"{LABEL}: workflow set #{set_id} does not exist any more. Pick another."
    )


def _members(entry: dict, slot: str) -> list[dict]:
    """The set's members in ``slot``; an error if any of them left the shelf.

    Refused rather than skipped: the owner said the set needs that file, and
    loading the rest without it builds a graph that is not the one they made.
    """
    members = [
        m
        for m in entry.get("members") or []
        if isinstance(m, dict) and m.get("slot") == slot
    ]
    for m in members:
        if not m.get("on_shelf"):
            raise RuntimeError(
                f"{LABEL}: “{m.get('name') or m.get('sha256')}” is in this set "
                "but no longer on the PixlStash shelf. Rescan the folder it "
                "was in, or take it out of the set."
            )
    return members


class PixlStashWorkflowSetLoader:
    """Loads a workflow set's checkpoint, text encoders and VAE in one node."""

    CATEGORY = "PixlStash"
    RETURN_TYPES = ("MODEL", "CLIP", "VAE", "PIXLSTASH_WORKFLOW_SET")
    # The last name must equal the widget's: combo_widgets.js reads a wired
    # value off the origin widget named like the output.
    RETURN_NAMES = ("model", "clip", "vae", "pixlstash_workflow_set")
    FUNCTION = "load_set"

    DESCRIPTION = (
        "Loads one of your PixlStash workflow sets — the checkpoint, text "
        "encoders and VAE you grouped together on the model shelf — in one "
        "node instead of three.\n\n"
        "The set's text encoders replace the checkpoint's own CLIP, and its "
        "VAE replaces the checkpoint's VAE; with none in the set, the "
        "checkpoint's own are used. Set `clip_type` to the model family, as "
        "on the CLIP Loader.\n\n"
        "Every text encoder in the set is loaded together, as one CLIP.\n\n"
        "LoRAs are not applied: wire the pixlstash_workflow_set output into a PixlStash "
        "Adapter (LoRA) Loader and its Browse grid shows only this set's LoRAs."
    )
    OUTPUT_TOOLTIPS = (
        "The diffusion model, for a KSampler.",
        "The set's text encoders, else the checkpoint's own CLIP. Empty when "
        "neither has one.",
        "The set's VAE, else the checkpoint's own. Empty when neither has one.",
        "Wire into a PixlStash Adapter (LoRA) Loader to browse only this set's LoRAs.",
    )

    @classmethod
    def INPUT_TYPES(cls):
        return {
            "required": {
                "pixlstash_workflow_set": (
                    ["(loading…)"],
                    {
                        "tooltip": (
                            "The workflow set to load. Populated live from "
                            "your PixlStash model shelf."
                        ),
                    },
                ),
                "clip_type": (
                    clip_loader._clip_types(),
                    {
                        "default": "stable_diffusion",
                        "tooltip": (
                            "The model family the set's text encoders are "
                            "loaded for. Unused when the set has none."
                        ),
                    },
                ),
            }
        }

    def load_set(self, pixlstash_workflow_set: str, clip_type: str):
        set_id = _extract_id(pixlstash_workflow_set)
        entry = fetch_set(set_id)
        models = []

        checkpoints = _members(entry, "checkpoint")
        if not checkpoints:
            raise RuntimeError(
                f"{LABEL}: this workflow set has no checkpoint. Add one to it "
                "on the PixlStash model shelf."
            )
        ckpt = checkpoints[0]
        # Read before the checkpoint loads, so it skips building a CLIP or VAE
        # the set is about to replace.
        encoders = _members(entry, "text_encoder")
        vaes = _members(entry, "vae")
        if ckpt.get("kind") not in ("checkpoint", "unknown"):
            # The server only lets these two into the slot, but a member keeps
            # its slot if its file is later re-kinded on the shelf.
            raise RuntimeError(
                f"{LABEL}: the set's checkpoint “{ckpt.get('name')}” is now "
                f"filed on the shelf as a {ckpt.get('kind')}, not a checkpoint. "
                "Fix it on the PixlStash model shelf."
            )
        if ckpt.get("kind") == "checkpoint":
            # Checkpoints are not served by hash, so only a local copy will do.
            record = checkpoint_loader.PixlStashCheckpointLoader._fetch_record(
                str(ckpt.get("id")), label=LABEL
            )
            path = checkpoint_loader.local_copy(record, label=LABEL)
        else:
            # An unclassified file in the checkpoint slot — the way a Flux or
            # Wan diffusion model usually lands — is hash-addressed like any
            # support file, so it can be fetched.
            record, path = shelf_file.resolve(
                ckpt["sha256"], label=LABEL, folder_key="diffusion_models"
            )
        model, clip, vae = checkpoint_loader.load_file(
            path, output_clip=not encoders, output_vae=not vaes
        )
        models.append(
            lock.shelf_model(
                "checkpoint",
                sha256=record.get("sha256") or ckpt["sha256"],
                row_id=record.get("id"),
            )
        )

        if len(encoders) > MAX_ENCODERS:
            raise RuntimeError(
                f"{LABEL}: this workflow set has {len(encoders)} text encoders, "
                f"and ComfyUI combines at most {MAX_ENCODERS}. Every encoder in a "
                "set is loaded together, so keep alternative versions of one "
                "encoder in separate sets."
            )
        if encoders:
            folder = clip_loader._encoder_folder()
            resolved = [
                shelf_file.resolve(m["sha256"], label=LABEL, folder_key=folder)
                for m in encoders
            ]
            clip = clip_loader.load_files([p for _r, p in resolved], clip_type)
            models += [
                lock.shelf_model("clip", sha256=r.get("sha256") or m["sha256"])
                for m, (r, _p) in zip(encoders, resolved)
            ]

        if vaes:
            if len(vaes) > 1:
                log.info(
                    "[PixlStash] Workflow set #%s has %d VAEs; loading the "
                    "first, “%s”.",
                    set_id,
                    len(vaes),
                    vaes[0].get("name"),
                )
            r, p = shelf_file.resolve(vaes[0]["sha256"], label=LABEL, folder_key="vae")
            vae = vae_loader.load_file(p)
            models.append(
                lock.shelf_model("vae", sha256=r.get("sha256") or vaes[0]["sha256"])
            )

        return lock.report((model, clip, vae, set_id), models=models)

    @classmethod
    def IS_CHANGED(cls, pixlstash_workflow_set="", clip_type=""):
        # A set id, unlike the other shelf loaders' hashes, can mean different
        # files tomorrow: members are added, removed and swapped on the shelf.
        # So the cache key is what the set holds now, not its name on the node.
        # On any failure load_set runs and reports the error itself.
        # ponytail: one whole workflow-sets read per queue (the server computes
        # its evidence counts too); a by-id route would make this cheap.
        try:
            entry = fetch_set(_extract_id(pixlstash_workflow_set))
        except Exception:  # noqa: BLE001
            return float("nan")
        return repr(
            sorted(
                (m.get("slot"), m.get("sha256"), m.get("kind"), m.get("on_shelf"))
                for m in entry.get("members") or []
                if isinstance(m, dict)
            )
        )

    @classmethod
    def VALIDATE_INPUTS(cls, pixlstash_workflow_set, clip_type):
        # The set list is filled client-side, so a saved set is never in the
        # placeholder list; clip_type may come from another ComfyUI's
        # CLIPType, and load_clip falls back to stable_diffusion for it.
        return True

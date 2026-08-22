"""PixlStash Checkpoint Loader node.

Shaped after ComfyUI's built-in ``CheckpointLoaderSimple`` — same three
outputs, in the same order, so it drops into a graph where one already sits —
and written against the same public API (``comfy.sd``, ``folder_paths``) that
every custom-node pack calls.  No ComfyUI source is copied here; see the
licence note in README.md.

The one shelf loader that cannot fetch its file.  ``GET /adapters/{sha256}``
and the ``/file`` route beside it **refuse checkpoints by design** ("that hash
is a checkpoint; see GET /checkpoints"), and no route serves checkpoint bytes —
so this node resolves a copy that is already on this machine, or says why it
cannot.  On a shared filesystem (the ordinary case: ComfyUI and PixlStash on
one box, or the same models drive mounted on both) that is every checkpoint;
on a split-host setup it is none of them, and the message says so.

Two more things follow from the server's shape:

* **Addressed by ``id``, not by hash.** ``sha256`` is null until the background
  hasher has read the file, and a 24 GB checkpoint is listable long before
  that. ``id`` is what the shelf's own UI holds on to, so it is what the picker
  writes into the widget here.
* **Resolved through the list route.** There is no ``GET /checkpoints/{id}``,
  so the record is found by listing and matching. ponytail: one unpaginated
  list per resolve, which ComfyUI caches on the node's inputs — revisit if the
  server ever grows a by-id route or a shelf gets big enough to notice.

A *bare diffusion model* (a Flux UNET in ``models/diffusion_models``) is filed
as a checkpoint by PixlStash's parameter-count rule, so it will show up in the
grid.  ``load_checkpoint_guess_config`` cannot build a CLIP or a VAE out of one
and raises; ``load_vae``/``load_diffusion_model`` is the fallback, and the node
then returns a MODEL with the other two outputs empty — which is what ComfyUI's
own checkpoint loader does for a checkpoint carrying no VAE.
"""

from __future__ import annotations

import logging

from . import shelf_file

log = logging.getLogger(__name__)

LABEL = "PixlStash Checkpoint Loader"


class PixlStashCheckpointLoader:
    """Loads a checkpoint off the PixlStash model shelf."""

    CATEGORY = "PixlStash"
    RETURN_TYPES = ("MODEL", "CLIP", "VAE")
    RETURN_NAMES = ("model", "clip", "vae")
    FUNCTION = "load_checkpoint"

    DESCRIPTION = (
        "Loads a checkpoint from your PixlStash model shelf, chosen by "
        "browsing a grid of them — with the names, base models and icons the "
        "shelf records — instead of a dropdown of filenames.\n\n"
        "Drop-in for the built-in Load Checkpoint: MODEL, CLIP and VAE out.\n\n"
        "Unlike the other PixlStash loaders this one cannot fetch the file: "
        "PixlStash does not serve checkpoint bytes. The copy has to be "
        "readable on this machine — the normal case when ComfyUI and PixlStash "
        "share a filesystem, and never the case when they do not.\n\n"
        "A bare diffusion model (a Flux UNET, say) lands on the shelf as a "
        "checkpoint too; it loads here as a MODEL with the CLIP and VAE "
        "outputs empty, so wire those from their own loaders."
    )
    OUTPUT_TOOLTIPS = (
        "The diffusion model, for a KSampler.",
        "The CLIP for encoding prompts. Empty for a checkpoint that carries "
        "no text encoder — wire a PixlStash CLIP Loader instead.",
        "The VAE for encoding and decoding images. Empty for a checkpoint that "
        "carries none — wire a PixlStash VAE Loader instead.",
    )

    @classmethod
    def INPUT_TYPES(cls):
        return {
            "required": {
                "checkpoint_id": (
                    "STRING",
                    {
                        "default": "",
                        "tooltip": (
                            "Written by the Browse button — the shelf id of the "
                            "checkpoint."
                        ),
                    },
                ),
            }
        }

    def load_checkpoint(self, checkpoint_id: str):
        import comfy.sd  # noqa: PLC0415 — only available inside ComfyUI
        import folder_paths  # noqa: PLC0415

        record = self._fetch_record(checkpoint_id)
        path = shelf_file.local_path(record, label=LABEL)
        if path is None:
            raise RuntimeError(
                f"{LABEL}: no usable copy of “"
                f"{record.get('display_name') or record.get('filename') or checkpoint_id}"
                "” is readable on this machine. PixlStash serves adapter bytes "
                "but not checkpoint bytes, so the file has to be on a "
                "filesystem ComfyUI can see — the shelf's paths are its own "
                "host's. Mount that drive here, or copy the file into a "
                "ComfyUI models folder and rescan it into PixlStash."
            )

        embeddings = folder_paths.get_folder_paths("embeddings")
        try:
            return comfy.sd.load_checkpoint_guess_config(
                path,
                output_vae=True,
                output_clip=True,
                embedding_directory=embeddings,
            )[:3]
        except RuntimeError as exc:
            # Comfy's own "could not detect model type" — the shape a bare
            # diffusion model has here. Re-raised untouched if the fallback
            # cannot read it either, so a genuinely broken file still reports
            # what ComfyUI said about it.
            loader = getattr(comfy.sd, "load_diffusion_model", None) or getattr(
                comfy.sd, "load_unet", None
            )
            if loader is None:
                raise
            log.info(
                "[PixlStash] %s is not an all-in-one checkpoint (%s) — loading "
                "it as a diffusion model, with no CLIP or VAE.",
                path,
                exc,
            )
            return (loader(path), None, None)

    @staticmethod
    def _fetch_record(checkpoint_id: str) -> dict:
        wanted = str(checkpoint_id or "").strip()
        if not wanted.isdigit():
            raise RuntimeError(
                f"{LABEL}: no checkpoint selected. Click “Browse checkpoints…” "
                "on the node and pick one."
            )
        client = shelf_file.client_for(LABEL)
        payload = client.get("/api/v1/checkpoints").json()
        rows = payload.get("checkpoints") if isinstance(payload, dict) else None
        if not isinstance(rows, list):
            raise RuntimeError(
                f"{LABEL}: the server did not return a checkpoint list (got "
                f"{type(payload).__name__})."
            )
        for row in rows:
            if isinstance(row, dict) and str(row.get("id")) == wanted:
                return row
        raise RuntimeError(
            f"{LABEL}: checkpoint #{wanted} is not on the shelf any more. Pick "
            "another one with “Browse checkpoints…”."
        )

"""PixlStash VAE Loader node.

Shaped after ComfyUI's built-in ``VAELoader``, with its dropdown replaced by
the shelf's Browse grid, and written against the same public API
(``comfy.utils.load_torch_file``, ``comfy.sd.VAE``) that every custom-node pack
calls.  No ComfyUI source is copied here; see the licence note in README.md.

The file is resolved (or fetched and cached under ``<vae>/pixlstash/``) by
``shelf_file``, exactly as the Adapter Loader's is — a VAE is hash-addressed on
the shelf like every other non-checkpoint, so the same routes serve it.

Deliberately *not* ported from the built-in: the TAESD / ``pixel_space`` /
``vae_approx`` branches.  Those files are composed from a pair of encoder and
decoder files in ComfyUI's own ``vae_approx`` folder, and PixlStash's scanner
excludes ``approxvae`` from the ``vae`` kind on purpose ("they live beside real
autoencoders and are not one").  There is therefore no shelf row that could
reach those branches, and the metadata argument they need goes with them.
"""

from __future__ import annotations

import logging

from . import shelf_file

log = logging.getLogger(__name__)

LABEL = "PixlStash VAE Loader"


class PixlStashVAELoader:
    """Loads a VAE off the PixlStash model shelf."""

    CATEGORY = "PixlStash"
    RETURN_TYPES = ("VAE",)
    RETURN_NAMES = ("vae",)
    FUNCTION = "load_vae"

    DESCRIPTION = (
        "Loads a VAE from your PixlStash model shelf, chosen by browsing a "
        "grid instead of hunting for a filename in a dropdown.\n\n"
        "Drop-in for the built-in Load VAE: one VAE output, wire it into a "
        "decode. Click “Browse VAEs…” to pick one.\n\n"
        "The file is used where it lies when PixlStash is on this machine, and "
        "otherwise fetched once and cached under your vae folder, verified "
        "against its SHA-256 before anything is written."
    )
    OUTPUT_TOOLTIPS = ("The VAE, for a VAE Decode or VAE Encode.",)

    @classmethod
    def INPUT_TYPES(cls):
        return {
            "required": {
                "vae_sha256": (
                    "STRING",
                    {
                        "default": "",
                        "tooltip": (
                            "Written by the Browse button — the SHA-256 of the "
                            "VAE on the shelf."
                        ),
                    },
                ),
            }
        }

    def load_vae(self, vae_sha256: str):
        import comfy.sd  # noqa: PLC0415 — only available inside ComfyUI
        import comfy.utils  # noqa: PLC0415

        _, path = shelf_file.resolve(vae_sha256, label=LABEL, folder_key="vae")
        # safe_load, as everywhere in this package: the path came off the wire.
        sd = comfy.utils.load_torch_file(path, safe_load=True)
        vae = comfy.sd.VAE(sd=sd)
        # Turns "a file that is not a VAE" into an error here rather than a
        # tensor-shape crash three nodes later. Guarded because it is a newer
        # addition to ComfyUI than this node's minimum.
        if hasattr(vae, "throw_exception_if_invalid"):
            vae.throw_exception_if_invalid()
        return (vae,)

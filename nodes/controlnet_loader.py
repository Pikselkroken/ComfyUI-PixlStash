"""PixlStash ControlNet Loader node.

Shaped after ComfyUI's built-in ``ControlNetLoader``, with its dropdown
replaced by the shelf's Browse grid, and written against the same public API
(``comfy.controlnet.load_controlnet``) that every custom-node pack calls.  No
ComfyUI source is copied here; see the licence note in README.md.

The file is resolved (or fetched and cached under ``<controlnet>/pixlstash/``)
by ``shelf_file``, exactly as the VAE Loader's is: a ControlNet is
hash-addressed on the shelf like every other non-checkpoint.

Deliberately *not* ported: ``DiffControlNetLoader``, which takes a MODEL as
well.  It is for the old diff-format ControlNets, and PixlStash swaps only the
plain loader in for one ComfyUI lacks.
"""

from __future__ import annotations

import logging

from . import lock, shelf_file

log = logging.getLogger(__name__)

LABEL = "PixlStash ControlNet Loader"


def load_file(path: str):
    """A ControlNet out of one file on this machine."""
    import comfy.controlnet  # noqa: PLC0415 — only available inside ComfyUI

    controlnet = comfy.controlnet.load_controlnet(path)
    # The built-in answers a file it cannot read with None rather than
    # raising; caught here rather than as a crash in the Apply node.
    if controlnet is None:
        raise RuntimeError(
            f"{LABEL}: the file is not a ControlNet ComfyUI can load ({path})."
        )
    return controlnet


class PixlStashControlNetLoader:
    """Loads a ControlNet off the PixlStash model shelf."""

    CATEGORY = "PixlStash"
    RETURN_TYPES = ("CONTROL_NET",)
    RETURN_NAMES = ("control_net",)
    FUNCTION = "load_controlnet"

    DESCRIPTION = (
        "Loads a ControlNet from your PixlStash model shelf, chosen by "
        "browsing a grid instead of hunting for a filename in a dropdown.\n\n"
        "Drop-in for the built-in Load ControlNet Model: one CONTROL_NET "
        "output, wire it into an Apply ControlNet. Click “Browse "
        "ControlNets…” to pick one.\n\n"
        "The file is used where it lies when PixlStash is on this machine, and "
        "otherwise fetched once and cached under your controlnet folder, "
        "verified against its SHA-256 before anything is written."
    )
    OUTPUT_TOOLTIPS = ("The ControlNet, for an Apply ControlNet node.",)

    @classmethod
    def INPUT_TYPES(cls):
        return {
            "required": {
                "control_net_sha256": (
                    "STRING",
                    {
                        "default": "",
                        "tooltip": (
                            "Written by the Browse button — the SHA-256 of the "
                            "ControlNet on the shelf."
                        ),
                    },
                ),
            }
        }

    def load_controlnet(self, control_net_sha256: str):
        record, path = shelf_file.resolve(
            control_net_sha256, label=LABEL, folder_key="controlnet"
        )
        return lock.report(
            (load_file(path),),
            models=[
                lock.shelf_model(
                    "controlnet",
                    sha256=record.get("sha256") or control_net_sha256,
                    record=record,
                )
            ],
        )

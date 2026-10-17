"""PixlStash Upscale Model Loader node.

ComfyUI's built-in ``UpscaleModelLoader`` with its dropdown replaced by the
shelf's Browse grid.  It *is* the built-in underneath: ComfyUI has no public
load-by-path call for an upscaler, and what the built-in hands on changes with
ComfyUI (a recent ``ImageUpscaleWithModel`` reads a ``patcher`` the loader
attaches), so a copy of its body would break on the next release.  No ComfyUI
source is copied here; see the licence note in README.md.

The file is resolved (or fetched and cached under ``<upscale_models>/pixlstash/``)
by ``shelf_file``, exactly as the ControlNet Loader's is.  Unlike the other
loaders it takes ``.pth`` as well as ``.safetensors``: nearly every upscaler
ships as a ``.pth``.  The built-in reads one with torch's ``weights_only``
unpickler, which refuses anything but tensors and plain containers: every
ComfyUI this pack supports (``requires-comfyui``) does, since v0.14.0 dropped
the unsafe path.
"""

from __future__ import annotations

import logging
import threading

from . import lock, shelf_file

log = logging.getLogger(__name__)

LABEL = "PixlStash Upscale Model Loader"
# The shelf's `upscaler` kind, and the first server that catalogues `.pth`.
MIN_SERVER_VERSION = "1.12.0"
SUFFIXES = (".pth", ".safetensors")

FOLDER_KEY = "upscale_models"

# One swap of `get_full_path` at a time, so an overlapping load can never
# restore the other's shim. ComfyUI runs prompts on one thread; this holds if
# something ever does not.
_SWAP_LOCK = threading.Lock()


def load_file(path: str):
    """An upscale model out of one file on this machine, by the built-in.

    The built-in looks its ``model_name`` up under ``upscale_models``, and the
    shelf's copy may be anywhere, so for the one call ``get_full_path`` answers
    *path* for itself and asks the real lookup for everything else.

    ponytail: a module-global swap for the length of one load, serialised by
    ``_SWAP_LOCK``. A concurrent lookup of any other name still reaches the
    real function; switch to a public load-by-path call if ComfyUI ever grows
    one.
    """
    import folder_paths  # noqa: PLC0415 — only available inside ComfyUI
    from comfy_extras.nodes_upscale_model import UpscaleModelLoader  # noqa: PLC0415

    def full_path(folder_name, filename):
        if folder_name == FOLDER_KEY and filename == path:
            return path
        return find(folder_name, filename)

    # The built-in calls `get_full_path_or_raise`, which calls this by its
    # module global.
    with _SWAP_LOCK:
        find = folder_paths.get_full_path
        folder_paths.get_full_path = full_path
        try:
            return UpscaleModelLoader().load_model(path)[0]
        finally:
            folder_paths.get_full_path = find


class PixlStashUpscaleModelLoader:
    """Loads an upscale model off the PixlStash model shelf."""

    CATEGORY = "PixlStash"
    RETURN_TYPES = ("UPSCALE_MODEL",)
    RETURN_NAMES = ("upscale_model",)
    FUNCTION = "load_upscale_model"

    DESCRIPTION = (
        "Loads an upscale model (ESRGAN, SwinIR, DAT, ...) from your PixlStash "
        "model shelf, chosen by browsing a grid instead of hunting for a "
        "filename in a dropdown.\n\n"
        "Drop-in for the built-in Load Upscale Model: one UPSCALE_MODEL "
        "output, wire it into an Upscale Image (using Model). Click “Browse "
        "upscalers…” to pick one.\n\n"
        "The file is used where it lies when PixlStash is on this machine, and "
        "otherwise fetched once and cached under your upscale_models folder, "
        "verified against its SHA-256 before anything is written."
    )
    OUTPUT_TOOLTIPS = ("The upscale model, for an Upscale Image (using Model).",)

    @classmethod
    def INPUT_TYPES(cls):
        return {
            "required": {
                "upscale_model_sha256": (
                    "STRING",
                    {
                        "default": "",
                        "tooltip": (
                            "Written by the Browse button — the SHA-256 of the "
                            "upscaler on the shelf."
                        ),
                    },
                ),
            }
        }

    def load_upscale_model(self, upscale_model_sha256: str):
        record, path = shelf_file.resolve(
            upscale_model_sha256,
            label=LABEL,
            folder_key=FOLDER_KEY,
            min_server_version=MIN_SERVER_VERSION,
            suffixes=SUFFIXES,
        )
        return lock.report(
            (load_file(path),),
            models=[
                lock.shelf_model(
                    "upscaler",
                    sha256=record.get("sha256") or upscale_model_sha256,
                    record=record,
                )
            ],
        )

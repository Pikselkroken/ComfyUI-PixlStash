"""Loading ``.gguf`` models, through the vendored copy of ComfyUI-GGUF.

Core ComfyUI reads safetensors and torch files only, so a quantised Flux UNet
or T5 on the shelf needs city96's ComfyUI-GGUF to become a MODEL or a CLIP.
Its code is vendored, unmodified, under ``vendor/comfyui_gguf/`` (see
``VENDORED.md`` there), and this is the only module that imports it.  The
vendored copy is used even when the real pack is installed, so there is one
code path and one tested version.

Everything is imported lazily, on a GGUF load: the ``gguf`` package is only
needed then, and a safetensors user never pays for it.

``label`` is the calling node's display name, as in ``shelf_file``.
"""

from __future__ import annotations

import inspect
import logging

log = logging.getLogger(__name__)

SUFFIX = ".gguf"


def is_gguf(path: str) -> bool:
    return str(path).lower().endswith(SUFFIX)


def _vendored(label: str):
    """The vendored ``nodes`` module, or an error naming what to install."""
    try:
        from ..vendor.comfyui_gguf import nodes  # noqa: PLC0415 — lazy on purpose
    except ImportError as exc:
        # Only the missing package is translated. Anything else missing (the
        # vendored files themselves, a ComfyUI module) is a different fix, and
        # telling that user to pip install gguf would send them the wrong way.
        if (exc.name or "").split(".")[0] != "gguf":
            raise
        raise RuntimeError(
            f"{label}: loading a GGUF needs the `gguf` Python package — run "
            "`pip install gguf` in ComfyUI's environment and restart ComfyUI."
        ) from exc
    return nodes


def load_clip(paths: list[str], clip_type, *, label: str):
    """One CLIP out of encoder files of which at least one is a GGUF.

    ``clip_type`` is the ``comfy.sd.CLIPType`` member. Upstream's ``load_data``
    reads each file by its extension, so a GGUF T5 beside a safetensors clip-l
    goes through here too.
    """
    gguf_nodes = _vendored(label)
    loader = gguf_nodes.CLIPLoaderGGUF()
    try:
        return loader.load_patcher(paths, clip_type, loader.load_data(paths))
    except Exception as exc:
        # Upstream's own refusals, e.g. NotImplementedError for a scaled-FP8
        # encoder beside a GGUF, carry the useful text; the label says which
        # node on the canvas raised it.
        log.error(
            "[PixlStash] GGUF text encoder load failed for %s (type %s): %s",
            paths,
            clip_type,
            exc,
        )
        raise RuntimeError(f"{label}: {exc}") from exc


def load_unet(path: str, *, label: str):
    """A MODEL out of a GGUF diffusion model at ``path``.

    Mirrors ``UnetLoaderGGUF.load_unet`` in ComfyUI-GGUF ``nodes.py`` at commit
    6ea2651, with its default dtypes. Upstream takes a filename out of
    ComfyUI's folder list and we have a path, so this is the one piece of
    upstream logic copied rather than called: whoever refreshes the vendored
    copy diffs that method and carries any change across (``VENDORED.md``).
    """
    import comfy.sd  # noqa: PLC0415 — only available inside ComfyUI

    gguf_nodes = _vendored(label)
    ops = gguf_nodes.GGMLOps()
    ops.Linear.dequant_dtype = None
    ops.Linear.patch_dtype = None

    try:
        sd, extra = gguf_nodes.gguf_sd_loader(path)
        kwargs = {}
        valid_params = inspect.signature(
            comfy.sd.load_diffusion_model_state_dict
        ).parameters
        if "metadata" in valid_params:
            kwargs["metadata"] = extra.get("metadata", {})
        model = comfy.sd.load_diffusion_model_state_dict(
            sd, model_options={"custom_operations": ops}, **kwargs
        )
    except Exception as exc:
        log.error("[PixlStash] GGUF diffusion model load failed for %s: %s", path, exc)
        raise RuntimeError(f"{label}: {exc}") from exc
    if model is None:
        raise RuntimeError(
            f"{label}: ComfyUI could not detect the model type of {path}. It is "
            "a GGUF, but not a diffusion model this ComfyUI knows."
        )
    model = gguf_nodes.GGUFModelPatcher.clone(model)
    model.patch_on_device = None
    return model

"""GGUF loading for the PixlStash loaders, through the vendored ComfyUI-GGUF.

Core ComfyUI reads safetensors and torch files only, so a ``.gguf`` text
encoder or diffusion model goes through city96's ComfyUI-GGUF instead, copied
unmodified into ``vendor/comfyui_gguf`` (see ``VENDORED.md`` there).  This is
the only module that imports it, and it does so lazily: the ``gguf`` package
is needed for a GGUF load and for nothing else.

The vendored copy is used even when the real pack is installed, so there is
one code path and it is the tested one.
"""

from __future__ import annotations

import importlib
import inspect


def is_gguf(path: str) -> bool:
    return path.lower().endswith(".gguf")


def _vendored(label: str):
    """The vendored ``nodes`` module, or a message naming what to install."""
    try:
        return importlib.import_module("..vendor.comfyui_gguf.nodes", __package__)
    except ModuleNotFoundError as exc:
        if exc.name != "gguf":
            raise
        raise RuntimeError(
            f"{label}: loading a GGUF needs the `gguf` Python package — run "
            "`pip install gguf` in ComfyUI's environment and restart ComfyUI."
        ) from exc


def load_clip(paths: list[str], clip_type, *, label: str):
    """One CLIP from encoder files, any of them GGUF; ``clip_type`` a ``CLIPType``.

    Upstream's ``load_data`` picks the reader per file by extension, so a GGUF
    T5 beside a safetensors clip-l loads as one CLIP.
    """
    nodes = _vendored(label)
    loader = nodes.CLIPLoaderGGUF()
    try:
        return loader.load_patcher(paths, clip_type, loader.load_data(paths))
    except Exception as exc:
        # Upstream's own errors, such as its NotImplementedError for a
        # scaled-FP8 encoder beside a GGUF one, named after our node.
        raise RuntimeError(f"{label}: {exc}") from exc


def load_unet(path: str, *, label: str):
    """A MODEL from a GGUF diffusion model file.

    Mirrors ``UnetLoaderGGUF.load_unet`` in upstream ``nodes.py`` at commit
    6ea2651 with its default dtypes, because upstream takes a filename under
    ``models/unet`` and we have a path. Re-diff it on every vendored refresh.
    """
    import comfy.sd  # noqa: PLC0415 — only available inside ComfyUI

    nodes = _vendored(label)
    ops = nodes.GGMLOps()
    ops.Linear.dequant_dtype = None
    ops.Linear.patch_dtype = None

    try:
        sd, extra = nodes.gguf_sd_loader(path)
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
        raise RuntimeError(f"{label}: {exc}") from exc
    if model is None:
        raise RuntimeError(
            f"{label}: ComfyUI could not detect the model type of {path}."
        )
    model = nodes.GGUFModelPatcher.clone(model)
    model.patch_on_device = None
    return model

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
import importlib.metadata
import inspect
import re

# The oldest ``gguf`` the pinned upstream works with (its requirements.txt).
# An older one fails importing the vendored code on a missing quant type, and a
# plain ``pip install gguf`` does not upgrade it, hence ``-U`` in the message.
MIN_GGUF = (0, 13)
_INSTALL = (
    'run `pip install -U "gguf>=0.13.0"` in ComfyUI\'s environment and restart ComfyUI.'
)


def is_gguf(path: str) -> bool:
    return path.lower().endswith(".gguf")


def _vendored(label: str):
    """The vendored ``nodes`` module, or a message naming what to install."""
    try:
        version = importlib.metadata.version("gguf")
    except importlib.metadata.PackageNotFoundError:
        version = None  # the import below says so, or finds an unpackaged copy
    if version and tuple(int(n) for n in re.findall(r"\d+", version)[:2]) < MIN_GGUF:
        raise RuntimeError(
            f"{label}: loading a GGUF needs `gguf` 0.13.0 or newer, and this "
            f"environment has {version} — {_INSTALL}"
        )
    try:
        return importlib.import_module("..vendor.comfyui_gguf.nodes", __package__)
    except ModuleNotFoundError as exc:
        if exc.name != "gguf":
            raise
        raise RuntimeError(
            f"{label}: loading a GGUF needs the `gguf` Python package — {_INSTALL}"
        ) from exc


def _is_oom(exc: Exception) -> bool:
    """Whether ComfyUI's executor would treat ``exc`` as out-of-memory.

    Such an error must reach it unwrapped: the executor unloads models and
    reports the OOM only for an exception ``is_oom`` recognises. (Interruption
    needs nothing here: ``InterruptProcessingException`` is a ``BaseException``
    and no ``except Exception`` catches it.)
    """
    import comfy.model_management as mm  # noqa: PLC0415 — only inside ComfyUI

    is_oom = getattr(mm, "is_oom", None)
    return is_oom(exc) if is_oom else isinstance(exc, mm.OOM_EXCEPTION)


def load_clip(paths: list[str], clip_type, *, label: str):
    """One CLIP from encoder files, any of them GGUF; ``clip_type`` a ``CLIPType``.

    The reader is picked per file by extension, so a GGUF T5 beside a
    safetensors clip-l loads as one CLIP.
    """
    nodes = _vendored(label)
    import comfy.utils  # noqa: PLC0415 — only available inside ComfyUI

    loader = nodes.CLIPLoaderGGUF()
    try:
        # Mirrors ``CLIPLoaderGGUF.load_data`` in upstream ``nodes.py`` at
        # commit 6ea2651, but with our case-insensitive ``is_gguf``: upstream's
        # ``p.endswith(".gguf")`` sends a ``T5.GGUF`` to the torch loader.
        # Re-diff it on every vendored refresh.
        data = []
        for p in paths:
            if is_gguf(p):
                sd = nodes.gguf_clip_loader(p)
            else:
                sd = comfy.utils.load_torch_file(p, safe_load=True)
                if "scaled_fp8" in sd:
                    raise NotImplementedError(
                        "Mixing scaled FP8 with GGUF is not supported! Use "
                        f"regular CLIP loader or switch model(s)\n({p})"
                    )
            data.append(sd)
        return loader.load_patcher(paths, clip_type, data)
    except Exception as exc:
        if _is_oom(exc):
            raise
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
        if _is_oom(exc):
            raise
        raise RuntimeError(f"{label}: {exc}") from exc
    if model is None:
        raise RuntimeError(
            f"{label}: ComfyUI could not detect the model type of {path}."
        )
    model = nodes.GGUFModelPatcher.clone(model)
    model.patch_on_device = None
    return model

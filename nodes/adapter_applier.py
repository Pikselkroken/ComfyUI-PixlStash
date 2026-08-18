"""PixlStash Apply Adapter node.

Applies a LoRA-shaped adapter file to a MODEL (and optionally a CLIP).  A thin
wrapper over two public ComfyUI API calls — ``comfy.utils.load_torch_file`` and
``comfy.sd.load_lora_for_models`` — which is deliberate: ComfyUI is GPL-3.0 and
this package is MIT, so nothing is copied out of its ``nodes.py``; this node is
written against the same public API every custom-node pack calls.

Chain several of these for several adapters, as with the built-in
``LoraLoader``.  The ``lora_path`` input is a plain STRING, so it accepts a path
from any pack that emits one.

The PixlStash Adapter (LoRA) Loader does not need this node — it applies its own
adapter and shares the ``AdapterApplier`` below to do it.

``clip`` is optional so model-only adapters work without a CLIP wire.  Note
what that costs: ComfyUI can't vary ``RETURN_TYPES`` per graph, so the CLIP
**output** is still declared and is empty when nothing was wired in.  Leave it
unconnected in that case — the built-in avoids the question by having a
separate model-only node with no CLIP output at all.
"""

from __future__ import annotations

import logging

log = logging.getLogger(__name__)


class AdapterApplier:
    """Loads an adapter file and patches MODEL / CLIP with it.

    The whole of the "apply a LoRA" job, owned by one object so both nodes in
    this pack do it identically: Apply Adapter, which is handed a path, and the
    Adapter Loader, which resolves one off the shelf first.

    Nothing here is copied out of ComfyUI. ``load_torch_file`` and
    ``load_lora_for_models`` are the two public calls every custom-node pack
    makes, which is what keeps this package MIT against a GPL-3.0 host.
    """

    def __init__(self) -> None:
        # (path, state_dict) of the last file loaded. A LoRA is hundreds of
        # megabytes and the graph re-runs on every generate; re-reading it each
        # time is the difference between usable and not.
        self._cached: tuple[str, object] | None = None

    def apply(self, model, clip, path, strength_model, strength_clip):
        import comfy.sd  # noqa: PLC0415 — only available inside ComfyUI
        import comfy.utils  # noqa: PLC0415

        # With no CLIP wired, clip stays None and is passed straight through,
        # which is what ComfyUI's own model-only LoRA node does internally.
        if strength_model == 0 and (clip is None or strength_clip == 0):
            return (model, clip)

        if self._cached is not None and self._cached[0] == path:
            state_dict = self._cached[1]
        else:
            # Dropped *before* the load, not after, so the outgoing state dict
            # is freeable while the incoming one is read — otherwise switching
            # adapters peaks at both resident at once. ComfyUI's own LoraLoader
            # clears its cache first for the same reason.
            self._cached = None
            state_dict = comfy.utils.load_torch_file(path, safe_load=True)
            self._cached = (path, state_dict)

        return comfy.sd.load_lora_for_models(
            model, clip, state_dict, strength_model, strength_clip
        )


class PixlStashApplyAdapter:
    """Loads an adapter file onto MODEL / CLIP."""

    CATEGORY = "PixlStash"
    RETURN_TYPES = ("MODEL", "CLIP")
    RETURN_NAMES = ("model", "clip")
    FUNCTION = "apply_adapter"

    DESCRIPTION = (
        "Applies a LoRA to a model, taking the file as an absolute PATH rather "
        "than a name off a dropdown — which is what lets a PixlStash Adapter "
        "(LoRA) Loader drive it.\n\n"
        "Chain several for several adapters, exactly as with the built-in LoRA "
        "loader. Leave clip unwired for a model-only adapter, and leave the "
        "CLIP output unwired too when you do. LoKr, LoHa, OFT and DoRA load "
        "here as well.\n\n"
        "You do not need this node to use the PixlStash shelf — the Adapter "
        "(LoRA) Loader applies its own adapter. This one is for a path that "
        "comes from somewhere else."
    )
    OUTPUT_TOOLTIPS = (
        "The model with the adapter applied. Chain into another Apply Adapter "
        "for a second one.",
        "The CLIP with the adapter applied — carries nothing if you left the "
        "clip input unwired, so leave this unwired too in that case.",
    )

    def __init__(self) -> None:
        self._applier = AdapterApplier()

    @classmethod
    def INPUT_TYPES(cls):
        strength = {
            "default": 1.0,
            "min": -100.0,
            "max": 100.0,
            "step": 0.01,
        }
        return {
            "required": {
                "model": ("MODEL", {"tooltip": "The diffusion model to patch."}),
                "lora_path": (
                    "STRING",
                    {
                        "forceInput": True,
                        "tooltip": (
                            "Wire the lora_path output of a PixlStash Adapter "
                            "(LoRA) Loader in here."
                        ),
                    },
                ),
                "strength_model": (
                    "FLOAT",
                    {**strength, "tooltip": "How strongly to patch the model."},
                ),
            },
            "optional": {
                "clip": (
                    "CLIP",
                    {
                        "tooltip": (
                            "Optional. Leave unwired for a model-only adapter — "
                            "but then leave the CLIP output unwired too, as it "
                            "has nothing to carry."
                        )
                    },
                ),
                "strength_clip": (
                    "FLOAT",
                    {**strength, "tooltip": "How strongly to patch CLIP."},
                ),
            },
        }

    def apply_adapter(
        self,
        model,
        lora_path: str,
        strength_model: float,
        clip=None,
        strength_clip: float = 1.0,
    ):
        # Checked before the strength short-circuit, so a graph missing its
        # path says so whatever the sliders happen to be set to.
        path = (lora_path or "").strip()
        if not path:
            raise RuntimeError(
                "PixlStash Apply Adapter: lora_path is empty. Wire it from a "
                "node that outputs an adapter file path."
            )
        return self._applier.apply(model, clip, path, strength_model, strength_clip)

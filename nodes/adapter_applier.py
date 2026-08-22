"""Applying a LoRA-shaped adapter file to a MODEL / CLIP.

A thin wrapper over two public ComfyUI API calls — ``comfy.utils.load_torch_file``
and ``comfy.sd.load_lora_for_models`` — which is deliberate: ComfyUI is GPL-3.0
and this package is MIT, so nothing is copied out of its ``nodes.py``; this is
written against the same public API every custom-node pack calls.

Used by the Adapter Loader, which resolves a file off the shelf first.  There
is no separate "apply" *node*: one that takes a path off a wire is a node
nothing in the ecosystem can feed, since every LoRA loader takes a name off a
combo widget.
"""

from __future__ import annotations

import logging

log = logging.getLogger(__name__)


class AdapterApplier:
    """Loads an adapter file and patches MODEL / CLIP with it.

    ``load_torch_file`` and ``load_lora_for_models`` are the two public calls
    every custom-node pack makes, which is what keeps this package MIT against
    a GPL-3.0 host.
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

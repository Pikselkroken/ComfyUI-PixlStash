"""PixlStash CLIP (text encoder) Loader node.

ComfyUI splits this in two — ``CLIPLoader`` for one encoder, ``DualCLIPLoader``
for the two that Flux, SD3 and HiDream need — because each takes its filenames
off a combo widget and a node cannot grow a widget.  Here both files come off
the Browse grid, so the second slot can simply be optional: one node, one
``comfy.sd.load_clip`` call, ``ckpt_paths`` one long or two.

``type`` is read off ``comfy.sd.CLIPType`` at call time rather than copied out
of ``nodes.py``.  That list grows with nearly every ComfyUI release (it is 28
values wide as of writing), and a hardcoded copy is a list that silently lacks
whatever model shipped last month.
"""

from __future__ import annotations

import logging

from . import shelf_file

log = logging.getLogger(__name__)

LABEL = "PixlStash CLIP Loader"

# Used only when ``comfy`` cannot be imported (outside ComfyUI, i.e. the tests).
# Not a maintained mirror of the enum — the point is that INPUT_TYPES returns
# *something* combo-shaped rather than raising during a node scan.
_FALLBACK_TYPES = ["stable_diffusion", "sdxl", "sd3", "flux", "wan", "qwen_image"]


def _clip_types() -> list[str]:
    try:
        import comfy.sd  # noqa: PLC0415

        names = [t.name.lower() for t in comfy.sd.CLIPType]
    except Exception:  # noqa: BLE001 — a missing enum must not break the scan
        return list(_FALLBACK_TYPES)
    # stable_diffusion first: it is the default and the one an SD1.5/SDXL user
    # wants, and the enum's own order is "whenever it was added".
    names.sort(key=lambda n: (n != "stable_diffusion", n))
    return names or list(_FALLBACK_TYPES)


def _encoder_folder() -> str:
    """The ComfyUI models folder text encoders live in.

    ``text_encoders`` on any current ComfyUI, ``clip`` on one old enough to
    predate the rename. Only consulted to decide where a *downloaded* copy is
    cached.
    """
    import folder_paths  # noqa: PLC0415

    return "text_encoders" if folder_paths.get_folder_paths("text_encoders") else "clip"


class PixlStashCLIPLoader:
    """Loads one or two text encoders off the PixlStash model shelf."""

    CATEGORY = "PixlStash"
    RETURN_TYPES = ("CLIP",)
    RETURN_NAMES = ("clip",)
    FUNCTION = "load_clip"

    DESCRIPTION = (
        "Loads a text encoder (CLIP) from your PixlStash model shelf, chosen "
        "by browsing a grid instead of hunting for a filename in a dropdown.\n\n"
        "Covers both of ComfyUI's loaders: pick one file for SD/SDXL, or two "
        "for the models that need a pair (Flux, SD3, HiDream — clip-l beside "
        "a T5 or a Llama). Set `type` to the model family you are building "
        "for; the list comes from ComfyUI itself.\n\n"
        "Each file is used where it lies when PixlStash is on this machine, "
        "and otherwise fetched once and cached under your text_encoders "
        "folder, verified against its SHA-256 before anything is written."
    )
    OUTPUT_TOOLTIPS = ("The CLIP, for a text encode.",)

    @classmethod
    def INPUT_TYPES(cls):
        return {
            "required": {
                "clip_sha256": (
                    "STRING",
                    {
                        "default": "",
                        "tooltip": (
                            "Written by the Browse button — the SHA-256 of the "
                            "text encoder on the shelf."
                        ),
                    },
                ),
                "type": (
                    _clip_types(),
                    {
                        "default": "stable_diffusion",
                        "tooltip": (
                            "The model family the encoder is being loaded for. "
                            "sd: clip-l · sdxl: clip-l + clip-g · flux/sd3: "
                            "clip-l + t5 · hidream: clip-l + llama."
                        ),
                    },
                ),
            },
            "optional": {
                "clip_sha256_2": (
                    "STRING",
                    {
                        "default": "",
                        "tooltip": (
                            "Optional second encoder, for the models that take "
                            "a pair. Leave empty for SD and SDXL."
                        ),
                    },
                ),
            },
        }

    def load_clip(self, clip_sha256: str, type: str, clip_sha256_2: str = ""):  # noqa: A002 — ComfyUI's own widget name
        import comfy.sd  # noqa: PLC0415 — only available inside ComfyUI
        import folder_paths  # noqa: PLC0415

        folder = _encoder_folder()
        paths = [
            shelf_file.resolve(sha, label=LABEL, folder_key=folder)[1]
            for sha in (clip_sha256, clip_sha256_2)
            if str(sha or "").strip()
        ]
        if not paths:
            raise RuntimeError(
                f"{LABEL}: no text encoder selected. Click “Browse text "
                "encoders…” on the node and pick one."
            )

        # getattr rather than a lookup table, exactly as the built-in does it:
        # the widget list came from this enum, so a miss means the enum changed
        # under a saved workflow, and stable_diffusion is the safe landing.
        clip_type = getattr(
            comfy.sd.CLIPType, type.upper(), comfy.sd.CLIPType.STABLE_DIFFUSION
        )
        clip = comfy.sd.load_clip(
            ckpt_paths=paths,
            embedding_directory=folder_paths.get_folder_paths("embeddings"),
            clip_type=clip_type,
        )
        return (clip,)

    @classmethod
    def VALIDATE_INPUTS(cls, type):  # noqa: A002
        # A workflow saved on a ComfyUI whose CLIPType had a value this one
        # does not would otherwise fail validation before `load_clip` can fall
        # back to stable_diffusion.
        return True

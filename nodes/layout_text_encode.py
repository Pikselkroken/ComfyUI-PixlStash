"""A text encoder that draws a layout prompt as regions, on any CLIP-encoded model.

MOSS-Image reads a layout prompt (JSON: a summary caption and items, each a
caption with a box) and puts each item where its box says. Other models read
the JSON as words. This node gives them the same layout through regional
conditioning: the summary is encoded over the whole canvas and every item's
caption over its box alone, as ComfyUI's own area conditioning does, so
PixlStash's character sheets should land in the grid the sheet cutter expects
whatever model draws them. No probe has measured how well each model holds it.

A layout prompt here is MOSS's (``bbox_coord_space`` ``abs_centralized``,
``bbox_format`` ``yxyx``) plus a ``canvas`` ``[width, height]`` the boxes
were laid out on: centred pixel boxes cannot be placed on a canvas of unknown
size, and MOSS's JSON does not carry one. Anything else, including plain text,
is encoded as it is, so the node stands in for ``CLIPTextEncode`` anywhere.

Written against the public CLIP API (``tokenize``,
``encode_from_tokens_scheduled``) and ComfyUI's conditioning format, never its
GPL source, which keeps this package MIT.
"""

from __future__ import annotations

import json
import logging

log = logging.getLogger(__name__)


def layout_regions(text: str):
    """``(summary, [(caption, (height, width, top, left)), ...])`` or ``None``.

    The box is each item's as fractions of the canvas, clamped to it, which is
    the ``percentage`` area ComfyUI's sampler scales to the latent it draws.
    ``None`` when *text* is not a layout prompt this node can place: plain
    text, JSON of another shape, or a layout with no canvas size.
    """
    try:
        layout = json.loads(text)
    except (TypeError, ValueError):
        return None
    if not isinstance(layout, dict) or not isinstance(layout.get("items"), list):
        return None
    summary = layout.get("summary")
    summary = summary.get("caption") if isinstance(summary, dict) else None
    canvas = layout.get("canvas")
    if (
        not isinstance(summary, str)
        or layout.get("bbox_coord_space") != "abs_centralized"
        or layout.get("bbox_format") != "yxyx"
        or not (isinstance(canvas, list) and len(canvas) == 2)
    ):
        log.warning(
            "PixlStash Layout Text Encode: the prompt is JSON but not a layout "
            "it can place (summary, abs_centralized yxyx boxes and a canvas "
            "size are needed), so it is encoded as plain text."
        )
        return None
    try:
        width, height = (float(v) for v in canvas)
    except (TypeError, ValueError):
        width = height = 0.0
    if width <= 0 or height <= 0:
        log.warning("PixlStash Layout Text Encode: canvas %s has no area.", canvas)
        return None

    def clamp(value: float) -> float:
        return min(1.0, max(0.0, value))

    regions = []
    for item in layout["items"]:
        caption = item.get("caption") if isinstance(item, dict) else None
        box = item.get("bbox") if isinstance(item, dict) else None
        try:
            if not isinstance(caption, str) or len(box) != 4:
                raise ValueError("no caption or no four-number box")
            y1, x1, y2, x2 = (float(v) for v in box)
        except (TypeError, ValueError) as exc:
            log.warning(
                "PixlStash Layout Text Encode: item %r is skipped: %s", item, exc
            )
            continue
        top, bottom = clamp(y1 / height + 0.5), clamp(y2 / height + 0.5)
        left, right = clamp(x1 / width + 0.5), clamp(x2 / width + 0.5)
        if bottom <= top or right <= left:
            log.warning("PixlStash Layout Text Encode: item box %s is empty.", box)
            continue
        regions.append((caption, (bottom - top, right - left, top, left)))
    return summary, regions


class PixlStashLayoutTextEncode:
    """Encode a prompt; a layout prompt becomes one area per item."""

    CATEGORY = "PixlStash"
    RETURN_TYPES = ("CONDITIONING",)
    FUNCTION = "encode"
    DESCRIPTION = (
        "Text encoder that reads a layout prompt (MOSS-Image's JSON plus a "
        "canvas size): the summary over the whole picture, each item's caption "
        "over its own box. Plain text is encoded as CLIPTextEncode would."
    )

    @classmethod
    def INPUT_TYPES(cls):
        return {
            "required": {
                "clip": ("CLIP",),
                "text": ("STRING", {"multiline": True, "dynamicPrompts": True}),
                # How much an item's caption outweighs the summary in its box.
                # ponytail: 1.0 is unprobed; tune it on real sheets.
                "region_strength": (
                    "FLOAT",
                    {"default": 1.0, "min": 0.0, "max": 10.0, "step": 0.05},
                ),
            }
        }

    def encode(self, clip, text, region_strength):
        if clip is None:
            raise RuntimeError(
                "PixlStash Layout Text Encode: no CLIP is wired, so there is "
                "nothing to encode the prompt with."
            )
        read = layout_regions(text)
        if read is None:
            return (self._encode(clip, text),)
        summary, regions = read
        conditioning = list(self._encode(clip, summary))
        for caption, (height, width, top, left) in regions:
            for tensor, extra in self._encode(clip, caption):
                extra = dict(extra)
                extra["area"] = ("percentage", height, width, top, left)
                extra["strength"] = region_strength
                extra["set_area_to_bounds"] = False
                conditioning.append([tensor, extra])
        return (conditioning,)

    @staticmethod
    def _encode(clip, text):
        return clip.encode_from_tokens_scheduled(clip.tokenize(text))

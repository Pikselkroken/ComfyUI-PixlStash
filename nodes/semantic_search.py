"""PixlStash Semantic Search node.

Performs a CLIP text-embedding search across the PixlStash vault and
returns a batch of matching pictures as IMAGE / MASK tensors.
"""

from __future__ import annotations

import io
import logging

import numpy as np
import torch
from PIL import Image

from ..connection import make_client

log = logging.getLogger(__name__)


class PixlStashSemanticSearch:
    """Returns vault pictures that match a free-text query."""

    CATEGORY = "PixlStash"
    RETURN_TYPES = ("IMAGE", "MASK", "INT")
    RETURN_NAMES = ("image", "mask", "batch_size")
    FUNCTION = "search"

    @classmethod
    def INPUT_TYPES(cls):
        return {
            "required": {
                "query": (
                    "STRING",
                    {
                        "default": "",
                        "multiline": False,
                        "tooltip": "Text prompt to search for semantically similar pictures.",
                    },
                ),
                "limit": (
                    "INT",
                    {
                        "default": 20,
                        "min": 1,
                        "max": 500,
                        "tooltip": "Maximum number of pictures to return.",
                    },
                ),
                "threshold": (
                    "FLOAT",
                    {
                        "default": 0.5,
                        "min": 0.0,
                        "max": 1.0,
                        "step": 0.01,
                        "tooltip": (
                            "Minimum cosine-similarity score required to include "
                            "a picture in the results."
                        ),
                    },
                ),
            },
            "hidden": {
                "url": "STRING",
                "token": "STRING",
                "verify_ssl": "BOOLEAN",
            },
        }

    # ------------------------------------------------------------------

    def search(
        self,
        query: str,
        limit: int,
        threshold: float,
        url: str = "",
        token: str = "",
        verify_ssl: bool = True,
    ):
        if not url.strip() or not token.strip():
            raise RuntimeError(
                "PixlStash Semantic Search: URL and API Token are required. "
                "Configure them in ComfyUI Settings › PixlStash."
            )
        if not query.strip():
            raise RuntimeError("PixlStash Semantic Search: query must not be empty.")

        client = make_client(url.strip(), token.strip(), verify_ssl)

        results = client.get(
            "/api/v1/pictures/search",
            params={
                "query": query.strip(),
                "limit": limit,
                "threshold": threshold,
                "offset": 0,
            },
        ).json()

        if not isinstance(results, list):
            raise RuntimeError(
                f"PixlStash Semantic Search: unexpected response format: {type(results)}"
            )

        picture_ids = self._extract_ids(results)
        if not picture_ids:
            raise RuntimeError(
                f"PixlStash Semantic Search: no pictures matched '{query}' "
                f"(threshold={threshold})."
            )

        pil_pairs: list[tuple[Image.Image, np.ndarray]] = []
        skipped: list[int] = []
        for pid in picture_ids:
            try:
                pil_pairs.append(self._fetch_image(client, pid))
            except RuntimeError as exc:
                log.warning("[PixlStash] Picture %s skipped — %s", pid, exc)
                skipped.append(pid)

        if skipped:
            log.warning("[PixlStash] %d picture(s) skipped: %s", len(skipped), skipped)
        if not pil_pairs:
            raise RuntimeError(
                f"PixlStash Semantic Search: none of the {len(skipped)} matched "
                "picture(s) could be fetched."
            )

        # Resize all to the first image's dimensions so torch.cat works.
        ref_w, ref_h = pil_pairs[0][0].size

        tensors: list[torch.Tensor] = []
        masks: list[torch.Tensor] = []
        for pil_img, mask_np in pil_pairs:
            if pil_img.size != (ref_w, ref_h):
                pil_img = pil_img.resize((ref_w, ref_h), Image.LANCZOS)
                mask_pil = Image.fromarray(
                    (mask_np * 255.0).clip(0, 255).astype(np.uint8)
                ).resize((ref_w, ref_h), Image.NEAREST)
                mask_np = np.array(mask_pil, dtype=np.float32) / 255.0

            img_np = np.array(pil_img.convert("RGB"), dtype=np.float32) / 255.0
            tensors.append(torch.from_numpy(img_np).unsqueeze(0))  # [1,H,W,3]
            masks.append(torch.from_numpy(mask_np).unsqueeze(0))  # [1,H,W]

        image_batch = torch.cat(tensors, dim=0)  # [N,H,W,3]
        mask_batch = torch.cat(masks, dim=0)  # [N,H,W]

        return (image_batch, mask_batch, len(pil_pairs))

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _extract_ids(results: list) -> list[int]:
        """Extract picture IDs from the search response list."""
        ids: list[int] = []
        for item in results:
            if isinstance(item, dict):
                pid = item.get("id") or item.get("picture_id")
                if pid is not None:
                    ids.append(int(pid))
            elif isinstance(item, (int, float)):
                ids.append(int(item))
        return ids

    @staticmethod
    def _fetch_image(
        client,
        picture_id: int,
    ) -> tuple[Image.Image, np.ndarray]:
        """Download a single picture and return (RGB PIL image, float32 mask)."""
        meta = client.get(f"/api/v1/pictures/{picture_id}/metadata").json()
        fmt = meta.get("format", "png").lower().strip(".")

        img_bytes = client.get(f"/api/v1/pictures/{picture_id}.{fmt}").content

        try:
            pil_img = Image.open(io.BytesIO(img_bytes))
            pil_img.load()
        except Exception as exc:
            raise RuntimeError(
                f"PixlStash: could not decode picture {picture_id}: {exc}"
            ) from exc

        if "A" in pil_img.getbands():
            alpha = np.array(pil_img.getchannel("A"), dtype=np.float32) / 255.0
            mask_np = 1.0 - alpha
        else:
            w, h = pil_img.size
            mask_np = np.zeros((h, w), dtype=np.float32)

        return pil_img.convert("RGB"), mask_np

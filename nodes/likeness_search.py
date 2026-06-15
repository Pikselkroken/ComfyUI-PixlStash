"""PixlStash Likeness Search node.

Uploads one or more query images to PixlStash and returns a batch of vault
pictures ranked by visual similarity (cosine similarity on CLIP embeddings).
When multiple query images are provided their per-candidate scores are
combined using the chosen strategy before ranking.

Parameters
----------
pool_size : int
    How many top-similar pictures to collect as the candidate pool.
select_count : int
    How many pictures to return. When ``select_count < pool_size`` the
    final selection is drawn randomly from the pool; otherwise the top
    ``pool_size`` results are returned directly.
threshold : float
    Minimum cosine-similarity score [0–1] required for a picture to
    enter the pool.
combine : str
    How to combine per-image scores when multiple query images are given.
    One of: mean, max, min, harmonic_mean, geometric_mean.
"""

from __future__ import annotations

import io
import logging

import numpy as np
import torch
from PIL import Image

from ..connection import make_client, read_credentials

log = logging.getLogger(__name__)


COMBINE_MODES = ["mean", "max", "min", "harmonic_mean", "geometric_mean"]
SEARCH_MODES = ["picture_likeness", "face_search"]
SEARCH_ENDPOINTS = {
    "picture_likeness": "/api/v1/pictures/likeness-search",
    "face_search": "/api/v1/pictures/face-search",
}


class PixlStashLikenessSearch:
    """Returns vault pictures that are visually similar to one or more query images."""

    CATEGORY = "PixlStash"
    RETURN_TYPES = (
        "IMAGE",
        "MASK",
        "PIXLSTASH_PROJECT",
        "PIXLSTASH_SET",
        "PIXLSTASH_CHARACTER",
        "INT",
    )
    RETURN_NAMES = (
        "image",
        "mask",
        "pixlstash_project",
        "pixlstash_set",
        "pixlstash_character",
        "batch_size",
    )
    FUNCTION = "search"

    @classmethod
    def INPUT_TYPES(cls):
        return {
            "required": {
                "search_mode": (
                    SEARCH_MODES,
                    {
                        "default": "picture_likeness",
                        "tooltip": (
                            "picture_likeness: full-image CLIP embedding comparison. "
                            "face_search: ArcFace facial feature comparison — "
                            "extracts the most prominent face from each query image."
                        ),
                    },
                ),
                "image": (
                    "IMAGE",
                    {
                        "tooltip": (
                            "One or more query images (batch). All frames are "
                            "uploaded and their scores combined using the "
                            "selected combine mode."
                        ),
                    },
                ),
                "combine": (
                    COMBINE_MODES,
                    {
                        "default": "mean",
                        "tooltip": (
                            "How to combine per-image scores when multiple query "
                            "images are provided. mean: average; max: best match "
                            "to any image; min: must match all; harmonic_mean / "
                            "geometric_mean: balance between extremes."
                        ),
                    },
                ),
                "pool_size": (
                    "INT",
                    {
                        "default": 50,
                        "min": 1,
                        "max": 2000,
                        "tooltip": (
                            "Number of top-similar pictures to build the candidate "
                            "pool from."
                        ),
                    },
                ),
                "select_count": (
                    "INT",
                    {
                        "default": 10,
                        "min": 1,
                        "max": 500,
                        "tooltip": (
                            "Number of pictures to return. When smaller than "
                            "pool_size, a random sample is drawn from the pool."
                        ),
                    },
                ),
                "threshold": (
                    "FLOAT",
                    {
                        "default": 0.0,
                        "min": 0.0,
                        "max": 1.0,
                        "step": 0.01,
                        "tooltip": (
                            "Minimum cosine-similarity score required to include "
                            "a picture in the pool."
                        ),
                    },
                ),
            },
            "optional": {
                "pixlstash_project": (
                    "PIXLSTASH_PROJECT",
                    {
                        "forceInput": True,
                        "tooltip": "Wire from a Project Loader to restrict the search.",
                    },
                ),
                "pixlstash_set": (
                    "PIXLSTASH_SET",
                    {
                        "forceInput": True,
                        "tooltip": "Wire from a Set Loader to restrict the search.",
                    },
                ),
                "pixlstash_character": (
                    "PIXLSTASH_CHARACTER",
                    {
                        "forceInput": True,
                        "tooltip": "Wire from a Character Loader to restrict the search.",
                    },
                ),
            },
        }

    # ------------------------------------------------------------------

    def search(
        self,
        search_mode: str,
        image: torch.Tensor,
        combine: str,
        pool_size: int,
        select_count: int,
        threshold: float,
        pixlstash_project: str = "",
        pixlstash_set: str = "",
        pixlstash_character: str = "",
        url: str = "",
        token: str = "",
        verify_ssl: bool = True,
    ):
        # Credentials are resolved server-side from ComfyUI Settings ->
        # PixlStash and never injected into the prompt.
        url, token, verify_ssl = read_credentials(url, token, verify_ssl)
        if not url or not token:
            raise RuntimeError(
                "PixlStash Likeness Search: URL and API Token are required. "
                "Configure them in ComfyUI Settings › PixlStash."
            )

        client = make_client(url, token, verify_ssl)

        endpoint = SEARCH_ENDPOINTS.get(
            search_mode, SEARCH_ENDPOINTS["picture_likeness"]
        )

        # Convert every frame in the batch to PNG bytes for upload.
        query_files = [
            ("files", (f"query_{i}.png", png_bytes, "image/png"))
            for i, png_bytes in enumerate(self._batch_to_png_bytes(image))
        ]

        # Decide whether to use random sampling from the pool.
        use_random = select_count < pool_size
        params: dict[str, object] = {"threshold": threshold, "combine": combine}
        if use_random:
            params["random"] = "true"
            params["pool_m"] = pool_size
            params["top_n"] = select_count
        else:
            params["random"] = "false"
            params["top_n"] = pool_size

        if pixlstash_project.strip():
            params["project_id"] = pixlstash_project.strip()
        if pixlstash_set.strip():
            params["set_id"] = pixlstash_set.strip()
        if pixlstash_character.strip():
            params["character_id"] = pixlstash_character.strip()

        response = client.post(
            endpoint,
            params=params,
            files=query_files,
        )

        results = response.json()
        if not isinstance(results, list):
            raise RuntimeError(
                f"PixlStash Likeness Search: unexpected response format: {type(results)}"
            )

        picture_ids = self._extract_ids(results)
        if not picture_ids:
            raise RuntimeError(
                f"PixlStash Likeness Search: no pictures matched the query "
                f"(mode={search_mode}, pool_size={pool_size}, threshold={threshold}, combine={combine})."
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
                f"PixlStash Likeness Search: none of the {len(skipped)} matched "
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

        return (
            image_batch,
            mask_batch,
            pixlstash_project,
            pixlstash_set,
            pixlstash_character,
            len(pil_pairs),
        )

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _batch_to_png_bytes(image: torch.Tensor) -> list[bytes]:
        """Convert every frame in a ComfyUI IMAGE batch to PNG bytes.

        image shape: [B, H, W, C], float32 in [0, 1].
        Returns a list of PNG-encoded byte strings, one per frame.
        """
        result: list[bytes] = []
        for i in range(image.shape[0]):
            arr = (image[i].cpu().numpy() * 255.0).clip(0, 255).astype(np.uint8)
            pil_img = Image.fromarray(arr, mode="RGB")
            buf = io.BytesIO()
            pil_img.save(buf, format="PNG")
            result.append(buf.getvalue())
        return result

    @staticmethod
    def _extract_ids(results: list) -> list[int]:
        """Extract picture IDs from the likeness-search response list."""
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

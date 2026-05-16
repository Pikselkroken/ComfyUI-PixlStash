"""PixlStash Image Loader node for ComfyUI.

Fetches one or more images from a PixlStash vault and returns them as a
batched IMAGE tensor together with an alpha-derived MASK and the resolved
comma-separated picture ID string.
"""
from __future__ import annotations

import io

import numpy as np
import torch
from PIL import Image

from .connection import PixlStashClient

# Sort key → (API sort param, descending flag)
_SORT_MAP: dict[str, tuple[str, bool | None]] = {
    "score_desc":    ("score",       True),
    "imported_desc": ("imported_at", True),
    "random":        ("random",      None),
}


class PixlStashImageLoader:
    """Replaces LoadImage — fetches pictures from PixlStash.

    When more than one image is selected, outputs a batched IMAGE tensor
    (shape [N, H, W, C]).  All images are resized to the dimensions of the
    first picture in the batch so that torch.cat() succeeds.

    The ``picture_ids`` input is a comma-separated string of integer IDs
    managed by the JS picker widget.  If it is left empty the node falls
    back to a live API query using the sort / limit / set_id parameters.
    """

    CATEGORY = "image/input"
    RETURN_TYPES = ("IMAGE", "MASK", "STRING", "STRING", "STRING", "STRING")
    RETURN_NAMES = ("image", "mask", "picture_ids", "project_id", "set_id", "character_id")
    FUNCTION = "load_images"

    @classmethod
    def INPUT_TYPES(cls):
        return {
            "required": {
                "pixlstash_url": (
                    "STRING",
                    {
                        "default": "https://localhost:8000",
                        "multiline": False,
                        "tooltip": "Base URL of your PixlStash instance.",
                    },
                ),
                "api_token": (
                    "STRING",
                    {
                        "default": "",
                        "multiline": False,
                        "password": True,
                        "tooltip": (
                            "Bearer token for authentication. "
                            "Configure once in ComfyUI Settings › PixlStash "
                            "to avoid re-entering it on every node."
                        ),
                    },
                ),
                "verify_ssl": (
                    "BOOLEAN",
                    {
                        "default": True,
                        "tooltip": (
                            "Uncheck to accept self-signed certificates. "
                            "A warning will be shown in the browser console."
                        ),
                    },
                ),
                "picture_ids": (
                    "STRING",
                    {
                        "default": "",
                        "multiline": False,
                        "tooltip": (
                            "Comma-separated picture IDs. "
                            "Use the Browse button to pick images interactively."
                        ),
                    },
                ),
                "sort": (list(_SORT_MAP.keys()),),
                "limit": (
                    "INT",
                    {
                        "default": 1,
                        "min": 1,
                        "max": 100,
                        "tooltip": (
                            "Maximum number of pictures to load "
                            "(applies when picture_ids is empty)."
                        ),
                    },
                ),
            },
            "optional": {
                "project_id": (
                    "STRING",
                    {
                        "default": "",
                        "forceInput": False,
                        "tooltip": (
                            "Filter picker to a project. "
                            "Populated by the JS widget from GET /projects."
                        ),
                    },
                ),
                "set_id": (
                    "STRING",
                    {
                        "default": "",
                        "forceInput": False,
                        "tooltip": (
                            "Filter picker to a picture set. "
                            "Populated by the JS widget from GET /picture_sets."
                        ),
                    },
                ),
                "character_id": (
                    "STRING",
                    {
                        "default": "",
                        "forceInput": False,
                        "tooltip": (
                            "Filter picker to a character. "
                            "Populated by the JS widget from GET /characters."
                        ),
                    },
                ),
            },
        }

    # ------------------------------------------------------------------
    # Main function
    # ------------------------------------------------------------------

    def load_images(
        self,
        pixlstash_url: str,
        api_token: str,
        verify_ssl: bool,
        picture_ids: str,
        sort: str,
        limit: int,
        project_id: str = "",
        set_id: str = "",
        character_id: str = "",
    ):
        if not pixlstash_url.strip():
            raise RuntimeError("PixlStash: pixlstash_url is required.")
        if not api_token.strip():
            raise RuntimeError("PixlStash: api_token is required.")

        client = PixlStashClient(pixlstash_url, api_token, verify_ssl)
        ids = self._resolve_ids(client, picture_ids, sort, limit, project_id, set_id, character_id)
        if not ids:
            raise RuntimeError(
                "PixlStash: no picture IDs to load. "
                "Enter IDs manually or use the Browse button to select images."
            )

        # Download all images; collect as PIL objects first so we can
        # harmonise sizes before converting to tensors.
        pil_pairs: list[tuple[Image.Image, np.ndarray]] = []
        for pid in ids:
            pil_img, mask_np = self._load_single(client, pid)
            pil_pairs.append((pil_img, mask_np))

        # Use the first image as the reference size for the batch.
        ref_w, ref_h = pil_pairs[0][0].size  # PIL size is (W, H)

        tensors: list[torch.Tensor] = []
        masks: list[torch.Tensor] = []
        for pil_img, mask_np in pil_pairs:
            if pil_img.size != (ref_w, ref_h):
                pil_img = pil_img.resize((ref_w, ref_h), Image.LANCZOS)
                mask_pil = Image.fromarray(
                    (mask_np * 255.0).clip(0, 255).astype(np.uint8)
                )
                mask_pil = mask_pil.resize((ref_w, ref_h), Image.NEAREST)
                mask_np = np.array(mask_pil, dtype=np.float32) / 255.0

            img_np = np.array(pil_img.convert("RGB"), dtype=np.float32) / 255.0
            tensors.append(torch.from_numpy(img_np).unsqueeze(0))   # [1, H, W, 3]
            masks.append(torch.from_numpy(mask_np).unsqueeze(0))    # [1, H, W]

        image_batch = torch.cat(tensors, dim=0)   # [N, H, W, 3]
        mask_batch = torch.cat(masks, dim=0)       # [N, H, W]
        picture_ids_out = ",".join(str(i) for i in ids)

        return (image_batch, mask_batch, picture_ids_out, project_id, set_id, character_id)

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _resolve_ids(
        client: PixlStashClient,
        picture_ids: str,
        sort: str,
        limit: int,
        project_id: str,
        set_id: str,
        character_id: str,
    ) -> list[int]:
        """Return the list of integer picture IDs to load.

        If *picture_ids* is non-empty, parse it directly and apply *limit*.
        Otherwise query ``/pictures`` using the sort / limit / filter params.
        """
        raw = picture_ids.strip()
        if raw:
            try:
                ids = [int(x.strip()) for x in raw.split(",") if x.strip()]
            except ValueError as exc:
                raise RuntimeError(
                    f"PixlStash: picture_ids contains a non-integer value: {exc}"
                ) from exc
            return ids[:limit]

        # Browse mode — query the API
        params: dict[str, object] = {
            "fields": "grid",
            "limit": limit,
            "offset": 0,
        }
        if project_id and project_id.strip():
            params["project_id"] = project_id.strip()
        if set_id and set_id.strip():
            params["set_id"] = set_id.strip()
        if character_id and character_id.strip():
            params["character_id"] = character_id.strip()

        if sort in _SORT_MAP:
            sort_key, descending = _SORT_MAP[sort]
            params["sort"] = sort_key
            if descending is not None:
                params["descending"] = "true" if descending else "false"

        response = client.get("/pictures", params=params)
        pictures = response.json()
        return [p["id"] for p in pictures]

    @staticmethod
    def _load_single(
        client: PixlStashClient,
        picture_id: int,
    ) -> tuple[Image.Image, np.ndarray]:
        """Download a single picture and return (PIL RGB image, mask array).

        The mask follows ComfyUI's convention: 1.0 = region to inpaint,
        0.0 = region to keep.  Images without an alpha channel get an
        all-zeros mask (keep everything).
        """
        # Fetch metadata to determine the stored file format.
        meta = client.get(f"/pictures/{picture_id}/metadata").json()
        fmt = meta.get("format", "png").lower().strip(".")

        # Download the full-resolution image.
        img_bytes = client.get(f"/pictures/{picture_id}.{fmt}").content

        try:
            pil_img = Image.open(io.BytesIO(img_bytes))
            pil_img.load()  # Materialise before the BytesIO is GC'd
        except Exception as exc:
            raise RuntimeError(
                f"PixlStash: could not decode image for picture {picture_id}: {exc}"
            ) from exc

        # Build the mask from the alpha channel, if present.
        if "A" in pil_img.getbands():
            alpha_np = np.array(pil_img.getchannel("A"), dtype=np.float32) / 255.0
            mask_np = 1.0 - alpha_np          # ComfyUI: 1 = inpaint, 0 = keep
        else:
            w, h = pil_img.size
            mask_np = np.zeros((h, w), dtype=np.float32)

        return pil_img.convert("RGB"), mask_np

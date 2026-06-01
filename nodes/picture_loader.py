"""PixlStash Picture Loader node.

Browses and loads pictures from a PixlStash vault.  Two operating modes:

* **Picker mode** — the JS extension's Browse button opens a modal
  thumbnail browser; the user multi-selects pictures; their IDs are
  written into the ``picture_ids`` widget; Python fetches and decodes
  exactly those images.

* **Browse mode** — ``picture_ids`` is empty; Python queries
  ``GET /pictures`` using the active sort key, limit, and any filter
  IDs to assemble the batch automatically (useful for headless / API
  use and random-sample workflows).

The three filter IDs (``project_id``, ``set_id``, ``character_id``) are
passed through as outputs so a downstream Saver can receive them without
requiring extra wires from each individual filter node.
"""

from __future__ import annotations

import io
import logging

import numpy as np
import torch
from PIL import Image

from ..connection import make_client, read_credentials

log = logging.getLogger(__name__)


class PixlStashPictureLoader:
    """Fetches pictures from PixlStash and outputs IMAGE / MASK tensors."""

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
    FUNCTION = "load_pictures"

    @classmethod
    def INPUT_TYPES(cls):
        return {
            "required": {
                "picture_ids": (
                    "STRING",
                    {
                        "default": "",
                        "multiline": False,
                        "tooltip": (
                            "Comma-separated picture IDs. "
                            "Use the Browse button to pick interactively. "
                            "Leave empty to auto-select using sort + filters."
                        ),
                    },
                ),
            },
            "optional": {
                "pixlstash_project": (
                    "PIXLSTASH_PROJECT",
                    {
                        "forceInput": True,
                        "tooltip": "Wire from a Project Loader.",
                    },
                ),
                "pixlstash_set": (
                    "PIXLSTASH_SET",
                    {
                        "forceInput": True,
                        "tooltip": "Wire from a Set Loader.",
                    },
                ),
                "pixlstash_character": (
                    "PIXLSTASH_CHARACTER",
                    {
                        "forceInput": True,
                        "tooltip": "Wire from a Character Loader.",
                    },
                ),
            },
        }

    # ------------------------------------------------------------------
    # Main function
    # ------------------------------------------------------------------

    def load_pictures(
        self,
        picture_ids: str,
        pixlstash_project: str = "",
        pixlstash_set: str = "",
        pixlstash_character: str = "",
        url: str = "",
        token: str = "",
        verify_ssl: bool = True,
    ):
        # Credentials are resolved server-side from ComfyUI Settings ->
        # PixlStash (or PIXLSTASH_* env vars) and never injected into the prompt.
        url, token, verify_ssl = read_credentials(url, token, verify_ssl)
        if not url or not token:
            raise RuntimeError(
                "PixlStash Picture Loader: URL and API Token are required. "
                "Configure them in ComfyUI Settings › PixlStash."
            )
        client = make_client(url, token, verify_ssl)

        ids = self._resolve_ids(
            client,
            picture_ids,
            pixlstash_project,
            pixlstash_set,
            pixlstash_character,
        )
        if not ids:
            raise RuntimeError(
                "PixlStash Picture Loader: no pictures to load. "
                "Use the Browse button to select pictures, or wire "
                "filter nodes and leave picture_ids empty for auto-select."
            )

        pil_pairs: list[tuple[Image.Image, np.ndarray]] = []
        skipped: list[int] = []
        for pid in ids:
            try:
                pil_pairs.append(self._fetch_image(client, pid))
            except RuntimeError as exc:
                log.warning("[PixlStash] Picture %s skipped — %s", pid, exc)
                skipped.append(pid)

        if skipped:
            log.warning(
                "[PixlStash] %d picture(s) skipped: %s",
                len(skipped),
                skipped,
            )
        if not pil_pairs:
            raise RuntimeError(
                f"PixlStash Picture Loader: none of the {len(skipped)} selected "
                "picture(s) could be found. "
                "Open Browse and re-select valid pictures."
            )

        # Resize all images to the first image's dimensions so torch.cat works.
        ref_w, ref_h = pil_pairs[0][0].size  # PIL size is (W, H)

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
    def _resolve_ids(
        client,
        picture_ids: str,
        project_id: str,
        set_id: str,
        character_id: str,
    ) -> list[int]:
        """Return the integer IDs to load.

        If ``picture_ids`` is non-empty, parse it (up to 200).
        Otherwise query ``GET /pictures`` with the active filters.
        """
        raw = picture_ids.strip()
        if raw:
            try:
                return [int(x.strip()) for x in raw.split(",") if x.strip()][:200]
            except ValueError as exc:
                raise RuntimeError(
                    f"PixlStash: picture_ids contains a non-integer value: {exc}"
                ) from exc

        params: dict[str, object] = {
            "fields": "grid",
            "sort": "IMPORTED_AT",
            "descending": "true",
        }
        if project_id.strip():
            params["project_id"] = project_id.strip()
        if set_id.strip():
            params["set_id"] = set_id.strip()
        if character_id.strip():
            params["character_id"] = character_id.strip()

        pictures = client.get("/api/v1/pictures", params=params).json()
        return [p["id"] for p in pictures]

    @staticmethod
    def _fetch_image(
        client,
        picture_id: int,
    ) -> tuple[Image.Image, np.ndarray]:
        """Download a single picture; return (RGB PIL image, float32 mask).

        The mask follows ComfyUI convention: 1.0 = inpaint, 0.0 = keep.
        Images without an alpha channel get an all-zero mask.
        """
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

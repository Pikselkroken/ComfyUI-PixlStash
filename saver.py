"""PixlStash Image Saver node for ComfyUI.

Encodes IMAGE tensors to PNG (with optional embedded workflow metadata),
uploads them to PixlStash via the async import endpoint, and optionally
adds the resulting pictures to a set and/or assigns a score.
"""
from __future__ import annotations

import io
import json
import time

import numpy as np
from PIL import Image
from PIL.PngImagePlugin import PngInfo

from .connection import PixlStashClient

# Interval between import-status poll requests (seconds)
_POLL_INTERVAL = 0.5


class PixlStashImageSaver:
    """Replaces SaveImage — uploads images to PixlStash.

    Returns a comma-separated string of the newly created picture IDs so
    that downstream nodes (tag writers, set assigners, etc.) can act on them.
    The workflow JSON is embedded into each PNG as ``tEXt`` chunks with the
    keys ``prompt`` and ``workflow``, matching ComfyUI's own SaveImage format.
    """

    CATEGORY = "image/output"
    RETURN_TYPES = ("STRING",)
    RETURN_NAMES = ("picture_ids",)
    OUTPUT_NODE = True
    FUNCTION = "save_images"

    @classmethod
    def INPUT_TYPES(cls):
        return {
            "required": {
                "images": ("IMAGE",),
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
                            "Configure once in ComfyUI Settings › PixlStash."
                        ),
                    },
                ),
                "verify_ssl": (
                    "BOOLEAN",
                    {
                        "default": True,
                        "tooltip": "Uncheck to accept self-signed certificates.",
                    },
                ),
                "filename_prefix": (
                    "STRING",
                    {
                        "default": "comfyui",
                        "multiline": False,
                        "tooltip": "Prefix for the generated PNG filenames.",
                    },
                ),
                "save_workflow": (
                    "BOOLEAN",
                    {
                        "default": True,
                        "tooltip": (
                            "Embed the serialised ComfyUI workflow and prompt "
                            "into PNG tEXt chunks before uploading."
                        ),
                    },
                ),
            },
            "optional": {
                "set_id": (
                    "INT",
                    {
                        "default": 0,
                        "min": 0,
                        "tooltip": (
                            "Add saved pictures to this picture set "
                            "(0 = skip)."
                        ),
                    },
                ),
                "score": (
                    "INT",
                    {
                        "default": -1,
                        "min": -1,
                        "max": 5,
                        "tooltip": "Pre-assign a score 0–5 (-1 = leave unscored).",
                    },
                ),
            },
            "hidden": {
                "prompt": "PROMPT",
                "extra_pnginfo": "EXTRA_PNGINFO",
            },
        }

    # ------------------------------------------------------------------
    # Main function
    # ------------------------------------------------------------------

    def save_images(
        self,
        images,
        pixlstash_url: str,
        api_token: str,
        verify_ssl: bool,
        filename_prefix: str,
        save_workflow: bool,
        set_id: int = 0,
        score: int = -1,
        prompt=None,
        extra_pnginfo=None,
    ):
        if not pixlstash_url.strip():
            raise RuntimeError("PixlStash: pixlstash_url is required.")
        if not api_token.strip():
            raise RuntimeError("PixlStash: api_token is required.")

        client = PixlStashClient(pixlstash_url, api_token, verify_ssl)

        # Encode each tensor to PNG bytes
        files: list[tuple[str, bytes]] = []
        for idx in range(images.shape[0]):
            img_np = (
                images[idx].cpu().numpy() * 255.0
            ).clip(0, 255).astype(np.uint8)
            pil_img = Image.fromarray(img_np, mode="RGB")
            filename = f"{filename_prefix}_{idx + 1:05d}.png"
            png_bytes = self._encode_png(
                pil_img,
                save_workflow=save_workflow,
                prompt=prompt,
                extra_pnginfo=extra_pnginfo,
            )
            files.append((filename, png_bytes))

        # Upload and wait for import to complete
        picture_ids = self._upload(client, files)

        # Optional post-processing
        if set_id and set_id > 0 and picture_ids:
            self._add_to_set(client, set_id, picture_ids)

        if score is not None and score >= 0 and picture_ids:
            for pid in picture_ids:
                self._set_score(client, pid, score)

        ids_str = ",".join(str(i) for i in picture_ids)
        return {"ui": {"picture_ids": [ids_str]}, "result": (ids_str,)}

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _encode_png(
        pil_img: Image.Image,
        *,
        save_workflow: bool,
        prompt,
        extra_pnginfo,
    ) -> bytes:
        """Encode *pil_img* to PNG bytes, optionally embedding workflow data."""
        metadata = PngInfo()
        if save_workflow:
            if prompt is not None:
                metadata.add_text("prompt", json.dumps(prompt))
            if extra_pnginfo is not None:
                for key, value in extra_pnginfo.items():
                    metadata.add_text(key, json.dumps(value))
        buf = io.BytesIO()
        pil_img.save(buf, format="PNG", pnginfo=metadata, compress_level=4)
        return buf.getvalue()

    @staticmethod
    def _upload(
        client: PixlStashClient,
        files: list[tuple[str, bytes]],
    ) -> list[int]:
        """Upload all files in one multipart request and poll until done."""
        multipart = [
            ("file", (name, data, "image/png"))
            for name, data in files
        ]
        response = client.post("/pictures/import", files=multipart)
        task_id = response.json()["task_id"]

        # Poll the import status endpoint until the task completes or fails.
        while True:
            status_resp = client.get(
                "/pictures/import/status", params={"task_id": task_id}
            )
            data = status_resp.json()
            status = data.get("status")

            if status == "completed":
                results = data.get("results", [])
                return [r["id"] for r in results]

            if status == "failed":
                error_msg = data.get("error", "unknown error")
                raise RuntimeError(
                    f"PixlStash: import task failed — {error_msg}"
                )

            time.sleep(_POLL_INTERVAL)

    @staticmethod
    def _add_to_set(
        client: PixlStashClient,
        set_id: int,
        picture_ids: list[int],
    ) -> None:
        client.post(
            f"/picture_sets/{set_id}/members",
            json={"picture_ids": picture_ids},
        )

    @staticmethod
    def _set_score(
        client: PixlStashClient,
        picture_id: int,
        score: int,
    ) -> None:
        client.patch(f"/pictures/{picture_id}", json={"score": score})

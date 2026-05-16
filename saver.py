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
                "project_id": (
                    "STRING",
                    {
                        "default": "",
                        "forceInput": False,
                        "tooltip": (
                            "Assign saved pictures to this project. "
                            "Accepts a wired string from the Loader or a standalone combo."
                        ),
                    },
                ),
                "set_id": (
                    "STRING",
                    {
                        "default": "",
                        "forceInput": False,
                        "tooltip": (
                            "Add saved pictures to this picture set. "
                            "Accepts a wired string from the Loader or a standalone combo."
                        ),
                    },
                ),
                "character_id": (
                    "STRING",
                    {
                        "default": "",
                        "forceInput": False,
                        "tooltip": (
                            "Assign saved pictures to this character. "
                            "Accepts a wired string from the Loader or a standalone combo."
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
        project_id: str = "",
        set_id: str = "",
        character_id: str = "",
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
        picture_ids = self._upload(client, files, project_id=project_id)

        # Optional post-processing
        if set_id and set_id.strip() and picture_ids:
            self._add_to_set(client, int(set_id.strip()), picture_ids)

        if character_id and character_id.strip() and picture_ids:
            self._assign_character(client, int(character_id.strip()), picture_ids)

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
        project_id: str = "",
    ) -> list[int]:
        """Upload all files in one multipart request and poll until done."""
        multipart = [
            ("file", (name, data, "image/png"))
            for name, data in files
        ]
        data = {}
        if project_id and project_id.strip():
            data["project_id"] = project_id.strip()
        response = client.post("/pictures/import", files=multipart, data=data or None)
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
                # Each result has status "success" or "duplicate"; both
                # yield a valid picture_id that can be used downstream.
                return [r["picture_id"] for r in results]

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
    def _assign_character(
        client: PixlStashClient,
        character_id: int,
        picture_ids: list[int],
    ) -> None:
        """Associate pictures with a character.

        The server identifies the best face in each picture and assigns it.
        If face extraction has not yet run, the server queues a pending
        assignment — no retry logic is needed here.

        Raises a clear error if the face extraction worker is not running
        (server returns HTTP 400 in that case).
        """
        try:
            client.post(
                f"/characters/{character_id}/faces",
                json={"picture_ids": picture_ids},
            )
        except RuntimeError as exc:
            if "HTTP 400" in str(exc):
                raise RuntimeError(
                    "PixlStash: cannot assign character — the face extraction "
                    "worker is not running. Start the worker and retry."
                ) from exc
            raise

    @staticmethod
    def _set_score(
        client: PixlStashClient,
        picture_id: int,
        score: int,
    ) -> None:
        client.patch(f"/pictures/{picture_id}", json={"score": score})

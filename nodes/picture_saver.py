"""PixlStash Picture Saver node.

Encodes IMAGE tensors to PNG (with optional embedded workflow metadata),
uploads them to PixlStash via the async import endpoint, then optionally
assigns the new pictures to a project, set, and/or character.

The ``connection`` input is **independent** — wire a separate Connector
with a write-scoped (ALL) token if the upstream Connector only has read
access.  HTTP 403 responses from any write operation raise a message that
explicitly names the write-scope requirement.
"""
from __future__ import annotations

import io
import json
import time

import numpy as np
from PIL import Image
from PIL.PngImagePlugin import PngInfo

from ..connection import make_client

_POLL_INTERVAL = 0.5  # seconds between import-status polls

# Substrings that identify the face-extraction-worker 400 error.
# PixlStash returns a "detail" field; we look for these in it.
_FACE_WORKER_HINTS = ("face extraction", "face worker", "worker not running")


class PixlStashPictureSaver:
    """Uploads images to PixlStash and assigns them to optional contexts.

    Returns the IDs of all newly imported pictures (status == "success")
    as a comma-separated string for use by downstream nodes.  Duplicate
    pictures that already exist in the vault are silently skipped in the
    output but are still processed for set/character/score assignment if
    ``all_ids`` is True.
    """

    CATEGORY = "PixlStash"
    RETURN_TYPES = ("STRING",)
    RETURN_NAMES = ("picture_ids",)
    OUTPUT_NODE = True
    FUNCTION = "save_pictures"

    @classmethod
    def INPUT_TYPES(cls):
        return {
            "required": {
                "images": ("IMAGE",),
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
                            "Embed the ComfyUI workflow and prompt JSON "
                            "into PNG tEXt chunks before uploading."
                        ),
                    },
                ),
            },
            "optional": {
                "pixlstash_project": (
                    "PIXLSTASH_PROJECT",
                    {
                        "forceInput": True,
                        "tooltip": "Assign pictures to this project at import time.",
                    },
                ),
                "pixlstash_set": (
                    "PIXLSTASH_SET",
                    {
                        "forceInput": True,
                        "tooltip": "Add pictures to this set after import.",
                    },
                ),
                "pixlstash_character": (
                    "PIXLSTASH_CHARACTER",
                    {
                        "forceInput": True,
                        "tooltip": "Assign pictures to this character after import.",
                    },
                ),
                # Injected at runtime by the JS queuePrompt interceptor from
                # ComfyUI Settings › PixlStash — never entered manually.
                "url": ("STRING", {"default": ""}),
                "token": ("STRING", {"default": ""}),
                "verify_ssl": ("BOOLEAN", {"default": True}),
            },
            "hidden": {
                "prompt": "PROMPT",
                "extra_pnginfo": "EXTRA_PNGINFO",
            },
        }

    # ------------------------------------------------------------------
    # Main function
    # ------------------------------------------------------------------

    def save_pictures(
        self,
        images,
        filename_prefix: str,
        save_workflow: bool,
        pixlstash_project: str = "",
        pixlstash_set: str = "",
        pixlstash_character: str = "",
        url: str = "",
        token: str = "",
        verify_ssl: bool = True,
        prompt=None,
        extra_pnginfo=None,
    ):
        if not url.strip() or not token.strip():
            raise RuntimeError(
                "PixlStash Picture Saver: URL and API Token are required. "
                "Configure them in ComfyUI Settings \u203a PixlStash."
            )
        client = make_client(url.strip(), token.strip(), verify_ssl)

        project_id   = pixlstash_project.strip()
        set_id       = pixlstash_set.strip()
        character_id = pixlstash_character.strip()

        # Encode each tensor to PNG bytes.
        files: list[tuple[str, bytes]] = []
        for idx in range(images.shape[0]):
            img_np = (
                images[idx].cpu().numpy() * 255.0
            ).clip(0, 255).astype(np.uint8)
            pil_img  = Image.fromarray(img_np, mode="RGB")
            filename = f"{filename_prefix}_{idx + 1:05d}.png"
            files.append(
                (
                    filename,
                    self._encode_png(
                        pil_img,
                        save_workflow=save_workflow,
                        prompt=prompt,
                        extra_pnginfo=extra_pnginfo,
                    ),
                )
            )

        new_ids, all_ids = self._upload(client, files, project_id=project_id)

        # Post-import assignments — applied to ALL ids (new + duplicates)
        # so that even re-imported images end up in the right context.
        if set_id and all_ids:
            client.post(
                f"/api/v1/picture_sets/{set_id}/members",
                is_write=True,
                json={"picture_ids": all_ids},
            )

        if character_id and all_ids:
            self._assign_character(client, character_id, all_ids)

        ids_str = ",".join(str(i) for i in new_ids)
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
        """Encode *pil_img* as PNG, optionally embedding workflow metadata."""
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
        client,
        files: list[tuple[str, bytes]],
        project_id: str = "",
    ) -> tuple[list[int], list[int]]:
        """Upload files via ``POST /pictures/import``, poll until done.

        Returns ``(new_ids, all_ids)`` where ``new_ids`` contains only
        entries with status "success" and ``all_ids`` includes duplicates.
        """
        multipart = [
            ("file", (name, data, "image/png")) for name, data in files
        ]
        form_data = {"project_id": project_id} if project_id else {}

        try:
            response = client.post(
                "/api/v1/pictures/import",
                is_write=True,
                files=multipart,
                data=form_data or None,
            )
        except RuntimeError as exc:
            msg = str(exc).lower()
            if any(hint in msg for hint in _FACE_WORKER_HINTS):
                raise RuntimeError(
                    "PixlStash: face extraction worker is not running. "
                    "Start it in PixlStash before importing."
                ) from exc
            raise

        task_id = response.json()["task_id"]

        while True:
            data = client.get(
                "/api/v1/pictures/import/status", params={"task_id": task_id}
            ).json()
            status = data.get("status")

            if status == "completed":
                results = data.get("results", [])
                new_ids = [
                    r["picture_id"]
                    for r in results
                    if r.get("status") == "success"
                ]
                all_ids = [r["picture_id"] for r in results]
                return new_ids, all_ids

            if status == "failed":
                raise RuntimeError(
                    f"PixlStash: import failed — {data.get('error', 'unknown error')}"
                )

            time.sleep(_POLL_INTERVAL)

    @staticmethod
    def _assign_character(
        client,
        character_id: str,
        picture_ids: list[int],
    ) -> None:
        """Associate pictures with a character via ``POST /characters/{id}/faces``.

        The server queues a pending face-extraction assignment for any
        picture whose face extraction hasn't run yet — no retry needed.
        HTTP 400 specifically means the face extraction worker is not
        running.
        """
        try:
            client.post(
                f"/api/v1/characters/{character_id}/faces",
                is_write=True,
                json={"picture_ids": picture_ids},
            )
        except RuntimeError as exc:
            msg = str(exc).lower()
            if any(hint in msg for hint in _FACE_WORKER_HINTS):
                raise RuntimeError(
                    "PixlStash: cannot assign character — the face "
                    "extraction worker is not running. "
                    "Start it in PixlStash and retry."
                ) from exc
            raise

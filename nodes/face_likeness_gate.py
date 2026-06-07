"""PixlStash Face Likeness Gate node.

Filters a batch of generated images by how closely each one resembles a
reference character's face.  Images that score at or above ``threshold``
are passed to the ``accepted`` output; the rest go to ``rejected``.  This
lets a workflow funnel only on-model renders into an upscale / save branch
while diverting the misses.

Face likeness is computed server-side, so the node:

1. Uploads every image to PixlStash via the async import endpoint
   (one file per request, polling each task to completion — the same
   mechanism the Picture Saver uses).
2. Waits for the face-extraction worker to finish embedding the freshly
   imported pictures, then reads their likeness to the reference character
   from ``POST /pictures/character_likeness/batch`` (one request per poll
   cycle for all pending ids; falls back to the per-id
   ``GET /pictures/{id}/character_likeness`` on an older backend).
3. Splits the original input frames into accepted / rejected batches.
4. Optionally deletes the scratch imports it created so the vault is not
   polluted (duplicates that already existed are never deleted).

Credentials are resolved server-side from ComfyUI Settings › PixlStash and
never injected into the prompt.
"""

from __future__ import annotations

import logging
import os
import time

import folder_paths
import numpy as np
import torch
from PIL import Image

from ..connection import make_client, read_credentials

log = logging.getLogger(__name__)

_POLL_INTERVAL = 0.5  # seconds between status / readiness polls

# Maximum picture ids per batched likeness request.  The common case (tens of
# pictures) is a single request; larger sets are split into chunks of this size
# so one poll cycle never sends an unbounded request body.
_LIKENESS_BATCH_CAP = 500

# Substrings that identify the face-extraction-worker error returned by
# the import endpoint when the worker is not running.
_FACE_WORKER_HINTS = ("face extraction", "face worker", "worker not running")


class PixlStashFaceLikenessGate:
    """Keeps only the generations that match a reference character's face.

    Returns two IMAGE batches (accepted / rejected) plus their counts and
    a pass-through of the reference character so an accepted branch can be
    saved back tagged to the same character without re-wiring.
    """

    CATEGORY = "PixlStash"
    RETURN_TYPES = ("IMAGE", "IMAGE", "INT", "INT", "PIXLSTASH_CHARACTER")
    RETURN_NAMES = (
        "accepted",
        "rejected",
        "accepted_count",
        "rejected_count",
        "pixlstash_character",
    )
    FUNCTION = "gate"
    OUTPUT_NODE = True

    @classmethod
    def INPUT_TYPES(cls):
        return {
            "required": {
                "image": (
                    "IMAGE",
                    {
                        "tooltip": (
                            "Batch of generated images to filter. Each frame "
                            "is scored individually against the reference "
                            "character."
                        ),
                    },
                ),
                "pixlstash_character": (
                    "PIXLSTASH_CHARACTER",
                    {
                        "forceInput": True,
                        "tooltip": (
                            "Wire from a Character Loader. Each input frame is "
                            "scored by face similarity to this character."
                        ),
                    },
                ),
                "threshold": (
                    "FLOAT",
                    {
                        "default": 0.7,
                        "min": 0.0,
                        "max": 1.0,
                        "step": 0.01,
                        "tooltip": (
                            "Minimum face-likeness score [0-1] a frame must "
                            "reach to be accepted. A frame with no detectable "
                            "face is always rejected."
                        ),
                    },
                ),
            },
            "optional": {
                "cleanup": (
                    "BOOLEAN",
                    {
                        "default": True,
                        "tooltip": (
                            "Delete the scratch pictures this node imports for "
                            "scoring once filtering is done, keeping the vault "
                            "clean. Pictures that already existed in the vault "
                            "(duplicates) are never deleted."
                        ),
                    },
                ),
                "face_timeout": (
                    "INT",
                    {
                        "default": 120,
                        "min": 5,
                        "max": 3600,
                        "tooltip": (
                            "Maximum seconds to wait for the face-extraction "
                            "worker to embed the imported frames. Frames still "
                            "unscored when this elapses are rejected."
                        ),
                    },
                ),
                "pixlstash_project": (
                    "PIXLSTASH_PROJECT",
                    {
                        "forceInput": True,
                        "tooltip": (
                            "Optional project to import the scratch pictures "
                            "into (useful when cleanup is off)."
                        ),
                    },
                ),
            },
        }

    # ------------------------------------------------------------------
    # Main function
    # ------------------------------------------------------------------

    def gate(
        self,
        image: torch.Tensor,
        pixlstash_character: str,
        threshold: float,
        cleanup: bool = True,
        face_timeout: int = 120,
        pixlstash_project: str = "",
        url: str = "",
        token: str = "",
        verify_ssl: bool = True,
    ):
        url, token, verify_ssl = read_credentials(url, token, verify_ssl)
        if not url or not token:
            raise RuntimeError(
                "PixlStash Face Likeness Gate: URL and API Token are required. "
                "Configure them in ComfyUI Settings › PixlStash."
            )

        character_id = pixlstash_character.strip()
        if not character_id:
            raise RuntimeError(
                "PixlStash Face Likeness Gate: a reference character is required. "
                "Wire a Character Loader into pixlstash_character."
            )

        client = make_client(url, token, verify_ssl)

        # Fail fast with a clear message if the character has no reference
        # faces — otherwise every frame would silently score 0 and reject.
        self._require_reference_faces(client, character_id)

        frame_count = int(image.shape[0])
        if frame_count == 0:
            raise RuntimeError(
                "PixlStash Face Likeness Gate: the input image batch is empty."
            )

        progress = self._make_progress(frame_count)

        # 1. Import every frame, remembering which picture id each maps to
        #    and whether we created it (new) or it already existed (dup).
        frame_pictures: list[int | None] = []
        new_ids: set[int] = set()
        project_id = pixlstash_project.strip()
        for idx in range(frame_count):
            pid, is_new = self._import_frame(client, image[idx], idx, project_id)
            frame_pictures.append(pid)
            if pid is not None and is_new:
                new_ids.add(pid)
            if progress is not None:
                progress.update(1)

        # 2. Wait for face embeddings and read each picture's likeness.
        unique_ids = {pid for pid in frame_pictures if pid is not None}
        scores = self._score_pictures(client, unique_ids, character_id, face_timeout)

        # 3. Split the original frames by threshold.  A frame whose import
        #    failed (no picture id) or that could not be scored in time is
        #    rejected — the safe default for a quality gate.
        accepted_idx: list[int] = []
        rejected_idx: list[int] = []
        for idx, pid in enumerate(frame_pictures):
            likeness, eligible = scores.get(pid, (0.0, False))
            if pid is not None and eligible and likeness >= threshold:
                accepted_idx.append(idx)
            else:
                rejected_idx.append(idx)

        accepted = self._select_frames(image, accepted_idx)
        rejected = self._select_frames(image, rejected_idx)

        log.info(
            "[PixlStash] Face Likeness Gate: %d accepted, %d rejected "
            "(threshold=%.2f, character_id=%s).",
            len(accepted_idx),
            len(rejected_idx),
            threshold,
            character_id,
        )

        # 4. Remove the scratch imports we created (never duplicates).
        if cleanup and new_ids:
            self._cleanup(client, new_ids)

        ui_images = self._write_previews(accepted, prefix="accepted")

        return {
            "ui": {
                "images": ui_images,
                "accepted_count": [str(len(accepted_idx))],
                "rejected_count": [str(len(rejected_idx))],
            },
            "result": (
                self._image_output(accepted),
                self._image_output(rejected),
                len(accepted_idx),
                len(rejected_idx),
                character_id,
            ),
        }

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _make_progress(total: int):
        """Return a ComfyUI ProgressBar if available, else None."""
        try:
            from comfy.utils import ProgressBar  # noqa: PLC0415

            return ProgressBar(total)
        except Exception:
            return None

    @staticmethod
    def _require_reference_faces(client, character_id: str) -> None:
        """Raise if the character has no embedded reference faces to score against."""
        try:
            data = client.get(
                f"/api/v1/characters/{character_id}/reference_pictures"
            ).json()
        except RuntimeError as exc:
            # Older servers may lack this endpoint; don't block on it.
            log.warning("[PixlStash] reference_pictures check skipped — %s", exc)
            return
        ref_ids = data.get("reference_picture_ids") if isinstance(data, dict) else data
        if not ref_ids:
            raise RuntimeError(
                f"PixlStash Face Likeness Gate: character {character_id} has no "
                "reference faces, so nothing can be scored against it. Assign "
                "reference faces to the character in PixlStash first."
            )

    def _import_frame(
        self,
        client,
        frame: torch.Tensor,
        idx: int,
        project_id: str,
    ) -> tuple[int | None, bool]:
        """Import one frame and return (picture_id, is_new).

        Returns ``(None, False)`` when the import produced no picture id
        (e.g. the file was skipped).  ``is_new`` is True only for pictures
        this call actually created (status "success"), so cleanup never
        touches pre-existing duplicates.
        """
        png_bytes = self._frame_to_png_bytes(frame)
        form_data = {"project_id": project_id} if project_id else None
        try:
            response = client.post(
                "/api/v1/pictures/import",
                is_write=True,
                files=[("file", (f"gate_{idx:05d}.png", png_bytes, "image/png"))],
                data=form_data,
            )
        except RuntimeError as exc:
            if any(hint in str(exc).lower() for hint in _FACE_WORKER_HINTS):
                raise RuntimeError(
                    "PixlStash Face Likeness Gate: the face-extraction worker is "
                    "not running. Start it in PixlStash before filtering."
                ) from exc
            raise

        task_id = response.json()["task_id"]

        deadline = time.time() + 300
        while True:
            status_data = client.get(
                "/api/v1/pictures/import/status", params={"task_id": task_id}
            ).json()
            status = status_data.get("status")
            if status == "completed":
                for result in status_data.get("results", []):
                    pid = result.get("picture_id")
                    if pid is None:
                        continue
                    return int(pid), result.get("status") == "success"
                return None, False
            if status == "failed":
                raise RuntimeError(
                    f"PixlStash Face Likeness Gate: import failed — "
                    f"{status_data.get('error', 'unknown error')}"
                )
            if time.time() > deadline:
                raise RuntimeError(
                    "PixlStash Face Likeness Gate: import timed out waiting for the "
                    f"server (task_id={task_id})."
                )
            time.sleep(_POLL_INTERVAL)

    def _score_pictures(
        self,
        client,
        picture_ids: set[int],
        character_id: str,
        face_timeout: int,
    ) -> dict[int, tuple[float, bool]]:
        """Poll the still-pending pictures until each face embedding is ready.

        Maps ``picture_id -> (character_likeness, eligible)``.

        Each poll cycle reads every still-pending picture in a single batched
        request (``_read_likeness_batch``) instead of one GET per picture, so a
        cycle is one request regardless of how many frames are in flight.

        Readiness comes from the explicit ``ready`` flag on the likeness
        endpoint.  While ``ready`` is ``False`` the face-extraction worker has
        not finished, so the score is provisional and the picture is polled
        again.  Once ``ready`` is ``True`` the ``character_likeness`` value is
        final and is recorded as-is, even when it is ``0.0`` or null (a
        genuinely low score is no longer mistaken for "still extracting").  Any
        picture still not ready when ``face_timeout`` elapses is treated as a
        non-match (0.0, not eligible).

        An older backend without the batch endpoint (HTTP 404/405) makes the
        first batched call fail; the cycle then falls back to the per-id
        ``_read_likeness`` GET path for the rest of the run.
        """
        scores: dict[int, tuple[float, bool]] = {}
        pending = set(picture_ids)
        progress = self._make_progress(len(pending)) if pending else None
        deadline = time.time() + max(face_timeout, _POLL_INTERVAL)
        # Flipped to True the first time the batch endpoint is missing, after
        # which every cycle uses the per-id path for backward compatibility.
        use_per_id = False

        while pending and time.time() < deadline:
            if use_per_id:
                results = {
                    pid: self._read_likeness(client, pid, character_id)
                    for pid in list(pending)
                }
            else:
                try:
                    results = self._read_likeness_batch(
                        client, list(pending), character_id
                    )
                except RuntimeError as exc:
                    msg = str(exc).lower()
                    if "not found" in msg or "http 405" in msg:
                        # Batch endpoint absent on this backend — degrade to
                        # the per-id path for the remainder of the run.
                        use_per_id = True
                        continue
                    raise

            for pid, (likeness, eligible, ready) in results.items():
                # Poll again only while extraction is still pending.
                if not ready:
                    continue
                # Ready: the score is final, accept 0.0/null as-is.
                scores[pid] = (likeness or 0.0, eligible)
                pending.discard(pid)
                if progress is not None:
                    progress.update(1)
            if pending:
                time.sleep(_POLL_INTERVAL)

        if pending:
            log.warning(
                "[PixlStash] Face Likeness Gate: %d picture(s) not scored within "
                "%ds — rejecting them: %s",
                len(pending),
                face_timeout,
                sorted(pending),
            )
            for pid in pending:
                scores[pid] = (0.0, False)
        return scores

    @staticmethod
    def _read_likeness(
        client,
        picture_id: int,
        character_id: str,
    ) -> tuple[float | None, bool, bool]:
        """Return (character_likeness, eligible, ready) for one picture.

        ``ready`` defaults to ``True`` when the key is absent so an older
        backend that does not send it stops polling instead of hanging until
        ``face_timeout`` (under-poll rather than spin forever).
        """
        data = client.get(
            f"/api/v1/pictures/{picture_id}/character_likeness",
            params={"reference_character_id": character_id},
        ).json()
        return (
            data.get("character_likeness"),
            bool(data.get("eligible")),
            bool(data.get("ready", True)),
        )

    @staticmethod
    def _read_likeness_batch(
        client,
        picture_ids: list[int],
        character_id: str,
    ) -> dict[int, tuple[float | None, bool, bool]]:
        """Read likeness for many pictures in one (or a few) POST request(s).

        POSTs ``{"reference_character_id": character_id, "picture_ids": [...]}``
        to ``/pictures/character_likeness/batch`` and returns
        ``{picture_id: (character_likeness, eligible, ready)}`` parsed from the
        ``results`` array.  Per-id semantics match ``_read_likeness``: ``ready``
        defaults to ``True`` when absent so a result is never polled forever.

        ``picture_ids`` is split into chunks of ``_LIKENESS_BATCH_CAP`` so a
        single cycle never sends an unbounded body; the common case (tens of
        pictures) is one request.  Raises ``RuntimeError`` (incl. HTTP 404/405)
        straight through so the caller can fall back to the per-id path.
        """
        results: dict[int, tuple[float | None, bool, bool]] = {}
        for start in range(0, len(picture_ids), _LIKENESS_BATCH_CAP):
            chunk = picture_ids[start : start + _LIKENESS_BATCH_CAP]
            data = client.post(
                "/api/v1/pictures/character_likeness/batch",
                json={
                    "reference_character_id": character_id,
                    "picture_ids": chunk,
                },
            ).json()
            for item in data.get("results", []):
                pid = item.get("picture_id")
                if pid is None:
                    continue
                results[int(pid)] = (
                    item.get("character_likeness"),
                    bool(item.get("eligible")),
                    bool(item.get("ready", True)),
                )
        return results

    def _cleanup(self, client, picture_ids: set[int]) -> None:
        """Soft-delete the scratch pictures this node created."""
        failed: list[int] = []
        for pid in picture_ids:
            try:
                client.delete(f"/api/v1/pictures/{pid}", is_write=True)
            except RuntimeError as exc:
                log.warning(
                    "[PixlStash] Could not delete scratch picture %s — %s", pid, exc
                )
                failed.append(pid)
        if failed:
            log.warning(
                "[PixlStash] Face Likeness Gate: %d scratch picture(s) left in the "
                "vault: %s",
                len(failed),
                sorted(failed),
            )

    @staticmethod
    def _frame_to_png_bytes(frame: torch.Tensor) -> bytes:
        """Encode a single [H,W,3] float32 tensor in [0,1] to PNG bytes."""
        import io  # noqa: PLC0415

        arr = (frame.cpu().numpy() * 255.0).clip(0, 255).astype(np.uint8)
        pil_img = Image.fromarray(arr, mode="RGB")
        buf = io.BytesIO()
        pil_img.save(buf, format="PNG", compress_level=4)
        return buf.getvalue()

    @staticmethod
    def _select_frames(image: torch.Tensor, indices: list[int]) -> torch.Tensor:
        """Return the chosen frames as a batch, or an empty [0,H,W,C] batch."""
        if indices:
            return image[torch.tensor(indices, dtype=torch.long)]
        _, h, w, c = image.shape
        return torch.zeros((0, h, w, c), dtype=image.dtype)

    @staticmethod
    def _image_output(batch: torch.Tensor):
        """Wrap an image batch for output, blocking an empty stream.

        ComfyUI's built-in Preview/Save Image read ``images[0]``, so a
        zero-length ``[0,H,W,C]`` batch makes them raise ``IndexError: index 0
        is out of bounds for dimension 0 with size 0``.  When a gate stream is
        empty we return an ``ExecutionBlocker`` instead, which silently skips
        the downstream branch wired to that output (the INT count outputs still
        carry real values).  Falls back to the empty batch on ComfyUI builds
        without the blocker.
        """
        if batch.shape[0] > 0:
            return batch
        try:
            from comfy_execution.graph_utils import ExecutionBlocker  # noqa: PLC0415
        except Exception:
            return batch
        return ExecutionBlocker(None)

    @staticmethod
    def _write_previews(images: torch.Tensor, prefix: str) -> list[dict]:
        """Write the batch to ComfyUI's temp dir and return preview descriptors."""
        import io  # noqa: PLC0415

        previews: list[dict] = []
        if images.shape[0] == 0:
            return previews
        temp_dir = folder_paths.get_temp_directory()
        os.makedirs(temp_dir, exist_ok=True)
        for idx in range(images.shape[0]):
            arr = (images[idx].cpu().numpy() * 255.0).clip(0, 255).astype(np.uint8)
            filename = f"pixlstash_gate_{prefix}_{idx:05d}.png"
            with open(os.path.join(temp_dir, filename), "wb") as fh:
                buf = io.BytesIO()
                Image.fromarray(arr, mode="RGB").save(buf, format="PNG")
                fh.write(buf.getvalue())
            previews.append({"filename": filename, "subfolder": "", "type": "temp"})
        return previews

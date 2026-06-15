"""PixlStash Face Likeness Gate node.

Filters a batch of generated images by how closely each one resembles a
reference character's face.  Images that score at or above ``threshold``
are passed to the ``accepted`` output; the rest go to ``rejected``.  This
lets a workflow funnel only on-model renders into an upscale / save branch
while diverting the misses.

Face likeness is computed server-side by a single stateless endpoint
(``POST /pictures/score_character_likeness``): the node uploads the frames in
batches, the server detects faces in-memory on the GPU and scores each frame
against the reference character's reference faces, and returns one score per
frame.  Nothing is imported or persisted — no scratch pictures, no tagging /
captioning / embedding, and no vault-wide likeness work — so scoring is fast
and leaves the vault untouched (there is nothing to clean up).

Credentials are resolved server-side from ComfyUI Settings › PixlStash and
never injected into the prompt.
"""

from __future__ import annotations

import logging
import os

import folder_paths
import numpy as np
import torch
from PIL import Image

from ..connection import make_client, read_credentials
from .likeness_search import COMBINE_MODES

log = logging.getLogger(__name__)

# Frames uploaded per scoring request.  Detection is batched on the GPU, so a
# few requests cover a typical gate batch while keeping each request's body (and
# wall time) comfortably under the HTTP client's fixed timeout.
_SCORE_BATCH_SIZE = 16

# Endpoint added in PixlStash 1.6.0.  Shown when an older backend 404s the call.
_MIN_SERVER_HINT = "1.6.0"


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
                "combine": (
                    COMBINE_MODES,
                    {
                        "default": "mean",
                        "tooltip": (
                            "How to combine a frame's similarity across the "
                            "character's reference faces. mean: average; max: "
                            "best matching reference; min: must match every "
                            "reference; harmonic_mean / geometric_mean: balance "
                            "between extremes."
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
        combine: str = "mean",
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

        # Score every frame in batched, stateless requests. Maps the global
        # frame index -> (likeness, eligible). Frames the server could not score
        # default to a non-match (the safe default for a quality gate).
        scores = self._score_frames(client, image, character_id, combine)

        accepted_idx: list[int] = []
        rejected_idx: list[int] = []
        for idx in range(frame_count):
            likeness, eligible = scores.get(idx, (None, False))
            if eligible and likeness is not None and likeness >= threshold:
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

    def _score_frames(
        self,
        client,
        image: torch.Tensor,
        character_id: str,
        combine: str = "mean",
    ) -> dict[int, tuple[float | None, bool]]:
        """Score every frame against the reference character in batched requests.

        Uploads the frames in chunks of ``_SCORE_BATCH_SIZE`` to the stateless
        ``/pictures/score_character_likeness`` endpoint and returns a map of
        ``frame_index -> (character_likeness, eligible)``.  Results are keyed by
        the per-request ``index`` the server echoes back (offset by the chunk
        start), so the mapping is robust to result ordering.
        """
        frame_count = int(image.shape[0])
        scores: dict[int, tuple[float | None, bool]] = {}
        progress = self._make_progress(frame_count)

        for start in range(0, frame_count, _SCORE_BATCH_SIZE):
            end = min(start + _SCORE_BATCH_SIZE, frame_count)
            files = [
                (
                    "files",
                    (
                        f"gate_{idx:05d}.jpg",
                        self._frame_to_jpeg_bytes(image[idx]),
                        "image/jpeg",
                    ),
                )
                for idx in range(start, end)
            ]
            try:
                response = client.post(
                    "/api/v1/pictures/score_character_likeness",
                    files=files,
                    data={
                        "reference_character_id": character_id,
                        "combine": combine,
                    },
                )
            except RuntimeError as exc:
                if "not found" in str(exc).lower():
                    raise RuntimeError(
                        "PixlStash Face Likeness Gate: this PixlStash server is too "
                        f"old — the scoring endpoint needs PixlStash {_MIN_SERVER_HINT} "
                        "or newer. Update PixlStash."
                    ) from exc
                raise

            for item in response.json().get("results", []):
                rel_index = item.get("index")
                if rel_index is None:
                    continue
                frame_idx = start + int(rel_index)
                scores[frame_idx] = (
                    item.get("character_likeness"),
                    bool(item.get("eligible")),
                )

            if progress is not None:
                progress.update(end - start)

        return scores

    @staticmethod
    def _frame_to_jpeg_bytes(frame: torch.Tensor) -> bytes:
        """Encode a single [H,W,3] float32 tensor in [0,1] to JPEG bytes.

        JPEG (quality 95) keeps the upload small and fast — these frames are
        only scored, never stored, and the ArcFace embedding shift at q95 is far
        below any sensible likeness threshold.
        """
        import io  # noqa: PLC0415

        arr = (frame.cpu().numpy() * 255.0).clip(0, 255).astype(np.uint8)
        pil_img = Image.fromarray(arr, mode="RGB")
        buf = io.BytesIO()
        pil_img.save(buf, format="JPEG", quality=95)
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

"""PixlStash Picture Likeness Gate node.

The whole-image twin of the (face-based) Likeness Gate.  It splits a batch of
generated images into an **accepted** output and a **rejected** output by
comparing each frame's whole-image likeness against a reference picture *set*
— so on-target renders flow to upscaling while the rest are diverted.

Every frame is scored against every member of the set; the per-member cosine
similarities are reduced with the chosen ``combine`` mode (default ``min`` —
"must match all") and a frame is accepted when that combined score reaches the
threshold.  A reference set of [monkey, banana, bicycle] with ``min`` keeps
only the frames that resemble all three.

How the scoring avoids async embeddings
----------------------------------------
Each candidate frame is sent as the *query* image to
``POST /api/v1/pictures/likeness-search`` with ``set_id`` restricting the
ranked corpus to the reference set's members.  The server computes the query
image's CLIP embedding **synchronously in-request** and ranks it against the
set members, whose embeddings already exist.  So — unlike the face gate —
nothing is uploaded to the vault, nothing is persisted, and there is no import
step and no embedding-readiness polling: the node is side-effect free.

Cost is one request per candidate frame (the API offers no batched
candidate-pool scoring), bounded by the server-side ``top_n`` cap of 500
members per set.

Credentials are resolved server-side from ComfyUI Settings › PixlStash and
never injected into the prompt.
"""

from __future__ import annotations

import io
import logging

import numpy as np
import torch
from PIL import Image

from ..connection import make_client, read_credentials

log = logging.getLogger(__name__)

# combine modes reduce a frame's per-reference scores into one number.
# "min" (default) = the frame must clear the threshold against EVERY member.
COMBINE_MODES = ["min", "mean", "max", "harmonic_mean", "geometric_mean"]

_LIKENESS_ENDPOINT = "/api/v1/pictures/likeness-search"
_MAX_TOP_N = 500  # server-side cap on likeness-search top_n


def _combine(scores: list[float], mode: str) -> float:
    """Reduce a frame's per-reference similarity scores to a single value.

    Missing members (a reference the frame is uncorrelated / anti-correlated
    with, hence absent from the threshold-0 results) are passed in as 0.0 by
    the caller so ``min`` and the means treat them as a non-match.
    """
    if not scores:
        return 0.0
    if mode == "max":
        return max(scores)
    if mode == "mean":
        return sum(scores) / len(scores)
    if mode == "geometric_mean":
        clipped = [max(s, 0.0) for s in scores]
        prod = 1.0
        for s in clipped:
            prod *= s
        return prod ** (1.0 / len(clipped))
    if mode == "harmonic_mean":
        if any(s <= 0.0 for s in scores):
            return 0.0
        return len(scores) / sum(1.0 / s for s in scores)
    # default / "min"
    return min(scores)


class PixlStashPictureLikenessGate:
    """Keeps only the generations that match a reference picture set.

    Returns two IMAGE batches (accepted / rejected) plus their counts and a
    pass-through of the reference set so an accepted branch can be saved back
    into the same set without re-wiring.
    """

    CATEGORY = "PixlStash"
    RETURN_TYPES = ("IMAGE", "IMAGE", "INT", "INT", "PIXLSTASH_SET")
    RETURN_NAMES = (
        "accepted",
        "rejected",
        "accepted_count",
        "rejected_count",
        "pixlstash_set",
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
                            "The batch of generations to judge. Each frame is "
                            "scored individually and routed to either the "
                            "accepted or the rejected output."
                        ),
                    },
                ),
                "pixlstash_set": (
                    "PIXLSTASH_SET",
                    {
                        "forceInput": True,
                        "tooltip": (
                            "Wire from a Set Loader. Every generation is scored "
                            "against this set's pictures — the bar each frame has "
                            "to clear to be accepted."
                        ),
                    },
                ),
                "combine": (
                    COMBINE_MODES,
                    {
                        "default": "min",
                        "tooltip": (
                            "How to combine a frame's per-reference scores. "
                            "min: must match every picture in the set (a "
                            "[monkey, banana, bicycle] set keeps only frames "
                            "showing all three). max: match any one. mean / "
                            "harmonic_mean / geometric_mean: balance between."
                        ),
                    },
                ),
                "threshold": (
                    "FLOAT",
                    {
                        "default": 0.25,
                        "min": 0.0,
                        "max": 1.0,
                        "step": 0.01,
                        "tooltip": (
                            "Minimum combined likeness score [0–1] a frame must "
                            "reach to be accepted. Read the scores logged after a "
                            "run (min / median / max) to set the cut between the "
                            "off-target and on-target frames."
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
        pixlstash_set: str,
        combine: str,
        threshold: float,
        url: str = "",
        token: str = "",
        verify_ssl: bool = True,
    ):
        url, token, verify_ssl = read_credentials(url, token, verify_ssl)
        if not url or not token:
            raise RuntimeError(
                "PixlStash Picture Likeness Gate: URL and API Token are required. "
                "Configure them in ComfyUI Settings › PixlStash."
            )

        set_id = (pixlstash_set or "").strip()
        if not set_id:
            raise RuntimeError(
                "PixlStash Picture Likeness Gate: a reference set is required. "
                "Wire a Set Loader into pixlstash_set so there's something to "
                "compare against."
            )

        client = make_client(url, token, verify_ssl)

        # Total member count drives top_n and the not-indexed diagnostic.
        n_total = self._set_member_count(client, set_id)
        if n_total == 0:
            raise RuntimeError(
                "PixlStash Picture Likeness Gate: the reference set is empty — "
                "add pictures to it in PixlStash, or pick a different set."
            )
        if n_total > _MAX_TOP_N:
            raise RuntimeError(
                f"PixlStash Picture Likeness Gate: the reference set has "
                f"{n_total} members, but the server compares against at most "
                f"{_MAX_TOP_N} per frame. Use a smaller reference set so every "
                "member can be scored."
            )

        frame_count = int(image.shape[0])
        if frame_count == 0:
            raise RuntimeError(
                "PixlStash Picture Likeness Gate: the input image batch is empty."
            )

        progress = self._make_progress(frame_count)

        # 1. Score every candidate against the set.  per_candidate[i] maps
        #    reference picture_id -> likeness for frame i.
        per_candidate: list[dict[int, float]] = []
        member_union: set[int] = set()
        for idx in range(frame_count):
            png_bytes = self._frame_to_png_bytes(image[idx])
            scored = self._score_frame(client, png_bytes, set_id, n_total)
            per_candidate.append(scored)
            member_union.update(scored.keys())
            if progress is not None:
                progress.update(1)

        effective_n = len(member_union)
        if effective_n == 0:
            raise RuntimeError(
                "PixlStash Picture Likeness Gate: none of the reference set's "
                "pictures have a computed embedding yet. Let PixlStash finish "
                "indexing the set and run again."
            )
        if effective_n < n_total:
            log.warning(
                "[PixlStash] Picture Likeness Gate: %d of %d reference pictures "
                "are not yet indexed (no embedding) and were skipped.",
                n_total - effective_n,
                n_total,
            )

        # 2. Combine scores and split.  Members absent from a frame's results
        #    scored below 0 there, so pad them with 0.0 before combining.
        members = sorted(member_union)
        accepted_idx: list[int] = []
        rejected_idx: list[int] = []
        frame_scores: list[float] = []
        for idx, scored in enumerate(per_candidate):
            combined = _combine([scored.get(mid, 0.0) for mid in members], combine)
            frame_scores.append(combined)
            (accepted_idx if combined >= threshold else rejected_idx).append(idx)

        accepted = self._select_frames(image, accepted_idx)
        rejected = self._select_frames(image, rejected_idx)

        self._log_summary(
            accepted_idx, rejected_idx, frame_scores, combine, threshold, effective_n
        )

        ui_images = self._write_previews(accepted, prefix="accepted")
        score_summary = self._score_summary(frame_scores)

        return {
            "ui": {
                "images": ui_images,
                "accepted_count": [str(len(accepted_idx))],
                "rejected_count": [str(len(rejected_idx))],
                "scores": [score_summary],
            },
            "result": (
                accepted,
                rejected,
                len(accepted_idx),
                len(rejected_idx),
                set_id,
            ),
        }

    # ------------------------------------------------------------------
    # Scoring helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _score_frame(
        client,
        png_bytes: bytes,
        set_id: str,
        top_n: int,
    ) -> dict[int, float]:
        """Return {reference_picture_id: likeness} for one candidate frame.

        Sends the frame as the likeness-search query image with ``threshold=0``
        so every embedded set member scoring at least 0 comes back with its
        cosine similarity.
        """
        files = [("files", ("query.png", png_bytes, "image/png"))]
        params = {
            "set_id": set_id,
            "top_n": top_n,
            "threshold": 0.0,
            "random": "false",
        }
        results = client.post(
            _LIKENESS_ENDPOINT,
            params=params,
            files=files,
        ).json()

        if not isinstance(results, list):
            raise RuntimeError(
                "PixlStash Picture Likeness Gate: unexpected likeness-search "
                f"response format: {type(results)}"
            )

        scored: dict[int, float] = {}
        for item in results:
            if not isinstance(item, dict):
                continue
            pid = item.get("picture_id", item.get("id"))
            sim = item.get("likeness", item.get("score"))
            if pid is None or sim is None:
                continue
            scored[int(pid)] = float(sim)
        return scored

    @staticmethod
    def _set_member_count(client, set_id: str) -> int:
        """Count the pictures belonging to a set.

        Prefers ``GET /picture_sets/{id}/members``; falls back to a filtered
        ``GET /pictures`` listing if that endpoint is unavailable.
        """
        try:
            data = client.get(f"/api/v1/picture_sets/{set_id}/members").json()
            if isinstance(data, dict) and isinstance(data.get("picture_ids"), list):
                return len(data["picture_ids"])
        except RuntimeError as exc:
            log.warning(
                "[PixlStash] set members endpoint failed (%s); "
                "falling back to pictures listing.",
                exc,
            )

        pics = client.get(
            "/api/v1/pictures",
            params={"set_id": set_id, "fields": "grid"},
        ).json()
        return len(pics) if isinstance(pics, list) else 0

    # ------------------------------------------------------------------
    # Batch / tensor helpers
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
    def _frame_to_png_bytes(frame: torch.Tensor) -> bytes:
        """Encode a single [H,W,3] float32 tensor in [0,1] to PNG bytes."""
        arr = (frame.cpu().numpy() * 255.0).clip(0, 255).astype(np.uint8)
        pil_img = Image.fromarray(arr, mode="RGB")
        buf = io.BytesIO()
        pil_img.save(buf, format="PNG")
        return buf.getvalue()

    @staticmethod
    def _select_frames(image: torch.Tensor, indices: list[int]) -> torch.Tensor:
        """Return the chosen frames as a batch, or an empty [0,H,W,C] batch."""
        if indices:
            return image[torch.tensor(indices, dtype=torch.long)]
        _, h, w, c = image.shape
        return torch.zeros((0, h, w, c), dtype=image.dtype)

    @staticmethod
    def _write_previews(images: torch.Tensor, prefix: str) -> list[dict]:
        """Write the batch to ComfyUI's temp dir and return preview descriptors."""
        import os  # noqa: PLC0415

        import folder_paths  # noqa: PLC0415

        previews: list[dict] = []
        if images.shape[0] == 0:
            return previews
        temp_dir = folder_paths.get_temp_directory()
        os.makedirs(temp_dir, exist_ok=True)
        for idx in range(images.shape[0]):
            arr = (images[idx].cpu().numpy() * 255.0).clip(0, 255).astype(np.uint8)
            filename = f"pixlstash_picgate_{prefix}_{idx:05d}.png"
            with open(os.path.join(temp_dir, filename), "wb") as fh:
                buf = io.BytesIO()
                Image.fromarray(arr, mode="RGB").save(buf, format="PNG")
                fh.write(buf.getvalue())
            previews.append({"filename": filename, "subfolder": "", "type": "temp"})
        return previews

    # ------------------------------------------------------------------
    # Reporting
    # ------------------------------------------------------------------

    @staticmethod
    def _score_summary(frame_scores: list[float]) -> str:
        """A compact 'min X · median Y · max Z' string for threshold tuning."""
        if not frame_scores:
            return "no frames"
        ordered = sorted(frame_scores)
        return (
            f"min {ordered[0]:.3f} · "
            f"median {ordered[len(ordered) // 2]:.3f} · "
            f"max {ordered[-1]:.3f}"
        )

    def _log_summary(
        self,
        accepted_idx: list[int],
        rejected_idx: list[int],
        frame_scores: list[float],
        combine: str,
        threshold: float,
        effective_n: int,
    ) -> None:
        log.info(
            "[PixlStash] Picture Likeness Gate: %d accepted / %d rejected of %d "
            "(combine=%s, threshold=%.2f, references=%d). scores: %s",
            len(accepted_idx),
            len(rejected_idx),
            len(accepted_idx) + len(rejected_idx),
            combine,
            threshold,
            effective_n,
            self._score_summary(frame_scores),
        )

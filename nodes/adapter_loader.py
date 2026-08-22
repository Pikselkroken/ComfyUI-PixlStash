"""PixlStash Adapter Loader node.

Applies one adapter (LoRA / LoKr / …) from the PixlStash model shelf to a
MODEL, resolving or fetching the file first.

Shaped like ComfyUI's built-in ``LoraLoader`` on purpose. An earlier version
emitted the file *path* and left the applying to another node; that could not
be wired to anything, because every LoRA loader in the ecosystem takes a name
off a combo widget and a combo input will not accept a STRING link. A loader
that cannot be connected to a loader has to be one.

The node itself filters nothing: the ``adapter_kind`` and ``base_model``
widgets exist only so the JS Browse modal (``web/js/adapter_picker.js``) can
narrow the grid, and the ``pixlstash_set`` / ``pixlstash_character`` wires are
read there too.  **Character wins when both are wired** — ``GET /adapters``
rejects being given ``character_id`` and ``set_id`` together — and that rule
lives in the picker, next to the request that would 400.  All Python gets is
one ``adapter_sha256``.

Getting the file onto this machine is ``shelf_file.resolve`` — used in place
when PixlStash shares this filesystem, fetched into ``<loras>/pixlstash/`` and
verified against its digest when it does not.  It is shared with the VAE, CLIP
and checkpoint loaders.

No ``IS_CHANGED``: the node is cached on its inputs like every other loader in
this package.  A NaN would re-resolve on every queue, but at the price of
invalidating every node downstream of it — a KSampler re-running on each press
of Queue is a far worse bug than the one it would fix (a file moved on disk
mid-session, which a restart or any graph edit already clears).
"""

from __future__ import annotations

import json
import logging

from . import shelf_file
from .adapter_applier import AdapterApplier

log = logging.getLogger(__name__)

# The algorithms ``pixlstash/utils/adapter_header.py`` can detect from a file's
# tensor markers.  There is no ``lycoris`` literal — LyCORIS formats are
# recorded as ``lokr`` / ``loha``.
ANY_KIND = "— Any —"
ADAPTER_KINDS = [ANY_KIND, "lora", "lokr", "loha", "oft", "dora", "unknown"]

LABEL = "PixlStash Adapter Loader"


def _trigger_words(record: dict) -> str:
    """``trigger_words`` as text you can paste into a prompt.

    The field is declared ``Optional[str]`` on the server but what it actually
    holds is a JSON array — every adapter on the shelf this was checked against
    stores ``'["Clementine"]'`` rather than ``Clementine``. Returning that
    verbatim puts brackets and quotes into the prompt, so a string that parses
    as a JSON list is unwrapped and joined.

    Anything else is passed through: a plain ``knight, plate armour`` is already
    what we want, and a bare word is not JSON at all. Only a *list* is unwrapped
    — ``json.loads`` would also happily turn the trigger word ``"1girl"`` into
    the number 1.
    """
    value = record.get("trigger_words")
    if isinstance(value, str):
        text = value.strip()
        if text.startswith("["):
            try:
                decoded = json.loads(text)
            except ValueError:
                decoded = None
            if isinstance(decoded, list):
                value = decoded
    if isinstance(value, (list, tuple)):
        return ", ".join(str(v).strip() for v in value if str(v).strip())
    return str(value).strip() if value else ""


class PixlStashAdapterLoader:
    """A LoRA loader whose file comes off the PixlStash shelf.

    Same shape as ComfyUI's built-in ``LoraLoader`` — MODEL and CLIP in, MODEL
    and CLIP out, two strengths — with the ``lora_name`` dropdown replaced by
    the Browse grid, and the file resolved (or fetched) from the shelf instead
    of read out of a local folder.
    """

    CATEGORY = "PixlStash"
    # Deliberately the built-in LoraLoader's signature: MODEL + CLIP in, MODEL
    # + CLIP out. Emitting a path instead was the original design and it did
    # not work — a path connects to nothing, because every LoRA loader in the
    # ecosystem takes a NAME off a combo widget, and a combo input will not
    # accept a STRING link. The only way to be usable is to be the loader.
    RETURN_TYPES = ("MODEL", "CLIP", "STRING")
    RETURN_NAMES = ("model", "clip", "trigger_words")
    FUNCTION = "load_lora"

    DESCRIPTION = (
        "Applies a LoRA from your PixlStash model shelf to a model, chosen by "
        "browsing a grid of them instead of hunting for a filename in a "
        "dropdown of hundreds.\n\n"
        "Drop-in for the built-in LoRA loader: same model / clip inputs, same "
        "two strengths, same two outputs, chain as many as you like. Click "
        "“Browse adapters…” to pick one. Wire a Character Loader or Set Loader "
        "in to see only that person's or that set's adapters.\n\n"
        "The file is used where it lies when PixlStash is on this machine, and "
        "otherwise fetched once and cached under your loras folder, verified "
        "against its SHA-256 before anything is written. LoKr, LoHa, OFT and "
        "DoRA work here too."
    )
    OUTPUT_TOOLTIPS = (
        "The model with the adapter applied. Chain into another of these for a "
        "second adapter.",
        "The CLIP with the adapter applied — carries nothing if you left the "
        "clip input unwired, so leave this unwired too in that case.",
        "The trigger words recorded on the shelf, comma-separated. Wire into a "
        "text encode. Empty when the shelf has none for this adapter.",
    )

    def __init__(self) -> None:
        self._applier = AdapterApplier()

    @classmethod
    def INPUT_TYPES(cls):
        strength = {
            "default": 1.0,
            "min": -100.0,
            "max": 100.0,
            "step": 0.01,
        }
        return {
            "required": {
                "model": (
                    "MODEL",
                    {"tooltip": "The diffusion model the adapter is applied to."},
                ),
                "adapter_kind": (
                    ADAPTER_KINDS,
                    {
                        "default": ANY_KIND,
                        "tooltip": (
                            "Narrows the Browse grid to one adapter algorithm. "
                            "Affects the grid only, not what is loaded."
                        ),
                    },
                ),
                "base_model": (
                    ["(loading…)"],
                    {
                        "tooltip": (
                            "Narrows the Browse grid to adapters trained against "
                            "one base model. Populated live from your shelf; "
                            "affects the grid only, not what is loaded."
                        ),
                    },
                ),
                "adapter_sha256": (
                    "STRING",
                    {
                        "default": "",
                        "multiline": False,
                        "tooltip": (
                            "SHA-256 of the chosen adapter. Written by the "
                            "Browse button — you don't normally type here."
                        ),
                    },
                ),
            },
            "optional": {
                "clip": (
                    "CLIP",
                    {
                        "tooltip": (
                            "Optional. Leave unwired for a model-only adapter — "
                            "but then leave the CLIP output unwired too, as it "
                            "has nothing to carry."
                        )
                    },
                ),
                "strength_model": (
                    "FLOAT",
                    {
                        **strength,
                        "tooltip": (
                            "How strongly to patch the model. Can be negative."
                        ),
                    },
                ),
                "strength_clip": (
                    "FLOAT",
                    {
                        **strength,
                        "tooltip": (
                            "How strongly to patch CLIP. Ignored with no clip "
                            "wired. Can be negative."
                        ),
                    },
                ),
                "pixlstash_set": (
                    "PIXLSTASH_SET",
                    {
                        "forceInput": True,
                        "tooltip": "Wire from a Set Loader to list only that set's adapters.",
                    },
                ),
                "pixlstash_character": (
                    "PIXLSTASH_CHARACTER",
                    {
                        "forceInput": True,
                        "tooltip": (
                            "Wire from a Character Loader to list only that "
                            "character's adapters. Takes precedence over a set."
                        ),
                    },
                ),
            },
        }

    def load_lora(
        self,
        model,
        adapter_kind: str,
        base_model: str,
        adapter_sha256: str,
        clip=None,
        strength_model: float = 1.0,
        strength_clip: float = 1.0,
        pixlstash_set: str = "",
        pixlstash_character: str = "",
    ):
        # adapter_kind / base_model / the two wires are read by the Browse
        # modal, not here — see the module docstring.
        record, path = self._resolve(adapter_sha256)
        triggers = _trigger_words(record)

        # AFTER the resolve, not before. The built-in returns early on a pair of
        # zero strengths and never touches the disk; here the shelf lookup is
        # also what produces trigger_words, and short-circuiting first would
        # emit an empty string for them whenever someone parked a strength at 0.
        model, clip = self._applier.apply(
            model, clip, path, strength_model, strength_clip
        )
        return (model, clip, triggers)

    @staticmethod
    def _resolve(adapter_sha256: str):
        return shelf_file.resolve(adapter_sha256, label=LABEL, folder_key="loras")

    @classmethod
    def VALIDATE_INPUTS(cls, base_model):
        # base_model's real option list is injected client-side, so accept any
        # runtime value rather than validating against the placeholder.
        return True

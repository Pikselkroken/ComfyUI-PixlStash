"""ComfyUI-PixlStash — custom node package entry point.

ComfyUI's node loader imports ``NODE_CLASS_MAPPINGS`` and optionally
``NODE_DISPLAY_NAME_MAPPINGS`` from this file, then serves every file
under ``WEB_DIRECTORY`` automatically.

Typical workflow:

    PixlStashProjectLoader  (optional)
        ├─> PixlStashSetLoader       (optional)
        └─> PixlStashCharacterLoader (optional)
                └─> PixlStashPictureLoader
                        └─> PixlStashPictureSaver

Credentials (URL, token, SSL) are configured once in
ComfyUI Settings › PixlStash and injected automatically at run time.
"""

from .nodes.project_loader import PixlStashProjectLoader
from .nodes.set_loader import PixlStashSetLoader
from .nodes.character_loader import PixlStashCharacterLoader
from .nodes.picture_loader import PixlStashPictureLoader
from .nodes.picture_saver import PixlStashPictureSaver
from .nodes.likeness_search import PixlStashLikenessSearch
from .nodes.face_likeness_gate import PixlStashFaceLikenessGate
from .nodes.picture_likeness_gate import PixlStashPictureLikenessGate
from .nodes.semantic_search import PixlStashSemanticSearch
from .nodes.adapter_loader import PixlStashAdapterLoader
from .nodes.checkpoint_loader import PixlStashCheckpointLoader
from .nodes.clip_loader import PixlStashCLIPLoader
from .nodes.vae_loader import PixlStashVAELoader
from .proxy_routes import register_routes

register_routes()

NODE_CLASS_MAPPINGS = {
    "PixlStashProjectLoader": PixlStashProjectLoader,
    "PixlStashSetLoader": PixlStashSetLoader,
    "PixlStashCharacterLoader": PixlStashCharacterLoader,
    "PixlStashPictureLoader": PixlStashPictureLoader,
    "PixlStashPictureSaver": PixlStashPictureSaver,
    "PixlStashLikenessSearch": PixlStashLikenessSearch,
    "PixlStashFaceLikenessGate": PixlStashFaceLikenessGate,
    "PixlStashPictureLikenessGate": PixlStashPictureLikenessGate,
    "PixlStashSemanticSearch": PixlStashSemanticSearch,
    "PixlStashAdapterLoader": PixlStashAdapterLoader,
    "PixlStashCheckpointLoader": PixlStashCheckpointLoader,
    "PixlStashVAELoader": PixlStashVAELoader,
    "PixlStashCLIPLoader": PixlStashCLIPLoader,
}

NODE_DISPLAY_NAME_MAPPINGS = {
    "PixlStashProjectLoader": "PixlStash Project Loader",
    "PixlStashSetLoader": "PixlStash Set Loader",
    "PixlStashCharacterLoader": "PixlStash Character Loader",
    "PixlStashPictureLoader": "PixlStash Picture Loader",
    "PixlStashPictureSaver": "PixlStash Picture Saver",
    "PixlStashLikenessSearch": "PixlStash Likeness Search",
    "PixlStashFaceLikenessGate": "PixlStash Face Likeness Gate",
    "PixlStashPictureLikenessGate": "PixlStash Picture Likeness Gate",
    "PixlStashSemanticSearch": "PixlStash Semantic Search",
    # "(LoRA)" is not a narrowing — the node takes LoKr, LoHa, OFT and DoRA
    # too — it is what makes it findable. Nearly every adapter anyone has is a
    # LoRA, and that is the word people type into the node search; "adapter"
    # is the shelf's word, not the ecosystem's.
    "PixlStashAdapterLoader": "PixlStash Adapter (LoRA) Loader",
    "PixlStashCheckpointLoader": "PixlStash Checkpoint Loader",
    "PixlStashVAELoader": "PixlStash VAE Loader",
    "PixlStashCLIPLoader": "PixlStash CLIP Loader",
}

WEB_DIRECTORY = "web/js"

__all__ = ["NODE_CLASS_MAPPINGS", "NODE_DISPLAY_NAME_MAPPINGS"]

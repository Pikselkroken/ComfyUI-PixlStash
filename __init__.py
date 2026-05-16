"""ComfyUI-PixlStash — custom node package entry point.

ComfyUI's node loader imports ``NODE_CLASS_MAPPINGS`` and optionally
``NODE_DISPLAY_NAME_MAPPINGS`` from this file, then serves every file
under the ``WEB_DIRECTORY`` path to the frontend automatically.
"""
from .loader import PixlStashImageLoader
from .saver import PixlStashImageSaver

NODE_CLASS_MAPPINGS = {
    "PixlStashImageLoader": PixlStashImageLoader,
    "PixlStashImageSaver": PixlStashImageSaver,
}

NODE_DISPLAY_NAME_MAPPINGS = {
    "PixlStashImageLoader": "PixlStash Image Loader",
    "PixlStashImageSaver": "PixlStash Image Saver",
}

WEB_DIRECTORY = "web"

__all__ = ["NODE_CLASS_MAPPINGS", "NODE_DISPLAY_NAME_MAPPINGS"]

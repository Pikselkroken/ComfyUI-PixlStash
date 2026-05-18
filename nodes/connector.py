"""PixlStash Connector node.

Packages the user's connection credentials into a ``PIXLSTASH_CONNECTION``
dict that flows through the rest of the PixlStash node chain.  No network
call is made here — validation happens lazily on first use in downstream
nodes, so an invalid URL / token surfaces a clear error message at the
right point without blocking graph loading.
"""
from __future__ import annotations


class PixlStashConnector:
    """Root node for every PixlStash workflow.

    Emits a typed ``PIXLSTASH_CONNECTION`` socket that the filter nodes
    (Project / Set / Character Loader) and the Picture Loader / Saver
    accept as input.  The token widget is marked ``serialize: false`` by
    the JavaScript extension so it is never written into exported workflow
    JSON files; it is re-filled automatically from ComfyUI Settings on
    graph load.
    """

    CATEGORY = "PixlStash"
    RETURN_TYPES = ("PIXLSTASH_CONNECTION",)
    RETURN_NAMES = ("connection",)
    FUNCTION = "build_connection"

    @classmethod
    def INPUT_TYPES(cls):
        return {
            "required": {
                "url": (
                    "STRING",
                    {
                        "default": "http://localhost:8000",
                        "multiline": False,
                        "tooltip": (
                            "Base URL of your PixlStash instance, "
                            "e.g. https://192.168.1.10:8000"
                        ),
                    },
                ),
                "verify_ssl": (
                    "BOOLEAN",
                    {
                        "default": True,
                        "tooltip": (
                            "Disable to accept self-signed certificates. "
                            "A warning is shown on the node when disabled."
                        ),
                    },
                ),
            },
            # token is optional so ComfyUI's prompt validator doesn't block
            # the run when the value is intentionally absent from workflow JSON
            # (serialize:false is set by the JS extension for security).
            # build_connection() raises a clear RuntimeError if it is blank.
            "optional": {
                "token": (
                    "STRING",
                    {
                        "default": "",
                        "multiline": False,
                        "tooltip": (
                            "Bearer token for authentication. "
                            "Stored in ComfyUI Settings › PixlStash — "
                            "never saved into exported workflow JSON."
                        ),
                    },
                ),
            },
        }

    def build_connection(self, url: str, verify_ssl: bool, token: str = ""):
        url = url.strip().rstrip("/")
        if not url:
            raise RuntimeError(
                "PixlStash Connector: url is required."
            )
        if not token.strip():
            raise RuntimeError(
                "PixlStash Connector: token is required. "
                "Enter it here or configure it in "
                "ComfyUI Settings › PixlStash › API Token."
            )
        conn: dict = {
            "url": url,
            "token": token,
            "verify_ssl": verify_ssl,
        }
        return (conn,)

"""PixlStash Project Loader node.

The ``project_id`` STRING input is converted into a live dropdown by the
JavaScript extension (``combo_widgets.js``).  On the Python side it is a
plain STRING so the node works in headless / API mode too.
"""

from __future__ import annotations


class PixlStashProjectLoader:
    """Lets the user pick a project from the PixlStash vault.

    Emits the chosen ``project_id`` as a string for downstream
    filter nodes or the Picture Loader / Saver.
    """

    CATEGORY = "PixlStash"
    RETURN_TYPES = ("PIXLSTASH_PROJECT",)
    RETURN_NAMES = ("pixlstash_project",)
    FUNCTION = "load_project"

    @classmethod
    def INPUT_TYPES(cls):
        return {
            "required": {
                "pixlstash_project": (
                    "PIXLSTASH_PROJECT_ID",
                    {
                        "default": "",
                        "tooltip": (
                            "Select a project. The dropdown is populated "
                            "live from your PixlStash instance."
                        ),
                    },
                ),
            }
        }

    def load_project(self, pixlstash_project: str):
        return (pixlstash_project.strip(),)

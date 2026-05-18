"""PixlStash Set Loader node.

The ``set_id`` STRING input is converted into a live dropdown by the
JavaScript extension (``combo_widgets.js``).  When ``project_id`` is
wired in, the proxy call passes it as a query parameter so PixlStash
returns only the sets that belong to that project.
"""
from __future__ import annotations


class PixlStashSetLoader:
    """Lets the user pick a picture set from the PixlStash vault.

    If ``project_id`` is wired from an upstream Project Loader, the
    dropdown shows only sets belonging to that project.  Without it,
    all sets accessible to the token are shown.
    """

    CATEGORY = "PixlStash"
    RETURN_TYPES = ("PIXLSTASH_PROJECT", "PIXLSTASH_SET")
    RETURN_NAMES = ("pixlstash_project", "pixlstash_set")
    FUNCTION = "load_set"

    @classmethod
    def INPUT_TYPES(cls):
        return {
            "required": {
                "pixlstash_set": (
                    "PIXLSTASH_SET_ID",
                    {
                        "default": "",
                        "multiline": False,
                        "tooltip": (
                            "Select a picture set. Populated live from "
                            "PixlStash. Wire a Project Loader to filter by project."
                        ),
                    },
                ),
            },
            "optional": {
                "pixlstash_project": (
                    "PIXLSTASH_PROJECT",
                    {
                        "forceInput": True,
                        "tooltip": (
                            "Wire from a Project Loader to restrict the "
                            "set list to one project."
                        ),
                    },
                ),
            },
        }

    def load_set(
        self,
        pixlstash_set: str,
        pixlstash_project: str = "",
    ):
        return (pixlstash_project, pixlstash_set.strip())

"""PixlStash Character Loader node.

The ``character_id`` STRING input is converted into a live dropdown by
the JavaScript extension (``combo_widgets.js``).  When ``project_id`` is
wired in, the proxy call passes it as a query parameter so PixlStash
returns only the characters that belong to that project.
"""

from __future__ import annotations

import re

_ID_RE = re.compile(r"#(\d+)\s*$")


def _extract_id(value: str) -> str:
    if not value:
        return ""
    m = _ID_RE.search(value)
    return m.group(1) if m else ""


class PixlStashCharacterLoader:
    """Lets the user pick a character from the PixlStash vault.

    If ``project_id`` is wired from an upstream Project Loader, the
    dropdown shows only characters belonging to that project.  Without
    it, all characters accessible to the token are shown.
    """

    CATEGORY = "PixlStash"
    RETURN_TYPES = ("PIXLSTASH_PROJECT", "PIXLSTASH_CHARACTER")
    RETURN_NAMES = ("pixlstash_project", "pixlstash_character")
    FUNCTION = "load_character"

    @classmethod
    def INPUT_TYPES(cls):
        return {
            "required": {
                "pixlstash_character": (
                    ["(loading…)"],
                    {
                        "tooltip": (
                            "Select a character. Populated live from "
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
                            "character list to one project."
                        ),
                    },
                ),
            },
        }

    def load_character(
        self,
        pixlstash_character: str,
        pixlstash_project: str = "",
    ):
        return (pixlstash_project, _extract_id(pixlstash_character))

    @classmethod
    def VALIDATE_INPUTS(cls, pixlstash_character):
        return True

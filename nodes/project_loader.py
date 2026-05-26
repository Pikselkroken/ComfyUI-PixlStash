"""PixlStash Project Loader node.

The ``pixlstash_project`` input is a plain COMBO whose option list is
populated dynamically by the JavaScript extension (``combo_widgets.js``).
Selected values are formatted ``"<name> #<id>"`` so Python can recover
the numeric ID with a simple regex, while the human-readable name stays
visible in the dropdown and in serialised workflows.
"""

from __future__ import annotations

import re

_ID_RE = re.compile(r"#(\d+)\s*$")


def _extract_id(value: str) -> str:
    """Return the numeric ID embedded in a combo selection, or ''."""
    if not value:
        return ""
    m = _ID_RE.search(value)
    return m.group(1) if m else ""


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
                    ["(loading…)"],
                    {
                        "tooltip": (
                            "Select a project. The dropdown is populated "
                            "live from your PixlStash instance."
                        ),
                    },
                ),
            }
        }

    def load_project(self, pixlstash_project: str):
        return (_extract_id(pixlstash_project),)

    @classmethod
    def VALIDATE_INPUTS(cls, pixlstash_project):
        # The dropdown list is populated dynamically by the JS extension;
        # accept any string here so ComfyUI doesn't reject runtime selections.
        return True

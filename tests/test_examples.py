"""The shipped example workflows: each has its card picture, and no crop dropdown
is left empty.

ComfyUI and PixlStash list every ``examples/*.json`` as a template and show the
``.jpg`` of the same name on its card. An API-format graph is validated against
each dropdown's list when it is queued, so an empty choice fails the whole run.
"""

import json
import pathlib
import unittest

EXAMPLES = pathlib.Path(__file__).resolve().parent.parent / "examples"

# InpaintCropImproved's dropdowns (ComfyUI-Inpaint-CropAndStitch).
CROP_CHOICES = {
    "preresize_mode": {
        "ensure minimum resolution",
        "ensure maximum resolution",
        "ensure minimum and maximum resolution",
    },
    "device_mode": {"cpu (compatible)", "gpu (much faster)"},
}


class ExamplesTest(unittest.TestCase):
    def test_every_example_has_its_picture(self):
        for graph in EXAMPLES.glob("*.json"):
            with self.subTest(graph=graph.name):
                self.assertTrue(graph.with_suffix(".jpg").is_file())

    def test_inpaint_crop_dropdowns_hold_a_listed_choice(self):
        crops = 0
        for path in EXAMPLES.glob("*.json"):
            graph = json.loads(path.read_text(encoding="utf-8"))
            if "nodes" in graph:  # editor format: dropdowns are positional
                continue
            for node_id, node in graph.items():
                if node.get("class_type") != "InpaintCropImproved":
                    continue
                crops += 1
                for name, choices in CROP_CHOICES.items():
                    with self.subTest(graph=path.name, node=node_id, input=name):
                        self.assertIn(node["inputs"][name], choices)
        self.assertGreaterEqual(crops, 5)


if __name__ == "__main__":
    unittest.main()

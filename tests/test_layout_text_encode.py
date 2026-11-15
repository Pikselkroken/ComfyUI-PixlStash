"""Layout Text Encode: a layout prompt read as regions, anything else as text.

``clip`` is a stand-in recording what it was asked to encode; what is worth
pinning is where each caption lands on the canvas and that plain text is left
alone.
"""

import json
import unittest

import _bootstrap as boot

lte = boot.load("nodes.layout_text_encode")
NODE = lte.PixlStashLayoutTextEncode


class _Clip:
    def __init__(self):
        self.encoded = []

    def tokenize(self, text):
        return text

    def encode_from_tokens_scheduled(self, tokens):
        self.encoded.append(tokens)
        return [[f"tensor:{tokens}", {"pooled_output": tokens}]]


def _layout(**overrides):
    layout = {
        "summary": {"caption": "a sheet"},
        "items": [
            # The left half's top-left quarter of a 2560x1536 canvas.
            {"caption": "head", "bbox": [-768, -1280, 0, -640]},
            # Past the canvas edge: clamped to it.
            {"caption": "body", "bbox": [-1000, 0, 1000, 1280]},
        ],
        "bbox_coord_space": "abs_centralized",
        "bbox_format": "yxyx",
        "canvas": [2560, 1536],
    }
    layout.update(overrides)
    return json.dumps(layout)


class LayoutRegionsTests(unittest.TestCase):
    def test_boxes_become_fractions_of_the_canvas(self):
        summary, regions = lte.layout_regions(_layout())
        self.assertEqual(summary, "a sheet")
        self.assertEqual(
            regions,
            [("head", (0.5, 0.25, 0.0, 0.0)), ("body", (1.0, 0.5, 0.0, 0.5))],
        )

    def test_what_is_not_a_placeable_layout_is_none(self):
        for text in (
            "a portrait of a woman",
            '{"prompt": "x"}',
            _layout(canvas=None),
            _layout(bbox_format="xyxy"),
            _layout(bbox_coord_space="relative"),
            _layout(canvas=[0, 1536]),
            _layout(canvas=["a", "b"]),
            _layout(summary="a sheet"),
        ):
            with self.subTest(text=text):
                self.assertIsNone(lte.layout_regions(text))

    def test_an_item_with_no_area_is_skipped(self):
        _summary, regions = lte.layout_regions(
            _layout(
                items=[
                    {"caption": "x", "bbox": [5, 5, 5, 9]},
                    {"caption": "y"},
                    {"caption": "z", "bbox": [0, 0, "a", 1]},
                    "not an item",
                ]
            )
        )
        self.assertEqual(regions, [])


class EncodeTests(unittest.TestCase):
    def test_plain_text_is_one_encode_with_no_area(self):
        clip = _Clip()
        (conditioning,) = NODE().encode(clip, "a portrait", 1.0)
        self.assertEqual(clip.encoded, ["a portrait"])
        self.assertNotIn("area", conditioning[0][1])

    def test_a_layout_is_the_summary_plus_one_area_per_item(self):
        clip = _Clip()
        (conditioning,) = NODE().encode(clip, _layout(), 1.5)
        self.assertEqual(clip.encoded, ["a sheet", "head", "body"])
        self.assertNotIn("area", conditioning[0][1])
        head = conditioning[1][1]
        self.assertEqual(head["area"], ("percentage", 0.5, 0.25, 0.0, 0.0))
        self.assertEqual(head["strength"], 1.5)
        self.assertFalse(head["set_area_to_bounds"])
        self.assertEqual(head["pooled_output"], "head")
        self.assertEqual(conditioning[2][1]["area"][3:], (0.0, 0.5))

    def test_no_clip_is_an_error_naming_the_node(self):
        with self.assertRaisesRegex(RuntimeError, "Layout Text Encode"):
            NODE().encode(None, "x", 1.0)


if __name__ == "__main__":
    unittest.main()

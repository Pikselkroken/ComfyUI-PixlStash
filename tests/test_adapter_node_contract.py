"""The surface ComfyUI actually consumes on the adapter node.

None of this is clever, and all of it is load-bearing: ComfyUI reads
``INPUT_TYPES``, ``RETURN_TYPES``/``RETURN_NAMES``, ``FUNCTION`` and
``CATEGORY`` off the class, and a mismatch between them (a renamed method, a
returns tuple one shorter than its names, a widget the JS looks for by name and
cannot find) is a node that loads and then fails in the UI, not an import
error anyone would notice.
"""

import ast
import pathlib
import unittest

import _bootstrap as boot

adapter_loader = boot.load("nodes.adapter_loader")

LOADER = adapter_loader.PixlStashAdapterLoader


def _display_names():
    """``NODE_DISPLAY_NAME_MAPPINGS`` out of ``__init__.py`` without importing it."""
    source = pathlib.Path(__file__).resolve().parent.parent / "__init__.py"
    tree = ast.parse(source.read_text(encoding="utf-8"))
    for node in tree.body:
        targets = getattr(node, "targets", [])
        if any(
            isinstance(t, ast.Name) and t.id == "NODE_DISPLAY_NAME_MAPPINGS"
            for t in targets
        ):
            return ast.literal_eval(node.value)
    raise AssertionError("__init__.py declares no NODE_DISPLAY_NAME_MAPPINGS")


class NodeContractTests(unittest.TestCase):
    def test_declares_a_callable_function_and_a_category(self):
        self.assertEqual(LOADER.CATEGORY, "PixlStash")
        self.assertTrue(callable(getattr(LOADER, LOADER.FUNCTION, None)))

    def test_return_names_line_up_with_return_types(self):
        self.assertEqual(len(LOADER.RETURN_TYPES), len(LOADER.RETURN_NAMES))

    def test_every_output_has_a_tooltip(self):
        # ComfyUI pairs OUTPUT_TOOLTIPS with the outputs BY INDEX, so a tuple
        # one short does not raise — it silently moves every tooltip after the
        # gap onto the wrong socket, which is worse than having none.
        self.assertEqual(len(LOADER.OUTPUT_TOOLTIPS), len(LOADER.RETURN_NAMES))
        for tooltip in LOADER.OUTPUT_TOOLTIPS:
            self.assertTrue(tooltip.strip())

    def test_the_node_describes_itself(self):
        # The node tooltip, shown on hover in the node browser. Its job is to
        # answer "what is this and do I want it" before the node is placed.
        self.assertGreater(len(LOADER.DESCRIPTION), 80)
        # LoRA, not "adapter": it is the word someone searching for this node
        # actually types.
        self.assertIn("LoRA", LOADER.DESCRIPTION)

    def test_the_display_name_says_lora(self):
        # Findability, checked here rather than left to the eye: the node is
        # named for the shelf's word ("adapter") and searched for by the
        # ecosystem's ("lora").
        # Read statically rather than imported: `__init__` pulls in every node
        # in the pack, and the picture ones want a real numpy. The mapping is a
        # literal, so parsing it is not a weaker check than importing it.
        self.assertIn("(LoRA)", _display_names()["PixlStashAdapterLoader"])


class LoaderContractTests(unittest.TestCase):
    def setUp(self):
        self.spec = LOADER.INPUT_TYPES()

    def test_is_shaped_like_the_built_in_lora_loader(self):
        # The whole point of the node's second design: MODEL/CLIP in the same
        # order and the same types as ComfyUI's LoraLoader, so it drops into a
        # graph where one already sits. A STRING output here would be the old
        # design back again, and the old design could not be wired to anything.
        self.assertEqual(LOADER.RETURN_TYPES[:2], ("MODEL", "CLIP"))
        self.assertEqual(LOADER.RETURN_NAMES[:2], ("model", "clip"))
        self.assertIn("model", self.spec["required"])
        self.assertEqual(self.spec["required"]["model"][0], "MODEL")
        self.assertEqual(self.spec["optional"]["clip"][0], "CLIP")

    def test_carries_both_strengths_with_the_built_in_s_range(self):
        for name in ("strength_model", "strength_clip"):
            with self.subTest(widget=name):
                kind, opts = self.spec["optional"][name]
                self.assertEqual(kind, "FLOAT")
                self.assertEqual(opts["default"], 1.0)
                # Same range as the built-in, negatives included: subtracting a
                # LoRA is a real technique, and clamping at 0 would forbid it.
                self.assertEqual((opts["min"], opts["max"]), (-100.0, 100.0))

    def test_the_widgets_the_js_drives_by_name_exist(self):
        # combo_widgets.js looks these up with widgets.find(w => w.name === …)
        # and returns early if any is missing, so a rename here silently
        # disables the Browse button rather than erroring.
        for name in ("adapter_kind", "base_model", "adapter_sha256"):
            self.assertIn(name, self.spec["required"])

    def test_the_filter_wires_are_optional_and_wire_only(self):
        for name, wire in (
            ("pixlstash_set", "PIXLSTASH_SET"),
            ("pixlstash_character", "PIXLSTASH_CHARACTER"),
        ):
            with self.subTest(name=name):
                declared, opts = self.spec["optional"][name]
                self.assertEqual(declared, wire)
                self.assertTrue(opts["forceInput"])

    def test_no_credential_widgets(self):
        # Credentials are resolved server-side from ComfyUI Settings; a widget
        # here would put the token in the prompt and the saved workflow.
        declared = set(self.spec["required"]) | set(self.spec.get("optional", {}))
        for forbidden in ("url", "token", "api_token", "verify_ssl"):
            self.assertNotIn(forbidden, declared)

    def test_adapter_kind_offers_the_algorithms_the_server_can_emit(self):
        kinds, _ = self.spec["required"]["adapter_kind"]
        self.assertEqual(kinds[0], adapter_loader.ANY_KIND)
        for kind in ("lora", "lokr", "loha", "oft", "dora"):
            self.assertIn(kind, kinds)

    def test_base_model_is_a_placeholder_the_js_replaces(self):
        # The literal must match LOADING_LABEL in combo_widgets.js.
        values, _ = self.spec["required"]["base_model"]
        self.assertEqual(values, ["(loading…)"])

    def test_validate_inputs_accepts_the_runtime_injected_combo(self):
        # Declared with base_model in its signature so ComfyUI skips its own
        # check on a list that is only populated client-side.
        self.assertTrue(LOADER.VALIDATE_INPUTS(base_model="anything at all"))

    def test_no_is_changed(self):
        # A NaN here would invalidate every downstream node on every queue.
        self.assertFalse(hasattr(LOADER, "IS_CHANGED"))


if __name__ == "__main__":
    unittest.main()

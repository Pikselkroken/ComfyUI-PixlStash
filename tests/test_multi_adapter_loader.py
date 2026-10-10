"""PixlStashMultiAdapterLoader — several shelf adapters in one node.

Two things are pinned.  The surface: the rows are flat, numbered inputs under
the names PixlStash finds LoRA slots by, which is a contract and not a style.
And the behaviour the issue lists as checks: a node of two rows is two chained
Adapter Loaders, an empty node hands its inputs back, each row is applied with
its own strengths, and a digest the shelf does not have fails naming its row.

``comfy`` is the recording stub from ``test_adapter_applier``; the shelf is a
dict.
"""

import pathlib
import re
import sys
import unittest
from unittest import mock

import _bootstrap as boot
from test_adapter_applier import CALLS

adapter_loader = boot.load("nodes.adapter_loader")
multi = boot.load("nodes.multi_adapter_loader")
shelf_file = boot.load("nodes.shelf_file")
lock = boot.load("nodes.lock")

NODE = multi.PixlStashMultiAdapterLoader

A, B, C = "a" * 64, "b" * 64, "c" * 64
SHELF = {
    A: {"sha256": A, "filename": "a.safetensors", "trigger_words": '["alpha"]'},
    B: {"sha256": B, "filename": "b.safetensors", "trigger_words": "beta, two"},
    C: {"sha256": C, "filename": "c.safetensors", "trigger_words": None},
}

# What PixlStash matches a LoRA digest slot with (LORA_DIGEST_FIELD_RE in its
# comfyui_recipe_service.py). Copied, because the contract is the spelling.
PIXLSTASH_DIGEST_FIELD = re.compile(r"^(adapter|lora)_sha256(_\d+)?$")


def fake_resolve(sha256, *, label, folder_key, **_):
    if sha256 not in SHELF:
        # As the client words a 404: with no node and no row in it.
        raise RuntimeError(f"PixlStash: not found — /api/v1/adapters/{sha256}")
    return SHELF[sha256], f"/loras/{SHELF[sha256]['filename']}"


def run(node=None, clip="CLIP", **rows):
    """Run the node with ``rows`` set and every other row empty."""
    inputs = {}
    for row in range(1, multi.ROWS + 1):
        inputs[multi.row_field("adapter_sha256", row)] = ""
        inputs[multi.row_field("strength_model", row)] = 1.0
        inputs[multi.row_field("strength_clip", row)] = 1.0
    inputs.update(rows)
    for calls in CALLS.values():
        calls.clear()
    with mock.patch.object(shelf_file, "resolve", fake_resolve):
        return (node or NODE()).load_loras(
            "MODEL", multi.SHOW_ALL, adapter_loader.ANY_KIND, "", clip=clip, **inputs
        )


class ContractTests(unittest.TestCase):
    def setUp(self):
        self.spec = NODE.INPUT_TYPES()
        self.declared = {**self.spec["required"], **self.spec["optional"]}

    def test_same_wires_as_the_adapter_loader(self):
        single = adapter_loader.PixlStashAdapterLoader
        self.assertEqual(NODE.CATEGORY, single.CATEGORY)
        self.assertEqual(NODE.RETURN_TYPES, single.RETURN_TYPES)
        self.assertEqual(NODE.RETURN_NAMES, single.RETURN_NAMES)
        self.assertEqual(self.spec["required"]["model"][0], "MODEL")
        self.assertEqual(self.spec["optional"]["clip"][0], "CLIP")
        self.assertTrue(callable(getattr(NODE, NODE.FUNCTION, None)))
        self.assertEqual(len(NODE.OUTPUT_TOOLTIPS), len(NODE.RETURN_NAMES))

    def test_the_rows_are_flat_numbered_inputs(self):
        digests = [name for name in self.declared if "sha256" in name]
        self.assertEqual(
            digests,
            ["adapter_sha256"] + [f"adapter_sha256_{n}" for n in range(2, 9)],
        )
        for name in digests:
            with self.subTest(name=name):
                self.assertRegex(name, PIXLSTASH_DIGEST_FIELD)
                kind, opts = self.declared[name]
                # An empty row is an empty digest, not a missing input.
                self.assertEqual((kind, opts["default"]), ("STRING", ""))

    def test_every_row_has_its_own_two_strengths(self):
        for row in range(1, multi.ROWS + 1):
            for field in ("strength_model", "strength_clip"):
                with self.subTest(row=row, field=field):
                    kind, opts = self.declared[multi.row_field(field, row)]
                    self.assertEqual(kind, "FLOAT")
                    self.assertEqual(opts["default"], 1.0)
                    self.assertEqual((opts["min"], opts["max"]), (-100.0, 100.0))

    def test_a_row_is_declared_together(self):
        # widgets_values is positional, so the order is what a saved graph is.
        names = [n for n in self.spec["optional"] if n != "clip"]
        self.assertEqual(names[:6], [
            "adapter_sha256", "strength_model", "strength_clip",
            "adapter_sha256_2", "strength_model_2", "strength_clip_2",
        ])  # fmt: skip
        self.assertEqual(len(names), 3 * multi.ROWS)

    def test_show_offers_all_adapters_then_people(self):
        # combo_widgets.js reads the people view as the SECOND value.
        values, opts = self.spec["required"]["show"]
        self.assertEqual(values, ["All adapters", "People who fit"])
        self.assertEqual(opts["default"], "All adapters")

    def test_the_grid_filters_are_the_adapter_loader_s(self):
        single = adapter_loader.PixlStashAdapterLoader.INPUT_TYPES()["required"]
        for name in ("adapter_kind", "base_model"):
            self.assertEqual(self.spec["required"][name][0], single[name][0])
        self.assertTrue(NODE.VALIDATE_INPUTS(base_model="anything at all"))

    def test_no_credential_widgets_and_no_is_changed(self):
        for forbidden in ("url", "token", "api_token", "verify_ssl"):
            self.assertNotIn(forbidden, self.declared)
        self.assertFalse(hasattr(NODE, "IS_CHANGED"))

    def test_the_browser_declares_the_same_number_of_rows(self):
        js = pathlib.Path(boot.ROOT, "web", "js", "adapter_rows.js").read_text("utf-8")
        self.assertIn(f"export const ROWS = {multi.ROWS};", js)

    def test_the_browser_knows_which_server_the_node_needs(self):
        # Every node has an entry; without one the version banner never draws.
        js = pathlib.Path(boot.ROOT, "web", "js", "combo_widgets.js").read_text("utf-8")
        self.assertRegex(js, r'"PixlStashMultiAdapterLoader":\s*"1\.10\.0"')

    def test_it_is_registered_beside_the_adapter_loader(self):
        init = pathlib.Path(boot.ROOT, "__init__.py").read_text("utf-8")
        self.assertIn(
            '"PixlStashMultiAdapterLoader": PixlStashMultiAdapterLoader,', init
        )
        self.assertIn('"PixlStash Multi Adapter (LoRA) Loader"', init)


class ApplyTests(unittest.TestCase):
    def test_two_rows_are_two_chained_adapter_loaders(self):
        out = run(
            adapter_sha256=A, strength_model=0.8, strength_clip=0.6,
            adapter_sha256_2=B, strength_model_2=0.5, strength_clip_2=0.25,
        )  # fmt: skip
        in_one_node = list(CALLS["applies"])

        for calls in CALLS.values():
            calls.clear()
        with mock.patch.object(shelf_file, "resolve", fake_resolve):
            first = adapter_loader.PixlStashAdapterLoader().load_lora(
                "MODEL", "", "", A, clip="CLIP", strength_model=0.8, strength_clip=0.6
            )["result"]
            second = adapter_loader.PixlStashAdapterLoader().load_lora(
                first[0], "", "", B, clip=first[1],
                strength_model=0.5, strength_clip=0.25,
            )["result"]  # fmt: skip

        self.assertEqual(out["result"][:2], second[:2])
        self.assertEqual(in_one_node, CALLS["applies"])
        self.assertEqual(len(in_one_node), 2)

    def test_all_rows_empty_hands_the_inputs_back(self):
        model, clip = object(), object()
        with mock.patch.object(shelf_file, "resolve", side_effect=AssertionError):
            out = NODE().load_loras(
                model, multi.SHOW_PEOPLE, adapter_loader.ANY_KIND, "", clip=clip
            )
        self.assertIs(out["result"][0], model)
        self.assertIs(out["result"][1], clip)
        self.assertEqual(out["result"][2], "")
        self.assertEqual(out["ui"][lock.UI_KEY], [{"pictures": [], "models": []}])

    def test_an_empty_row_between_two_is_skipped(self):
        out = run(adapter_sha256=A, adapter_sha256_3=C, adapter_sha256_2="  ")
        self.assertEqual(
            [call[2] for call in CALLS["applies"]],
            [
                {"state_dict_for": "/loras/a.safetensors"},
                {"state_dict_for": "/loras/c.safetensors"},
            ],
        )
        self.assertEqual(out["result"][0], "MODEL+patched+patched")

    def test_clip_unwired_is_model_only(self):
        out = run(clip=None, adapter_sha256=A, adapter_sha256_2=B)
        self.assertEqual(out["result"][0], "MODEL+patched+patched")
        self.assertIsNone(out["result"][1])
        self.assertEqual([call[1] for call in CALLS["applies"]], [None, None])

    def test_the_second_row_s_strengths_go_to_the_second_adapter(self):
        run(
            adapter_sha256=A, strength_model=0.9, strength_clip=0.8,
            adapter_sha256_2=B, strength_model_2=0.3, strength_clip_2=-0.2,
        )  # fmt: skip
        first, second = CALLS["applies"]
        self.assertEqual(
            first[2:], ({"state_dict_for": "/loras/a.safetensors"}, 0.9, 0.8)
        )
        self.assertEqual(second[2:], ({"state_dict_for": "/loras/b.safetensors"}, 0.3, -0.2))  # fmt: skip
        # Each to the result of the one before.
        self.assertEqual(second[0], "MODEL+patched")

    def test_a_row_missing_from_the_prompt_is_an_empty_row(self):
        with mock.patch.object(shelf_file, "resolve", fake_resolve):
            out = NODE().load_loras("MODEL", "", "", "", adapter_sha256_2=B)
        self.assertEqual(out["result"][0], "MODEL+patched")

    def test_the_same_adapter_on_two_rows_is_applied_twice(self):
        # As two chained Adapter Loaders holding the same file would.
        out = run(adapter_sha256=A, strength_model=0.5, adapter_sha256_2=A)
        self.assertEqual([call[3] for call in CALLS["applies"]], [0.5, 1.0])
        self.assertEqual(CALLS["loads"], ["/loras/a.safetensors"])
        self.assertEqual(out["result"][2], "alpha, alpha")

    def test_a_digest_is_normalised_before_it_is_looked_up(self):
        out = run(adapter_sha256=f"  {A.upper()} ")
        self.assertEqual(out["ui"][lock.UI_KEY][0]["models"][0]["sha256"], A)


class TriggerWordTests(unittest.TestCase):
    def test_every_row_s_words_in_row_order(self):
        out = run(adapter_sha256=B, adapter_sha256_2=C, adapter_sha256_3=A)
        # C has none, and leaves no empty slot between the commas.
        self.assertEqual(out["result"][2], "beta, two, alpha")

    def test_a_strength_of_zero_still_gives_the_row_s_words(self):
        out = run(adapter_sha256=A, strength_model=0.0, strength_clip=0.0)
        self.assertEqual(out["result"][2], "alpha")
        self.assertEqual(out["result"][0], "MODEL")
        self.assertEqual(CALLS["loads"], [])


class FailureTests(unittest.TestCase):
    def test_a_digest_the_shelf_does_not_have_names_its_row(self):
        with self.assertRaises(RuntimeError) as raised:
            run(adapter_sha256=A, adapter_sha256_2="d" * 64, adapter_sha256_3=B)
        message = str(raised.exception)
        self.assertIn("PixlStash Multi Adapter Loader", message)
        self.assertIn("row 2", message)
        self.assertNotIn("row 1", message)

    def test_the_row_named_is_the_one_on_the_node_not_a_count(self):
        # Row 1 empty: the bad digest is on row 3 and is called row 3.
        with self.assertRaises(RuntimeError) as raised:
            run(adapter_sha256_2=A, adapter_sha256_3="d" * 64)
        self.assertIn("row 3", str(raised.exception))

    def test_a_value_that_is_not_a_digest_names_its_row(self):
        # Refused by shelf_file.resolve itself, before any request.
        with self.assertRaises(RuntimeError) as raised:
            NODE().load_loras("MODEL", "", "", "", adapter_sha256_2="not-a-digest")
        self.assertEqual(str(raised.exception).count("row 2"), 1)
        self.assertIn("64-character", str(raised.exception))

    def test_an_error_that_already_names_the_row_is_not_named_twice(self):
        def labelled(sha256, *, label, **_):
            raise RuntimeError(f"{label}: nothing selected.")

        with mock.patch.object(shelf_file, "resolve", labelled):
            with self.assertRaises(RuntimeError) as raised:
                NODE().load_loras("MODEL", "", "", "", adapter_sha256=A)
        self.assertEqual(str(raised.exception).count("row 1"), 1)


class LockTests(unittest.TestCase):
    def test_every_applied_adapter_is_reported_in_row_order(self):
        out = run(adapter_sha256=B, adapter_sha256_2=A)
        [entry] = out["ui"][lock.UI_KEY]
        self.assertEqual(
            [(m["kind"], m["sha256"], m["filename"]) for m in entry["models"]],
            [("adapter", B, "b.safetensors"), ("adapter", A, "a.safetensors")],
        )

    def test_a_row_parked_at_zero_is_reported_as_the_adapter_loader_reports_it(self):
        # Resolved, not patched in: the single loader reports that too, and
        # the two must not disagree about what a zero means.
        out = run(adapter_sha256=A, strength_model=0.0, strength_clip=0.0)
        with mock.patch.object(shelf_file, "resolve", fake_resolve):
            single = adapter_loader.PixlStashAdapterLoader().load_lora(
                "MODEL", "", "", A, clip="CLIP", strength_model=0.0, strength_clip=0.0
            )
        self.assertEqual(out["ui"], single["ui"])
        self.assertEqual(len(out["ui"][lock.UI_KEY][0]["models"]), 1)


class CacheTests(unittest.TestCase):
    def test_a_row_moving_up_does_not_re_read_its_file(self):
        node = NODE()
        run(node, adapter_sha256=A, adapter_sha256_2=B)
        self.assertEqual(CALLS["loads"], ["/loras/a.safetensors", "/loras/b.safetensors"])  # fmt: skip
        # Row 1 removed: B is now row 1.
        run(node, adapter_sha256=B)
        self.assertEqual(CALLS["loads"], [])

    def test_an_adapter_taken_off_the_node_is_let_go(self):
        node = NODE()
        run(node, adapter_sha256=A, adapter_sha256_2=B)
        run(node, adapter_sha256=B)
        self.assertEqual(list(node._appliers), [B])

    def test_it_is_let_go_before_its_replacement_is_read(self):
        # Otherwise swapping one adapter for another peaks at both in memory.
        node = NODE()
        run(node, adapter_sha256=A)
        held = []
        utils = sys.modules["comfy.utils"]
        original = utils.load_torch_file

        def observing_load(path, **kwargs):
            held.append(list(node._appliers))
            return original(path, **kwargs)

        with mock.patch.object(utils, "load_torch_file", observing_load):
            run(node, adapter_sha256=C)
        self.assertEqual(held, [[C]])


if __name__ == "__main__":
    unittest.main()

"""The VAE, CLIP and Checkpoint loaders — contract, and the logic each adds.

Resolution itself is `shelf_file`'s and is covered by test_adapter_path.py /
test_adapter_download.py / test_adapter_resolve.py. What is left per node is
small and easy to get wrong: the widget names `combo_widgets.js` drives by
name (a rename silently disables the Browse button rather than erroring), the
CLIP loader's one-or-two-file rule, and the checkpoint loader's id lookup,
which is a linear scan of a list route because the server has no by-id one.
"""

import ast
import pathlib
import sys
import types
import unittest
from unittest import mock

import _bootstrap as boot

shelf_file = boot.load("nodes.shelf_file")
vae_loader = boot.load("nodes.vae_loader")
clip_loader = boot.load("nodes.clip_loader")
checkpoint_loader = boot.load("nodes.checkpoint_loader")

VAE = vae_loader.PixlStashVAELoader
CLIP = clip_loader.PixlStashCLIPLoader
CKPT = checkpoint_loader.PixlStashCheckpointLoader

SHA = "a" * 64


def _display_names():
    """``NODE_DISPLAY_NAME_MAPPINGS`` out of ``__init__.py`` without importing it."""
    source = pathlib.Path(__file__).resolve().parent.parent / "__init__.py"
    tree = ast.parse(source.read_text(encoding="utf-8"))
    for node in tree.body:
        if any(
            isinstance(t, ast.Name) and t.id == "NODE_DISPLAY_NAME_MAPPINGS"
            for t in getattr(node, "targets", [])
        ):
            return ast.literal_eval(node.value)
    raise AssertionError("NODE_DISPLAY_NAME_MAPPINGS not found in __init__.py")


class ContractTests(unittest.TestCase):
    def test_each_node_is_a_pixlstash_node_with_a_callable_function(self):
        for cls in (VAE, CLIP, CKPT):
            with self.subTest(node=cls.__name__):
                self.assertEqual(cls.CATEGORY, "PixlStash")
                self.assertTrue(callable(getattr(cls, cls.FUNCTION, None)))
                self.assertGreater(len(cls.DESCRIPTION), 80)

    def test_output_tooltips_line_up_with_the_outputs(self):
        # Paired BY INDEX by ComfyUI, so a short tuple moves every later
        # tooltip onto the wrong socket instead of raising.
        for cls in (VAE, CLIP, CKPT):
            with self.subTest(node=cls.__name__):
                self.assertEqual(len(cls.RETURN_TYPES), len(cls.RETURN_NAMES))
                self.assertEqual(len(cls.OUTPUT_TOOLTIPS), len(cls.RETURN_NAMES))

    def test_they_return_what_the_built_ins_do(self):
        self.assertEqual(VAE.RETURN_TYPES, ("VAE",))
        self.assertEqual(CLIP.RETURN_TYPES, ("CLIP",))
        self.assertEqual(CKPT.RETURN_TYPES, ("MODEL", "CLIP", "VAE"))

    def test_the_widgets_the_js_drives_by_name_exist(self):
        # combo_widgets.js finds these with widgets.find(w => w.name === …).
        # Kept in step with SHELF_BROWSERS there.
        cases = (
            (VAE, "vae_sha256", "required"),
            (CLIP, "clip_sha256", "required"),
            (CLIP, "clip_sha256_2", "optional"),
            (CKPT, "checkpoint_id", "required"),
        )
        for cls, name, section in cases:
            with self.subTest(node=cls.__name__, widget=name):
                self.assertIn(name, cls.INPUT_TYPES()[section])

    def test_no_credential_widgets(self):
        # Credentials are resolved server-side from ComfyUI Settings; a widget
        # here would put the token in the prompt and the saved workflow.
        for cls in (VAE, CLIP, CKPT):
            spec = cls.INPUT_TYPES()
            declared = set(spec["required"]) | set(spec.get("optional", {}))
            for forbidden in ("url", "token", "api_token", "verify_ssl"):
                with self.subTest(node=cls.__name__, widget=forbidden):
                    self.assertNotIn(forbidden, declared)

    def test_the_display_names_are_registered(self):
        names = _display_names()
        for key in (
            "PixlStashVAELoader",
            "PixlStashCLIPLoader",
            "PixlStashCheckpointLoader",
        ):
            with self.subTest(node=key):
                self.assertIn(key, names)


class ClipTypeTests(unittest.TestCase):
    """The `type` combo is read off ComfyUI's enum, not copied out of it."""

    def test_falls_back_to_a_list_when_comfy_is_not_importable(self):
        # This is the case the tests themselves run in, and the one a node
        # scan hits if comfy.sd ever stops exposing CLIPType: it must still
        # return a non-empty combo rather than raise during registration.
        values, opts = CLIP.INPUT_TYPES()["required"]["type"]
        self.assertTrue(values)
        self.assertEqual(values[0], "stable_diffusion")
        self.assertEqual(opts["default"], "stable_diffusion")

    def test_stable_diffusion_leads_whatever_order_the_enum_is_in(self):
        class _Member:
            def __init__(self, name):
                self.name = name

        fake = mock.Mock()
        fake.sd.CLIPType = [
            _Member("FLUX"),
            _Member("STABLE_DIFFUSION"),
            _Member("SD3"),
        ]
        with mock.patch.dict("sys.modules", {"comfy": fake, "comfy.sd": fake.sd}):
            self.assertEqual(
                clip_loader._clip_types(), ["stable_diffusion", "flux", "sd3"]
            )

    def test_a_stale_saved_type_is_accepted_rather_than_failing_validation(self):
        # load_clip falls back to STABLE_DIFFUSION for a value this ComfyUI
        # does not know; it never gets the chance if validation rejects first.
        self.assertTrue(CLIP.VALIDATE_INPUTS(type="whatever_ships_next"))


class ClipPathTests(unittest.TestCase):
    """One file or two, and nothing in between."""

    def setUp(self):
        # A comfy stub of our own rather than whatever another test module
        # left in sys.modules: what is under test is which paths reach
        # `load_clip` and in what order, so it has to be this fake recording
        # them. `comfy.sd.CLIPType` is a plain object, so the getattr fallback
        # in the node lands on STABLE_DIFFUSION for anything it does not carry.
        self.calls = {}

        comfy = types.ModuleType("comfy")
        comfy.__path__ = []
        sd = types.ModuleType("comfy.sd")

        class CLIPType:
            STABLE_DIFFUSION = "sd"
            FLUX = "flux"

        def load_clip(ckpt_paths, embedding_directory=None, clip_type=None):
            self.calls["paths"] = list(ckpt_paths)
            self.calls["clip_type"] = clip_type
            return "CLIP"

        sd.CLIPType = CLIPType
        sd.load_clip = load_clip
        comfy.sd = sd

        fp = types.ModuleType("folder_paths")
        fp.get_folder_paths = lambda kind: ["/models/text_encoders"]

        self._patch = mock.patch.dict(
            sys.modules,
            {"comfy": comfy, "comfy.sd": sd, "folder_paths": fp},
        )
        self._patch.start()
        self.addCleanup(self._patch.stop)

    def _load(self, first, second, clip_type="flux"):
        """The shas `load_clip` resolved, in order."""
        resolved = []

        def fake_resolve(sha, **kwargs):
            resolved.append(sha)
            return ({}, f"/models/{sha[:4]}.safetensors")

        with mock.patch.object(shelf_file, "resolve", fake_resolve):
            out = CLIP().load_clip(first, clip_type, second)
        self.assertEqual(out, ("CLIP",))
        return resolved

    def test_one_hash_resolves_one_file(self):
        self.assertEqual(self._load(SHA, ""), [SHA])
        self.assertEqual(self.calls["paths"], ["/models/aaaa.safetensors"])

    def test_two_hashes_resolve_in_widget_order(self):
        # ckpt_paths order is the pair's order; swapping it is a different
        # (and usually broken) model.
        second = "b" * 64
        self.assertEqual(self._load(SHA, second), [SHA, second])
        self.assertEqual(
            self.calls["paths"],
            ["/models/aaaa.safetensors", "/models/bbbb.safetensors"],
        )

    def test_whitespace_in_the_second_slot_is_not_a_second_file(self):
        self.assertEqual(self._load(SHA, "   "), [SHA])

    def test_the_type_widget_picks_the_clip_type(self):
        self._load(SHA, "", clip_type="flux")
        self.assertEqual(self.calls["clip_type"], "flux")

    def test_a_type_this_comfyui_never_heard_of_lands_on_stable_diffusion(self):
        # The combo was built from another ComfyUI's enum, saved, and reopened
        # here. Falling back beats refusing to run.
        self._load(SHA, "", clip_type="ships_next_year")
        self.assertEqual(self.calls["clip_type"], "sd")

    def test_nothing_selected_is_refused_before_any_request(self):
        def explode(*a, **k):
            raise AssertionError("resolved a file with nothing selected")

        with mock.patch.object(shelf_file, "resolve", explode):
            with self.assertRaises(RuntimeError) as ctx:
                CLIP().load_clip("", "flux", "")
        self.assertIn("no text encoder selected", str(ctx.exception))


class _Client:
    def __init__(self, payload):
        self.payload = payload
        self.paths = []

    def get(self, path, **kwargs):
        self.paths.append(path)
        return self

    def json(self):
        return self.payload


class CheckpointLookupTests(unittest.TestCase):
    """Addressed by id, because `sha256` is null until the shelf hashes it."""

    def _fetch(self, payload, wanted="7"):
        client = _Client(payload)
        with mock.patch.object(shelf_file, "client_for", lambda label: client):
            return CKPT._fetch_record(wanted), client

    def test_finds_the_row_by_id(self):
        payload = {
            "checkpoints": [{"id": 6, "sha256": None}, {"id": 7, "sha256": None}]
        }
        row, client = self._fetch(payload)
        self.assertEqual(row["id"], 7)
        self.assertEqual(client.paths, ["/api/v1/checkpoints"])

    def test_an_unhashed_checkpoint_is_still_found(self):
        # The whole reason this addresses by id: a 24 GB file is listable long
        # before MissingCheckpointHashFinder has read it.
        row, _ = self._fetch({"checkpoints": [{"id": 7}]})
        self.assertEqual(row["id"], 7)

    def test_a_row_that_is_gone_says_so_rather_than_indexerroring(self):
        with self.assertRaises(RuntimeError) as ctx:
            self._fetch({"checkpoints": [{"id": 6}]})
        self.assertIn("not on the shelf any more", str(ctx.exception))

    def test_a_junk_payload_is_a_pixlstash_error(self):
        for payload in ([], None, "nope", {"checkpoints": "nope"}):
            with self.subTest(payload=payload):
                with self.assertRaises(RuntimeError) as ctx:
                    self._fetch(payload)
                self.assertIn("did not return a checkpoint list", str(ctx.exception))

    def test_nothing_selected_is_refused_before_any_request(self):
        def explode(label):
            raise AssertionError("asked the server about an empty selection")

        for value in ("", "   ", "not-a-number"):
            with self.subTest(value=value):
                with mock.patch.object(shelf_file, "client_for", explode):
                    with self.assertRaises(RuntimeError) as ctx:
                        CKPT._fetch_record(value)
                self.assertIn("no checkpoint selected", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()

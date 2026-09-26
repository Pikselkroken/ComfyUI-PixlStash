"""The Workflow Set Loader: which set member goes to which output.

Loading itself is the three shelf loaders' (``load_file`` in each) and is
stubbed here; what is left is picking the set, reading its slots, and refusing
the cases that would otherwise build a graph other than the one the owner made.
"""

import unittest
from unittest import mock

import _bootstrap as boot

shelf_file = boot.load("nodes.shelf_file")
checkpoint_loader = boot.load("nodes.checkpoint_loader")
clip_loader = boot.load("nodes.clip_loader")
vae_loader = boot.load("nodes.vae_loader")
wsl = boot.load("nodes.workflow_set_loader")

NODE = wsl.PixlStashWorkflowSetLoader

CKPT_SHA, TE1, TE2, VAE1, VAE2, LORA = (c * 64 for c in "abcdef")


def _member(sha, slot, kind, on_shelf=True, model_id=None):
    return {
        "sha256": sha,
        "slot": slot,
        "kind": kind,
        "on_shelf": on_shelf,
        "id": model_id,
        "name": f"{slot}-{sha[0]}",
    }


class _Client:
    def __init__(self, payload):
        self.payload = payload
        self.paths = []

    def get(self, path, **kwargs):
        self.paths.append(path)
        return self

    def json(self):
        return self.payload


class ContractTests(unittest.TestCase):
    def test_the_set_output_is_named_like_the_widget_the_js_reads(self):
        # getWiredValue reads the origin widget named like the wired output.
        self.assertEqual(NODE.RETURN_NAMES[-1], "pixlstash_workflow_set")
        self.assertIn("pixlstash_workflow_set", NODE.INPUT_TYPES()["required"])
        self.assertEqual(NODE.RETURN_TYPES[:3], ("MODEL", "CLIP", "VAE"))
        self.assertEqual(len(NODE.OUTPUT_TOOLTIPS), len(NODE.RETURN_NAMES))

    def test_it_asks_for_the_server_that_has_hand_made_sets(self):
        seen = {}

        def client_for(label, *, min_server_version="1.10.0"):
            seen["min"] = min_server_version
            return _Client({"hand_made": [{"id": 3, "members": []}]})

        with mock.patch.object(shelf_file, "client_for", client_for):
            self.assertEqual(wsl.fetch_set("3")["id"], 3)
        self.assertEqual(seen["min"], "1.12.0")


class LoadTests(unittest.TestCase):
    def setUp(self):
        self.resolved = []
        self.clip_calls = []
        self.vae_calls = []
        self.ckpt_flags = []

        def load_ckpt(path, *, output_clip=True, output_vae=True):
            self.ckpt_flags.append((output_clip, output_vae))
            return ("MODEL", "CKPT_CLIP", "CKPT_VAE")

        def resolve(sha, *, label, folder_key, download=True):
            self.resolved.append((sha, folder_key))
            return {"sha256": sha}, f"/models/{sha[:4]}.safetensors"

        def load_clip(paths, clip_type):
            self.clip_calls.append((paths, clip_type))
            return "SET_CLIP"

        def load_vae(path):
            self.vae_calls.append(path)
            return "SET_VAE"

        for target, name, fake in (
            (shelf_file, "resolve", resolve),
            (
                checkpoint_loader,
                "load_file",
                load_ckpt,
            ),
            (
                checkpoint_loader,
                "local_copy",
                lambda r, label: "/models/ckpt.safetensors",
            ),
            (
                checkpoint_loader.PixlStashCheckpointLoader,
                "_fetch_record",
                staticmethod(lambda i, label: {"id": int(i), "sha256": CKPT_SHA}),
            ),
            (clip_loader, "load_files", load_clip),
            (clip_loader, "_encoder_folder", lambda: "text_encoders"),
            (vae_loader, "load_file", load_vae),
        ):
            patcher = mock.patch.object(target, name, fake)
            patcher.start()
            self.addCleanup(patcher.stop)

    def _run(self, members, clip_type="flux"):
        entry = {"id": 3, "members": members}
        with mock.patch.object(wsl, "fetch_set", lambda set_id: entry):
            return NODE().load_set("My set #3", clip_type)

    def test_a_checkpoint_alone_passes_its_own_clip_and_vae_through(self):
        out = self._run([_member(CKPT_SHA, "checkpoint", "checkpoint", model_id=9)])
        self.assertEqual(out["result"], ("MODEL", "CKPT_CLIP", "CKPT_VAE", "3"))
        self.assertEqual(self.ckpt_flags, [(True, True)])
        models = out["ui"]["pixlstash_lock"][0]["models"]
        self.assertEqual(models, [{"kind": "checkpoint", "sha256": CKPT_SHA, "id": 9}])

    def test_the_sets_encoders_and_vae_replace_the_checkpoints(self):
        out = self._run(
            [
                _member(CKPT_SHA, "checkpoint", "checkpoint", model_id=9),
                _member(TE1, "text_encoder", "text_encoder"),
                _member(TE2, "text_encoder", "text_encoder"),
                _member(VAE1, "vae", "vae"),
                _member(LORA, "lora", "adapter"),
            ]
        )
        self.assertEqual(out["result"], ("MODEL", "SET_CLIP", "SET_VAE", "3"))
        # The checkpoint's own CLIP and VAE are not built just to be dropped.
        self.assertEqual(self.ckpt_flags, [(False, False)])
        self.assertEqual(
            self.clip_calls,
            [(["/models/bbbb.safetensors", "/models/cccc.safetensors"], "flux")],
        )
        # LoRAs are the Adapter Loader's job; nothing resolves one here.
        self.assertNotIn(LORA, [sha for sha, _ in self.resolved])
        kinds = [m["kind"] for m in out["ui"]["pixlstash_lock"][0]["models"]]
        self.assertEqual(kinds, ["checkpoint", "clip", "clip", "vae"])

    def test_of_two_vaes_the_first_listed_is_loaded(self):
        self._run(
            [
                _member(CKPT_SHA, "checkpoint", "checkpoint", model_id=9),
                _member(VAE1, "vae", "vae"),
                _member(VAE2, "vae", "vae"),
            ]
        )
        self.assertEqual(self.vae_calls, ["/models/dddd.safetensors"])

    def test_an_unclassified_checkpoint_is_fetched_as_a_diffusion_model(self):
        self._run([_member(CKPT_SHA, "checkpoint", "unknown", model_id=9)])
        self.assertEqual(self.resolved, [(CKPT_SHA, "diffusion_models")])

    def test_a_checkpoint_member_re_kinded_to_something_else_is_refused(self):
        with self.assertRaises(RuntimeError) as ctx:
            self._run([_member(CKPT_SHA, "checkpoint", "vae", model_id=9)])
        self.assertIn("filed on the shelf as a vae", str(ctx.exception))
        self.assertEqual(self.resolved, [])

    def test_a_set_with_no_checkpoint_is_refused(self):
        with self.assertRaises(RuntimeError) as ctx:
            self._run([_member(VAE1, "vae", "vae")])
        self.assertIn("no checkpoint", str(ctx.exception))

    def test_a_member_that_left_the_shelf_is_refused_not_skipped(self):
        with self.assertRaises(RuntimeError) as ctx:
            self._run(
                [
                    _member(CKPT_SHA, "checkpoint", "checkpoint", model_id=9),
                    _member(VAE1, "vae", None, on_shelf=False),
                ]
            )
        self.assertIn("no longer on the PixlStash shelf", str(ctx.exception))

    def test_nothing_selected_is_refused_before_any_request(self):
        def explode(*a, **k):
            raise AssertionError("asked the server about an empty selection")

        with mock.patch.object(shelf_file, "client_for", explode):
            for value in ("", "(loading…)", "— None —"):
                with self.subTest(value=value):
                    with self.assertRaises(RuntimeError) as ctx:
                        NODE().load_set(value, "flux")
                    self.assertIn("no workflow set selected", str(ctx.exception))


class CacheKeyTests(unittest.TestCase):
    """A set id can mean other files tomorrow, so it is not the cache key."""

    def _key(self, members):
        entry = {"id": 3, "members": members}
        with mock.patch.object(wsl, "fetch_set", lambda set_id: entry):
            return NODE.IS_CHANGED("My set #3", "flux")

    def test_editing_the_set_changes_the_key(self):
        before = [_member(CKPT_SHA, "checkpoint", "checkpoint")]
        after = before + [_member(VAE1, "vae", "vae")]
        self.assertEqual(self._key(before), self._key(list(before)))
        self.assertNotEqual(self._key(before), self._key(after))

    def test_a_failed_lookup_never_matches_so_load_set_reports_it(self):
        def fail(set_id):
            raise RuntimeError("gone")

        with mock.patch.object(wsl, "fetch_set", fail):
            key = NODE.IS_CHANGED("My set #3", "flux")
        self.assertNotEqual(key, key)


if __name__ == "__main__":
    unittest.main()

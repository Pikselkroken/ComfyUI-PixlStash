"""PixlStash Apply Adapter — the guards around ComfyUI's LoRA API.

The node is three calls into ``comfy.utils`` / ``comfy.sd``, which don't exist
outside ComfyUI, so those two are stubbed here.  What is worth pinning is
everything around them: that an unwired path is refused, that a zero-strength
graph is a genuine no-op, that CLIP is left alone when nothing was wired, and
that the state-dict cache keys on the path rather than handing back whichever
adapter happened to be loaded last.
"""

import sys
import types
import unittest

import _bootstrap as boot


def _install_comfy_stubs():
    """Minimal ``comfy.utils`` / ``comfy.sd``, recording what they were given."""
    calls = {"loads": [], "applies": [], "safe_load": []}

    comfy = sys.modules.get("comfy") or types.ModuleType("comfy")
    comfy.__path__ = []

    utils = types.ModuleType("comfy.utils")

    def load_torch_file(path, safe_load=False, **kwargs):
        calls["loads"].append(path)
        calls["safe_load"].append(safe_load)
        return {"state_dict_for": path}

    utils.load_torch_file = load_torch_file

    sd = types.ModuleType("comfy.sd")

    def load_lora_for_models(model, clip, lora, strength_model, strength_clip):
        calls["applies"].append((model, clip, lora, strength_model, strength_clip))
        return (f"{model}+patched", None if clip is None else f"{clip}+patched")

    sd.load_lora_for_models = load_lora_for_models

    # Both in sys.modules and as attributes: `import comfy.utils` only sets the
    # attribute on the parent when it actually performs the import, and a
    # pre-seeded sys.modules entry skips that.
    comfy.utils = utils
    comfy.sd = sd
    sys.modules["comfy"] = comfy
    sys.modules["comfy.utils"] = utils
    sys.modules["comfy.sd"] = sd
    return calls


CALLS = _install_comfy_stubs()
adapter_applier = boot.load("nodes.adapter_applier")


def new_node():
    for v in CALLS.values():
        v.clear()
    return adapter_applier.PixlStashApplyAdapter()


class EmptyPathTests(unittest.TestCase):
    def test_an_unwired_path_is_refused(self):
        with self.assertRaises(RuntimeError) as ctx:
            new_node().apply_adapter("MODEL", "", 1.0)
        self.assertIn("lora_path is empty", str(ctx.exception))

    def test_whitespace_only_is_refused(self):
        with self.assertRaises(RuntimeError):
            new_node().apply_adapter("MODEL", "   ", 1.0)

    def test_refused_regardless_of_the_strength_sliders(self):
        # The check sits before the zero-strength short-circuit, so the same
        # broken graph doesn't pass or fail depending on a slider.
        with self.assertRaises(RuntimeError):
            new_node().apply_adapter("MODEL", "", 0.0, clip="CLIP", strength_clip=0.0)


class ShortCircuitTests(unittest.TestCase):
    def test_zero_strengths_load_nothing(self):
        out = new_node().apply_adapter(
            "MODEL", "/a.safetensors", 0.0, clip="CLIP", strength_clip=0.0
        )
        self.assertEqual(out, ("MODEL", "CLIP"))
        self.assertEqual(CALLS["loads"], [], "read a file it had no reason to read")

    def test_zero_model_strength_still_patches_clip(self):
        node = new_node()
        out = node.apply_adapter(
            "MODEL", "/a.safetensors", 0.0, clip="CLIP", strength_clip=1.0
        )
        self.assertEqual(out, ("MODEL+patched", "CLIP+patched"))
        self.assertEqual(len(CALLS["applies"]), 1)

    def test_zero_model_strength_with_no_clip_is_a_no_op(self):
        out = new_node().apply_adapter("MODEL", "/a.safetensors", 0.0)
        self.assertEqual(out, ("MODEL", None))
        self.assertEqual(CALLS["loads"], [])


class ApplyTests(unittest.TestCase):
    def test_strengths_are_passed_through_separately(self):
        new_node().apply_adapter(
            "MODEL", "/a.safetensors", 0.8, clip="CLIP", strength_clip=0.25
        )
        _, _, _, sm, sc = CALLS["applies"][0]
        self.assertEqual((sm, sc), (0.8, 0.25))

    def test_no_clip_wired_passes_none_through(self):
        model, clip = new_node().apply_adapter("MODEL", "/a.safetensors", 1.0)
        self.assertEqual(model, "MODEL+patched")
        self.assertIsNone(clip)
        self.assertIsNone(CALLS["applies"][0][1], "invented a CLIP that wasn't wired")


class SafeLoadTests(unittest.TestCase):
    """The weights file is read with pickle disabled, always.

    `lora_path` is a wire — the Adapter Loader hands it a path the *server*
    chose, and the docstring invites paths from other packs. Without
    `safe_load=True`, ComfyUI falls back to `torch.load` for anything that is
    not safetensors, which executes pickle opcodes out of that file.
    """

    def test_pickle_is_never_enabled(self):
        node = new_node()
        node.apply_adapter("MODEL", "/a.safetensors", 1.0)
        node.apply_adapter("MODEL", "/b.ckpt", 1.0, clip="CLIP")
        self.assertEqual(CALLS["safe_load"], [True, True])


class CacheTests(unittest.TestCase):
    def test_the_same_path_is_read_once(self):
        node = new_node()
        node.apply_adapter("MODEL", "/a.safetensors", 1.0)
        node.apply_adapter("MODEL", "/a.safetensors", 0.5)
        self.assertEqual(CALLS["loads"], ["/a.safetensors"])

    def test_a_different_path_is_re_read(self):
        # Without a path comparison the cache would serve adapter A's weights
        # for adapter B — wrong output, no error.
        node = new_node()
        node.apply_adapter("MODEL", "/a.safetensors", 1.0)
        node.apply_adapter("MODEL", "/b.safetensors", 1.0)
        self.assertEqual(CALLS["loads"], ["/a.safetensors", "/b.safetensors"])
        self.assertEqual(CALLS["applies"][1][2], {"state_dict_for": "/b.safetensors"})

    def test_the_outgoing_state_dict_is_released_before_the_next_is_read(self):
        node = new_node()
        node.apply_adapter("MODEL", "/a.safetensors", 1.0)

        seen = {}
        utils = sys.modules["comfy.utils"]
        original = utils.load_torch_file

        def observing_load(path, safe_load=False, **kwargs):
            # What the node is holding at the moment it starts reading the new
            # file: both resident here would double peak memory on a switch.
            seen["cached"] = node._applier._cached
            return original(path, safe_load=safe_load, **kwargs)

        utils.load_torch_file = observing_load
        try:
            node.apply_adapter("MODEL", "/b.safetensors", 1.0)
        finally:
            utils.load_torch_file = original
        self.assertIsNone(seen["cached"])


if __name__ == "__main__":
    unittest.main()

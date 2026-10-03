"""GGUF: which loader each file goes to, and which nodes take one at all.

The loading itself is the vendored ComfyUI-GGUF's and needs torch, ``gguf`` and
a real model, so it is stubbed here. What is pinned is the routing by suffix,
the refusal of a GGUF by the nodes that cannot load one (before any download),
and the two error messages a user will actually see.
"""

import hashlib
import os
import shutil
import sys
import tempfile
import types
import unittest
from unittest import mock

import _bootstrap as boot

shelf_file = boot.load("nodes.shelf_file")
gguf_support = boot.load("nodes.gguf_support")
clip_loader = boot.load("nodes.clip_loader")
checkpoint_loader = boot.load("nodes.checkpoint_loader")

SHA = "c" * 64


def _comfy():
    """Stub ``comfy.sd`` / ``folder_paths`` modules, and the core load calls made."""
    calls = []
    sd = types.ModuleType("comfy.sd")
    sd.CLIPType = types.SimpleNamespace(STABLE_DIFFUSION="SD", FLUX="FLUX")
    sd.load_clip = lambda **kw: calls.append(("core_clip", kw["ckpt_paths"])) or "CORE"
    comfy = types.ModuleType("comfy")
    comfy.__path__ = []
    comfy.sd = sd
    fp = types.ModuleType("folder_paths")
    fp.get_folder_paths = lambda kind: []
    modules = {"comfy": comfy, "comfy.sd": sd, "folder_paths": fp}
    return mock.patch.dict(sys.modules, modules), calls


class RoutingTests(unittest.TestCase):
    def setUp(self):
        patcher, self.core = _comfy()
        patcher.start()
        self.addCleanup(patcher.stop)
        self.gguf = []

    def _clip(self, paths):
        def load_clip(paths, clip_type, *, label):
            self.gguf.append((paths, clip_type, label))
            return "GGUF"

        with mock.patch.object(gguf_support, "load_clip", load_clip):
            return clip_loader.load_files(paths, "flux")

    def test_any_gguf_encoder_sends_the_whole_clip_to_gguf(self):
        for paths in (["/m/t5.gguf"], ["/m/t5.gguf", "/m/clip_l.safetensors"]):
            with self.subTest(paths=paths):
                self.gguf.clear()
                self.assertEqual(self._clip(paths), "GGUF")
                self.assertEqual(self.gguf, [(paths, "FLUX", clip_loader.LABEL)])
        self.assertEqual(self.core, [])

    def test_safetensors_encoders_stay_on_core_comfyui(self):
        self.assertEqual(self._clip(["/m/clip_l.safetensors"]), "CORE")
        self.assertEqual(self.core, [("core_clip", ["/m/clip_l.safetensors"])])
        self.assertEqual(self.gguf, [])

    def test_a_gguf_checkpoint_is_a_unet_with_no_clip_or_vae(self):
        seen = []

        def load_unet(path, *, label):
            seen.append((path, label))
            return "UNET"

        with mock.patch.object(gguf_support, "load_unet", load_unet):
            out = checkpoint_loader.load_file("/m/flux.gguf", label="Set")
        self.assertEqual(out, ("UNET", None, None))
        self.assertEqual(seen, [("/m/flux.gguf", "Set")])


class NodeWiringTests(unittest.TestCase):
    """The CLIP and Checkpoint nodes ask the shelf for GGUF; the VAE node does not."""

    def test_the_clip_node_resolves_gguf_encoders(self):
        seen = []

        def resolve(sha, **kw):
            seen.append(kw["suffixes"])
            return {}, "/m/t5.gguf"

        with (
            mock.patch.object(shelf_file, "resolve", resolve),
            mock.patch.object(clip_loader, "_encoder_folder", lambda: "text_encoders"),
            mock.patch.object(clip_loader, "load_files", lambda *a, **k: "CLIP"),
        ):
            clip_loader.PixlStashCLIPLoader().load_clip(SHA, "flux", "d" * 64)
        self.assertEqual(seen, [(".safetensors", ".gguf")] * 2)

    def test_the_checkpoint_node_takes_a_local_gguf(self):
        seen = []

        def local_path(record, *, label, suffixes):
            seen.append(suffixes)
            return "/m/flux.gguf"

        with mock.patch.object(shelf_file, "local_path", local_path):
            checkpoint_loader.local_copy({}, label="X")
        self.assertEqual(seen, [(".safetensors", ".gguf")])

    def test_the_vae_node_keeps_the_safetensors_default(self):
        vae_loader = boot.load("nodes.vae_loader")
        seen = []

        def resolve(sha, **kw):
            seen.append(kw.get("suffixes", shelf_file.SAFETENSORS))
            return {}, "/m/vae.safetensors"

        with (
            mock.patch.object(shelf_file, "resolve", resolve),
            mock.patch.object(vae_loader, "load_file", lambda p: "VAE"),
        ):
            vae_loader.PixlStashVAELoader().load_vae(SHA)
        self.assertEqual(seen, [(".safetensors",)])


class LoadUnetTests(unittest.TestCase):
    """``load_unet`` mirrors upstream's ``UnetLoaderGGUF.load_unet`` defaults."""

    def test_default_dtypes_metadata_and_patcher(self):
        calls = {}

        class Linear:
            dequant_dtype = "unset"
            patch_dtype = "unset"

        class Ops:
            pass

        Ops.Linear = Linear

        class Patcher:
            patch_on_device = "unset"

            @classmethod
            def clone(cls, model):
                calls["cloned"] = model
                return cls()

        vendored = types.SimpleNamespace(
            GGMLOps=Ops,
            GGUFModelPatcher=Patcher,
            gguf_sd_loader=lambda p: ({"w": 1}, {"metadata": {"k": "v"}}),
        )

        def load_sd(sd, model_options=None, metadata=None):
            calls["load"] = (sd, model_options["custom_operations"], metadata)
            return "MODEL"

        patcher, _ = _comfy()
        with patcher:
            sys.modules["comfy.sd"].load_diffusion_model_state_dict = load_sd
            with mock.patch.object(
                gguf_support.importlib, "import_module", lambda *a: vendored
            ):
                model = gguf_support.load_unet("/m/flux.gguf", label="X")
        self.assertIsInstance(model, Patcher)
        self.assertIsNone(model.patch_on_device)
        self.assertEqual(calls["cloned"], "MODEL")
        sd, ops, metadata = calls["load"]
        self.assertEqual((sd, metadata), ({"w": 1}, {"k": "v"}))
        self.assertIsNone(ops.Linear.dequant_dtype)
        self.assertIsNone(ops.Linear.patch_dtype)


class VendoredErrorTests(unittest.TestCase):
    def test_a_missing_gguf_package_names_what_to_install(self):
        def missing(name, package=None):
            raise ModuleNotFoundError("No module named 'gguf'", name="gguf")

        with mock.patch.object(gguf_support.importlib, "import_module", missing):
            with self.assertRaises(RuntimeError) as ctx:
                gguf_support.load_clip(
                    ["/m/t5.gguf"], "FLUX", label="PixlStash CLIP Loader"
                )
        self.assertIn("PixlStash CLIP Loader:", str(ctx.exception))
        self.assertIn("pip install gguf", str(ctx.exception))

    def test_some_other_missing_module_is_not_blamed_on_gguf(self):
        def missing(name, package=None):
            raise ModuleNotFoundError("No module named 'torch'", name="torch")

        with mock.patch.object(gguf_support.importlib, "import_module", missing):
            with self.assertRaises(ModuleNotFoundError):
                gguf_support.load_clip(["/m/t5.gguf"], "FLUX", label="X")

    def _load_clip(self, paths, torch_sd=None, gguf_error=None):
        """``load_clip`` against a stub vendored pack; the readers each path hit."""
        read = []

        def gguf_clip_loader(p):
            read.append(("gguf", p))
            if gguf_error:
                raise gguf_error
            return {"gguf": p}

        def load_torch_file(p, safe_load=False):
            read.append(("torch", p))
            return dict(torch_sd or {"st": p})

        class Loader:
            def load_patcher(self, paths, clip_type, data):
                return ("CLIP", clip_type, data)

        vendored = types.SimpleNamespace(
            CLIPLoaderGGUF=Loader, gguf_clip_loader=gguf_clip_loader
        )
        utils = types.ModuleType("comfy.utils")
        utils.load_torch_file = load_torch_file
        patcher, _ = _comfy()
        with (
            patcher,
            mock.patch.dict(sys.modules, {"comfy.utils": utils}),
            mock.patch.object(
                gguf_support.importlib, "import_module", lambda *a: vendored
            ),
        ):
            sys.modules["comfy"].utils = utils
            out = gguf_support.load_clip(paths, "FLUX", label="My Node")
        return out, read

    def test_an_uppercase_gguf_is_read_as_gguf_beside_a_safetensors(self):
        out, read = self._load_clip(["/m/T5.GGUF", "/m/clip_l.safetensors"])
        self.assertEqual(
            read, [("gguf", "/m/T5.GGUF"), ("torch", "/m/clip_l.safetensors")]
        )
        self.assertEqual(
            out,
            ("CLIP", "FLUX", [{"gguf": "/m/T5.GGUF"}, {"st": "/m/clip_l.safetensors"}]),
        )

    def test_scaled_fp8_beside_gguf_is_refused_with_the_label(self):
        with self.assertRaises(RuntimeError) as ctx:
            self._load_clip(
                ["/m/t5.gguf", "/m/clip_l.safetensors"], torch_sd={"scaled_fp8": 1}
            )
        self.assertTrue(
            str(ctx.exception).startswith(
                "My Node: Mixing scaled FP8 with GGUF is not supported!"
            )
        )
        self.assertIsInstance(ctx.exception.__cause__, NotImplementedError)

    def test_an_upstream_error_comes_back_labelled(self):
        with self.assertRaises(RuntimeError) as ctx:
            self._load_clip(["/m/t5.gguf"], gguf_error=ValueError("bad arch 'pig'"))
        self.assertEqual(str(ctx.exception), "My Node: bad arch 'pig'")
        self.assertIsInstance(ctx.exception.__cause__, ValueError)


class SuffixTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="pixlstash_gguf_test_")
        self.addCleanup(shutil.rmtree, self.tmp, True)

    def _record(self, name, body=b"gguf-bytes"):
        with open(os.path.join(self.tmp, name), "wb") as fh:
            fh.write(body)
        return {
            "sha256": SHA,
            "filename": name,
            "file_size": len(body),
            "locations": [
                {"folder_path": self.tmp, "relpath": name, "state": "present"}
            ],
        }

    def test_a_local_gguf_is_used_only_by_a_node_that_takes_one(self):
        record = self._record("t5.gguf")
        self.assertIsNone(shelf_file.local_path(record))
        self.assertEqual(
            shelf_file.local_path(record, suffixes=(".safetensors", ".gguf")),
            os.path.join(self.tmp, "t5.gguf"),
        )

    def _resolve(self, record, **kw):
        downloads = []

        def cached_download(client, sha, **k):
            downloads.append(k["suffix"])
            return "/cache/x"

        with (
            mock.patch.object(shelf_file, "client_for", lambda label: object()),
            mock.patch.object(shelf_file, "fetch_record", lambda c, s, label: record),
            mock.patch.object(shelf_file, "cached_download", cached_download),
        ):
            shelf_file.resolve(
                SHA, label="PixlStash VAE Loader", folder_key="vae", **kw
            )
        return downloads

    def test_a_gguf_record_is_refused_by_default_before_any_download(self):
        record = {"sha256": SHA, "filename": "vae.gguf", "locations": []}
        with self.assertRaises(RuntimeError) as ctx:
            self._resolve(record)
        self.assertIn(
            "PixlStash VAE Loader: “vae.gguf” is a .gguf file", str(ctx.exception)
        )

    def test_a_gguf_record_downloads_under_its_own_suffix(self):
        record = {"sha256": SHA, "filename": "T5.GGUF", "locations": []}
        self.assertEqual(
            self._resolve(record, suffixes=(".safetensors", ".gguf")), [".gguf"]
        )
        record = {"sha256": SHA, "filename": "clip_l.safetensors", "locations": []}
        self.assertEqual(self._resolve(record), [".safetensors"])

    def test_the_cache_file_carries_the_suffix_it_was_given(self):
        body = b"gguf-bytes" * 10
        sha = hashlib.sha256(body).hexdigest()

        class Client:
            def get(self, path, stream=False):
                return self

            def iter_content(self, chunk_size):
                return [body]

            def close(self):
                pass

        fp = types.ModuleType("folder_paths")
        fp.get_folder_paths = lambda kind: [self.tmp]
        with mock.patch.dict(sys.modules, {"folder_paths": fp}):
            path = shelf_file.cached_download(
                Client(), sha, folder_key="text_encoders", label="X", suffix=".gguf"
            )
        self.assertEqual(path, os.path.join(self.tmp, "pixlstash", f"{sha}.gguf"))
        self.assertTrue(os.path.isfile(path))


if __name__ == "__main__":
    unittest.main()

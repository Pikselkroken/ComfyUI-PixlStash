"""GGUF loading: which reader each file reaches, and what a user is told.

The vendored ComfyUI-GGUF needs torch and the ``gguf`` package, neither of
which is in the test venv, so ``gguf_support``'s view of it is a stub module
installed where the vendored ``nodes.py`` would be imported from. What is under
test is ours: the routing by extension, the suffix rules in ``shelf_file``, the
labelled errors, and ``load_unet``'s copy of upstream's steps.
"""

import builtins
import contextlib
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

VENDORED = f"{boot.PKG}.vendor.comfyui_gguf.nodes"
LABEL = "Some Node"
SHA = "a" * 64


def _comfy(**sd_attrs):
    """sys.modules entries for a ``comfy.sd`` stub carrying ``sd_attrs``."""
    comfy = types.ModuleType("comfy")
    comfy.__path__ = []
    sd = types.ModuleType("comfy.sd")

    class CLIPType:
        STABLE_DIFFUSION = "SD"
        FLUX = "FLUX"

    sd.CLIPType = CLIPType
    for name, value in sd_attrs.items():
        setattr(sd, name, value)
    comfy.sd = sd
    fp = types.ModuleType("folder_paths")
    fp.get_folder_paths = lambda kind: []
    return {"comfy": comfy, "comfy.sd": sd, "folder_paths": fp}


@contextlib.contextmanager
def vendored(module):
    """Serve ``module`` as the vendored ``nodes.py`` for the block."""
    pkg = sys.modules.get(f"{boot.PKG}.vendor.comfyui_gguf")
    with mock.patch.dict(sys.modules, {VENDORED: module}):
        if pkg is not None and hasattr(pkg, "nodes"):
            with mock.patch.object(pkg, "nodes", module):
                yield
        else:
            yield


class RoutingTests(unittest.TestCase):
    """Any GGUF goes to gguf_support; a safetensors-only load never does."""

    def setUp(self):
        self.calls = []

        def core_clip(ckpt_paths, embedding_directory=None, clip_type=None):
            self.calls.append(("core", list(ckpt_paths), clip_type))
            return "CORE_CLIP"

        def gguf_clip(paths, clip_type, *, label):
            self.calls.append(("gguf", list(paths), clip_type, label))
            return "GGUF_CLIP"

        def gguf_unet(path, *, label):
            self.calls.append(("unet", path, label))
            return "GGUF_MODEL"

        def guess(*a, **k):
            raise AssertionError("a GGUF reached load_checkpoint_guess_config")

        patches = [
            mock.patch.dict(
                sys.modules,
                _comfy(load_clip=core_clip, load_checkpoint_guess_config=guess),
            ),
            mock.patch.object(gguf_support, "load_clip", gguf_clip),
            mock.patch.object(gguf_support, "load_unet", gguf_unet),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)

    def test_one_gguf_encoder_goes_to_gguf(self):
        out = clip_loader.load_files(["/m/t5.gguf"], "flux")
        self.assertEqual(out, "GGUF_CLIP")
        self.assertEqual(
            self.calls, [("gguf", ["/m/t5.gguf"], "FLUX", clip_loader.LABEL)]
        )

    def test_a_gguf_beside_a_safetensors_goes_to_gguf_together(self):
        # Upstream's load_data reads each file by extension, so the pair stays
        # one call, in widget order.
        clip_loader.load_files(["/m/clip_l.safetensors", "/m/T5.GGUF"], "flux")
        self.assertEqual(
            self.calls,
            [
                (
                    "gguf",
                    ["/m/clip_l.safetensors", "/m/T5.GGUF"],
                    "FLUX",
                    clip_loader.LABEL,
                )
            ],
        )

    def test_safetensors_only_stays_on_core_comfy(self):
        self.assertEqual(
            clip_loader.load_files(["/m/clip_l.safetensors"], "flux"), "CORE_CLIP"
        )
        self.assertEqual(self.calls, [("core", ["/m/clip_l.safetensors"], "FLUX")])

    def test_the_callers_label_reaches_gguf(self):
        clip_loader.load_files(["/m/t5.gguf"], "flux", label="Set Loader")
        self.assertEqual(self.calls[0][3], "Set Loader")

    def test_a_gguf_checkpoint_is_a_model_with_no_clip_or_vae(self):
        out = checkpoint_loader.load_file("/m/flux-Q8.gguf", label="Set Loader")
        self.assertEqual(out, ("GGUF_MODEL", None, None))
        self.assertEqual(self.calls, [("unet", "/m/flux-Q8.gguf", "Set Loader")])


class _Resp:
    def __init__(self, payload=None, body=b""):
        self.payload = payload
        self.body = body

    def json(self):
        return self.payload

    def iter_content(self, chunk_size):
        yield self.body

    def close(self):
        pass


class _Client:
    def __init__(self, record, body=b""):
        self.record = record
        self.body = body
        self.paths = []

    def get(self, path, **kwargs):
        self.paths.append(path)
        if path.endswith("/file"):
            return _Resp(body=self.body)
        return _Resp(payload=self.record)


class SuffixTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="pixlstash_gguf_test_")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        with open(os.path.join(self.tmp, "t5.gguf"), "wb") as fh:
            fh.write(b"x")

    def _record(self, filename="t5.gguf"):
        return {
            "sha256": SHA,
            "filename": filename,
            "file_size": 1,
            "locations": [
                {"folder_path": self.tmp, "relpath": filename, "state": "present"}
            ],
        }

    def test_a_local_gguf_is_used_only_where_the_node_takes_gguf(self):
        record = self._record()
        self.assertIsNone(shelf_file.local_path(record))
        self.assertEqual(
            shelf_file.local_path(record, suffixes=shelf_file.WITH_GGUF),
            os.path.join(self.tmp, "t5.gguf"),
        )

    def test_a_safetensors_only_node_refuses_a_gguf_before_any_download(self):
        client = _Client(self._record())

        def explode(*a, **k):
            raise AssertionError("downloaded a file this node cannot load")

        with (
            mock.patch.object(shelf_file, "client_for", lambda label: client),
            mock.patch.object(shelf_file, "cached_download", explode),
        ):
            with self.assertRaises(RuntimeError) as ctx:
                shelf_file.resolve(SHA, label="PixlStash VAE Loader", folder_key="vae")
        self.assertIn("PixlStash VAE Loader", str(ctx.exception))
        self.assertIn(".gguf file, which this node does not load", str(ctx.exception))
        self.assertEqual(client.paths, [f"/api/v1/adapters/{SHA}"])

    def test_a_fetched_gguf_is_cached_under_its_own_suffix(self):
        body = b"GGUF-bytes"
        sha = hashlib.sha256(body).hexdigest()
        record = {"sha256": sha, "filename": "t5.gguf", "locations": []}
        client = _Client(record, body)
        fp = types.ModuleType("folder_paths")
        fp.get_folder_paths = lambda kind: [self.tmp]
        with (
            mock.patch.dict(sys.modules, {"folder_paths": fp}),
            mock.patch.object(shelf_file, "client_for", lambda label: client),
        ):
            _record, path = shelf_file.resolve(
                sha,
                label=LABEL,
                folder_key="text_encoders",
                suffixes=shelf_file.WITH_GGUF,
            )
        self.assertEqual(path, os.path.join(self.tmp, "pixlstash", f"{sha}.gguf"))
        with open(path, "rb") as fh:
            self.assertEqual(fh.read(), body)

    def test_a_safetensors_record_still_caches_as_safetensors(self):
        body = b"st-bytes"
        sha = hashlib.sha256(body).hexdigest()
        client = _Client({"sha256": sha, "filename": "vae.safetensors"}, body)
        fp = types.ModuleType("folder_paths")
        fp.get_folder_paths = lambda kind: [self.tmp]
        with (
            mock.patch.dict(sys.modules, {"folder_paths": fp}),
            mock.patch.object(shelf_file, "client_for", lambda label: client),
        ):
            _record, path = shelf_file.resolve(sha, label=LABEL, folder_key="vae")
        self.assertTrue(path.endswith(f"{sha}.safetensors"))


class MissingPackageTests(unittest.TestCase):
    def _import_failing_with(self, missing):
        real = builtins.__import__

        def fake(name, globals=None, locals=None, fromlist=(), level=0):
            if level and name.endswith("vendor.comfyui_gguf"):
                raise ModuleNotFoundError(f"No module named {missing!r}", name=missing)
            return real(name, globals, locals, fromlist, level)

        return mock.patch.object(builtins, "__import__", fake)

    def test_a_missing_gguf_package_says_what_to_install(self):
        with self._import_failing_with("gguf"):
            with self.assertRaises(RuntimeError) as ctx:
                gguf_support.load_clip(["/m/t5.gguf"], "FLUX", label=LABEL)
        self.assertTrue(str(ctx.exception).startswith(f"{LABEL}: "))
        self.assertIn("pip install gguf", str(ctx.exception))

    def test_anything_else_missing_is_not_blamed_on_gguf(self):
        with self._import_failing_with("torch"):
            with self.assertRaises(ModuleNotFoundError) as ctx:
                gguf_support.load_clip(["/m/t5.gguf"], "FLUX", label=LABEL)
        self.assertEqual(ctx.exception.name, "torch")


class ClipTests(unittest.TestCase):
    def _module(self, load_data):
        calls = {}

        class CLIPLoaderGGUF:
            def load_data(self, paths):
                return load_data(paths)

            def load_patcher(self, paths, clip_type, data):
                calls["patcher"] = (paths, clip_type, data)
                return "CLIP"

        mod = types.ModuleType("vendored_nodes")
        mod.CLIPLoaderGGUF = CLIPLoaderGGUF
        return mod, calls

    def test_the_paths_and_type_reach_upstream(self):
        mod, calls = self._module(lambda paths: ["sd"] * len(paths))
        with vendored(mod):
            out = gguf_support.load_clip(
                ["/a.gguf", "/b.safetensors"], "FLUX", label=LABEL
            )
        self.assertEqual(out, "CLIP")
        self.assertEqual(
            calls["patcher"], (["/a.gguf", "/b.safetensors"], "FLUX", ["sd", "sd"])
        )

    def test_an_upstream_refusal_comes_back_labelled(self):
        def refuse(paths):
            raise NotImplementedError("Mixing scaled FP8 with GGUF is not supported!")

        mod, _ = self._module(refuse)
        with vendored(mod):
            with self.assertRaises(RuntimeError) as ctx:
                gguf_support.load_clip(["/a.gguf"], "FLUX", label=LABEL)
        self.assertEqual(
            str(ctx.exception),
            f"{LABEL}: Mixing scaled FP8 with GGUF is not supported!",
        )
        self.assertIsInstance(ctx.exception.__cause__, NotImplementedError)


class UnetTests(unittest.TestCase):
    """``load_unet`` repeats upstream's UnetLoaderGGUF.load_unet steps."""

    def _module(self):
        seen = {}

        class Linear:
            dequant_dtype = "unset"
            patch_dtype = "unset"

        class GGMLOps:
            def __init__(self):
                self.Linear = Linear
                seen["ops"] = self

        class GGUFModelPatcher:
            @staticmethod
            def clone(model):
                seen["cloned"] = model
                return types.SimpleNamespace(of=model, patch_on_device="unset")

        def gguf_sd_loader(path):
            seen["path"] = path
            return {"w": 1}, {"metadata": {"arch": "flux"}}

        mod = types.ModuleType("vendored_nodes")
        mod.GGMLOps = GGMLOps
        mod.GGUFModelPatcher = GGUFModelPatcher
        mod.gguf_sd_loader = gguf_sd_loader
        return mod, seen

    def _run(self, loader):
        mod, seen = self._module()
        with (
            vendored(mod),
            mock.patch.dict(
                sys.modules, _comfy(load_diffusion_model_state_dict=loader)
            ),
        ):
            return gguf_support.load_unet("/m/flux.gguf", label=LABEL), seen

    def test_the_model_is_built_with_ggml_ops_and_wrapped_as_upstream_does(self):
        calls = {}

        def loader(sd, model_options=None, metadata=None):
            calls.update(sd=sd, options=model_options, metadata=metadata)
            return "PATCHER"

        model, seen = self._run(loader)
        self.assertEqual(seen["path"], "/m/flux.gguf")
        self.assertIsNone(seen["ops"].Linear.dequant_dtype)
        self.assertIsNone(seen["ops"].Linear.patch_dtype)
        self.assertEqual(calls["sd"], {"w": 1})
        self.assertIs(calls["options"]["custom_operations"], seen["ops"])
        self.assertEqual(calls["metadata"], {"arch": "flux"})
        self.assertEqual(seen["cloned"], "PATCHER")
        self.assertEqual(model.of, "PATCHER")
        self.assertIsNone(model.patch_on_device)

    def test_metadata_is_left_out_for_a_comfy_that_does_not_take_it(self):
        def loader(sd, model_options=None):
            return "PATCHER"

        model, _ = self._run(loader)
        self.assertEqual(model.of, "PATCHER")

    def test_an_unrecognised_model_is_a_labelled_error(self):
        with self.assertRaises(RuntimeError) as ctx:
            self._run(lambda sd, model_options=None: None)
        self.assertTrue(str(ctx.exception).startswith(f"{LABEL}: "))
        self.assertIn("/m/flux.gguf", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()

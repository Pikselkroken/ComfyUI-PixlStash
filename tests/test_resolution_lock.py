"""The resolution lock: what a node reports a run was actually locked to.

The payload is a contract with PixlStash, which reads it out of ComfyUI's
history, so the shape is pinned here rather than left to the eye. Two pieces of
real logic sit behind it and both fail quietly if they break:

* the Picture Loader reports the pictures that **loaded**, not the ones that
  were asked for — a picture that 404s is skipped from the batch, and leaving
  it in the lock would tell PixlStash the render used an image it never saw;
* the CLIP loader reports **both** encoders, in widget order, because which
  file sat in which slot is part of what was run.

A lock that is merely wrong still looks like a lock, so an assertion on the
count alone would pass with either bug in place.
"""

import sys
import types
import unittest
from unittest import mock

import _bootstrap as boot

lock = boot.load("nodes.lock")
shelf_file = boot.load("nodes.shelf_file")
clip_loader = boot.load("nodes.clip_loader")

SHA_A = "a" * 64
SHA_B = "b" * 64


# ---------------------------------------------------------------------------
# Stubs for the tensor plumbing the picture loader does around the ids.
# ---------------------------------------------------------------------------


class _Arr:
    """Stands in for a numpy array: divisible, and that is all that is done."""

    def __truediv__(self, other):
        return self


class _Tensor:
    def unsqueeze(self, dim):
        return self


def _install_tensor_stubs():
    numpy = types.ModuleType("numpy")
    numpy.uint8 = "uint8"
    numpy.float32 = "float32"
    numpy.array = lambda obj, dtype=None: _Arr()
    numpy.zeros = lambda shape, dtype=None: _Arr()

    torch = types.ModuleType("torch")
    torch.from_numpy = lambda arr: _Tensor()
    torch.cat = lambda parts, dim=0: f"batch of {len(parts)}"

    pil = types.ModuleType("PIL")

    class _Image:
        LANCZOS = "lanczos"
        NEAREST = "nearest"
        Image = object

        @staticmethod
        def fromarray(arr, mode=None):
            return arr

    pil.Image = _Image
    sys.modules.update(
        {"numpy": numpy, "torch": torch, "PIL": pil, "PIL.Image": pil.Image}
    )


class _Pil:
    """A decoded picture, already the reference size so no resize is attempted."""

    size = (8, 8)

    def convert(self, mode):
        return self


_install_tensor_stubs()
picture_loader = boot.load("nodes.picture_loader")


class PayloadShapeTests(unittest.TestCase):
    def test_report_wraps_the_outputs_and_names_the_ui_key(self):
        out = lock.report(("a", "b"), pictures=[3], models=[{"kind": "vae"}])
        self.assertEqual(out["result"], ("a", "b"))
        self.assertEqual(
            out["ui"],
            {lock.UI_KEY: [{"pictures": [3], "models": [{"kind": "vae"}]}]},
        )

    def test_a_node_that_locked_nothing_still_answers_both_keys(self):
        # PixlStash indexes into these; an absent key and an empty one are
        # different bugs to chase, so they are never absent.
        payload = lock.report(("x",))["ui"][lock.UI_KEY][0]
        self.assertEqual(payload, {"pictures": [], "models": []})

    def test_both_shelf_identifiers_are_always_present_and_nullable(self):
        # A hash-addressed file has no row id here; a checkpoint has no digest
        # until the shelf's hasher reaches it. Either may be null, neither is
        # ever missing.
        self.assertEqual(
            lock.shelf_model("adapter", sha256=SHA_A),
            {"kind": "adapter", "sha256": SHA_A, "id": None},
        )
        self.assertEqual(
            lock.shelf_model("checkpoint", sha256=None, row_id=7),
            {"kind": "checkpoint", "sha256": None, "id": 7},
        )

    def test_an_empty_digest_is_reported_as_null_not_as_an_empty_string(self):
        self.assertIsNone(lock.shelf_model("vae", sha256="")["sha256"])


class PictureLockTests(unittest.TestCase):
    """The lock is the batch, not the request."""

    def _load(self, ids, missing=()):
        node = picture_loader.PixlStashPictureLoader()

        def fake_fetch(client, pid):
            if pid in missing:
                raise RuntimeError(f"PixlStash: not found — picture {pid}")
            return (_Pil(), _Arr())

        with (
            mock.patch.object(
                picture_loader,
                "read_credentials",
                lambda *a: ("https://vault.example", "t", True),
            ),
            mock.patch.object(picture_loader, "make_client", lambda *a: object()),
            mock.patch.object(
                picture_loader.PixlStashPictureLoader,
                "_fetch_image",
                staticmethod(fake_fetch),
            ),
        ):
            return node.load_pictures(",".join(str(i) for i in ids))

    def test_reports_every_picture_that_loaded(self):
        out = self._load([11, 12])
        self.assertEqual(out["ui"][lock.UI_KEY][0]["pictures"], [11, 12])
        # …and the batch_size output agrees with it.
        self.assertEqual(out["result"][5], 2)

    def test_a_picture_that_could_not_be_fetched_is_not_in_the_lock(self):
        # The one that matters: 12 is gone from the vault, so the render did
        # not use it and PixlStash must not be told that it did.
        out = self._load([11, 12, 13], missing={12})
        self.assertEqual(out["ui"][lock.UI_KEY][0]["pictures"], [11, 13])
        self.assertEqual(out["result"][5], 2)

    def test_nothing_is_locked_to_a_model(self):
        self.assertEqual(self._load([11])["ui"][lock.UI_KEY][0]["models"], [])


class ClipLockTests(unittest.TestCase):
    """A pair of text encoders is two locks, in widget order."""

    def setUp(self):
        comfy = types.ModuleType("comfy")
        comfy.__path__ = []
        sd = types.ModuleType("comfy.sd")

        class CLIPType:
            STABLE_DIFFUSION = "sd"

        sd.CLIPType = CLIPType
        sd.load_clip = lambda ckpt_paths, embedding_directory=None, clip_type=None: (
            "CLIP"
        )
        comfy.sd = sd

        fp = types.ModuleType("folder_paths")
        fp.get_folder_paths = lambda kind: ["/models/text_encoders"]

        patch = mock.patch.dict(
            sys.modules, {"comfy": comfy, "comfy.sd": sd, "folder_paths": fp}
        )
        patch.start()
        self.addCleanup(patch.stop)

    def _models(self, first, second, shelf_digest=None):
        def fake_resolve(sha, **kwargs):
            return ({"sha256": shelf_digest} if shelf_digest else {}, f"/m/{sha}.st")

        with mock.patch.object(shelf_file, "resolve", fake_resolve):
            out = clip_loader.PixlStashCLIPLoader().load_clip(first, "flux", second)
        return out["ui"][lock.UI_KEY][0]["models"]

    def test_one_encoder_locks_one_file(self):
        self.assertEqual(
            self._models(SHA_A, ""),
            [{"kind": "clip", "sha256": SHA_A, "id": None}],
        )

    def test_two_encoders_lock_in_widget_order(self):
        # Swapping the pair is a different (and usually broken) model, so the
        # order is part of the report.
        self.assertEqual(
            [m["sha256"] for m in self._models(SHA_A, SHA_B)], [SHA_A, SHA_B]
        )

    def test_an_uppercase_selection_is_locked_in_the_shelf_s_own_casing(self):
        # The widget can hold either; the shelf addresses in lowercase hex and
        # a lock PixlStash cannot match against its own rows is no lock at all.
        self.assertEqual(self._models(SHA_A.upper(), "")[0]["sha256"], SHA_A)


if __name__ == "__main__":
    unittest.main()

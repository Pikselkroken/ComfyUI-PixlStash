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
checkpoint_loader = boot.load("nodes.checkpoint_loader")

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


def _tensor_stubs():
    """sys.modules entries for the tensor plumbing, for a ``patched_modules`` block.

    Installed for the length of the import and then taken back out. The stubs
    are thin — a numpy with four attributes, a torch with two — so leaving them
    in sys.modules for the whole process (unittest imports every test module
    before running any of them) would hand a later test a fake numpy and make
    it fail somewhere else entirely. ``picture_loader`` binds them at its own
    import and keeps those bindings, which is all this needs.
    """
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
    return {"numpy": numpy, "torch": torch, "PIL": pil, "PIL.Image": pil.Image}


class _Pil:
    """A decoded picture, already the reference size so no resize is attempted."""

    size = (8, 8)

    def convert(self, mode):
        return self


with boot.patched_modules(_tensor_stubs()):
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

    def test_the_digest_is_normalised_here_and_not_at_four_call_sites(self):
        # PixlStash matches these against its own rows, which are lowercase
        # hex. Three loaders lowercasing and a fourth passing the server's
        # field through raw is not a contract.
        for raw in (SHA_A.upper(), f"  {SHA_A}  ", f"\t{SHA_A.upper()}\n"):
            with self.subTest(raw=raw):
                self.assertEqual(lock.shelf_model("vae", sha256=raw)["sha256"], SHA_A)

    def test_the_row_id_is_normalised_too_and_not_passed_through_raw(self):
        # checkpoint_loader._fetch_record matches on str(row["id"]), so a
        # server serialising ids as strings is supported — and the lock would
        # then report {"id": "7"} where another server reports {"id": 7}.
        for raw, expected in (
            (7, 7),
            ("7", 7),
            ("  7  ", 7),
            (None, None),
            ("", None),
            (0, None),
            (-1, None),
            (True, None),
            ("007", None),
            ("7; DROP", None),
            ("٧", None),
        ):
            with self.subTest(raw=raw):
                self.assertEqual(
                    lock.shelf_model("checkpoint", row_id=raw)["id"], expected
                )

    def test_anything_that_is_not_a_digest_is_null_rather_than_reported(self):
        # Null says "address this by id"; a junk string says "address it by
        # this", which is worse than saying nothing.
        for raw in ("", None, "not-a-digest", 12345, SHA_A[:-1], SHA_A + "a", "z" * 64):
            with self.subTest(raw=raw):
                self.assertIsNone(lock.shelf_model("vae", sha256=raw)["sha256"])


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

    def test_an_uppercase_selection_is_locked_in_lowercase_hex(self):
        # The widget can hold either; the shelf addresses in lowercase hex and
        # a lock PixlStash cannot match against its own rows is no lock at all.
        self.assertEqual(self._models(SHA_A.upper(), "")[0]["sha256"], SHA_A)

    def test_the_shelf_s_own_digest_wins_over_the_widget_s(self):
        # They agree today. If they ever stop, the one the bytes were verified
        # against is the honest answer.
        self.assertEqual(
            self._models(SHA_A, "", shelf_digest=SHA_B)[0]["sha256"], SHA_B
        )


class CheckpointLockTests(unittest.TestCase):
    """The only loader addressed by row id, and the only one that can lock a null digest."""

    def setUp(self):
        comfy = types.ModuleType("comfy")
        comfy.__path__ = []
        sd = types.ModuleType("comfy.sd")
        sd.load_checkpoint_guess_config = lambda path, **kwargs: (
            "MODEL",
            "CLIP",
            "VAE",
            "extra",
        )
        comfy.sd = sd

        fp = types.ModuleType("folder_paths")
        fp.get_folder_paths = lambda kind: ["/models/embeddings"]

        patch = mock.patch.dict(
            sys.modules, {"comfy": comfy, "comfy.sd": sd, "folder_paths": fp}
        )
        patch.start()
        self.addCleanup(patch.stop)
        self.sd = sd

    def _load(self, row):
        node = checkpoint_loader.PixlStashCheckpointLoader()
        with (
            mock.patch.object(
                checkpoint_loader.PixlStashCheckpointLoader,
                "_fetch_record",
                staticmethod(lambda checkpoint_id: row),
            ),
            mock.patch.object(
                checkpoint_loader.shelf_file,
                "local_path",
                lambda record, label="": "/m/c.st",
            ),
        ):
            return node.load_checkpoint("7")

    def test_an_unhashed_checkpoint_locks_its_row_id_with_a_null_digest(self):
        # The reason this loader is addressed by id at all: a 24 GB file is
        # loadable here long before the shelf's hasher has read it.
        out = self._load({"id": 7, "sha256": None})
        self.assertEqual(out["result"], ("MODEL", "CLIP", "VAE"))
        self.assertEqual(
            out["ui"][lock.UI_KEY][0]["models"],
            [{"kind": "checkpoint", "sha256": None, "id": 7}],
        )

    def test_a_hashed_checkpoint_locks_both_identifiers(self):
        models = self._load({"id": 7, "sha256": SHA_A.upper()})["ui"][lock.UI_KEY][0][
            "models"
        ]
        self.assertEqual(models, [{"kind": "checkpoint", "sha256": SHA_A, "id": 7}])

    def test_a_bare_diffusion_model_still_reports_its_lock(self):
        # load_checkpoint_guess_config raises for a Flux UNET; the node falls
        # back to a MODEL with no CLIP or VAE, and the lock must survive the
        # fallback rather than only existing on the happy path.
        def explode(path, **kwargs):
            raise RuntimeError("ERROR: Could not detect model type")

        self.sd.load_checkpoint_guess_config = explode
        self.sd.load_diffusion_model = lambda path: "UNET"

        out = self._load({"id": 7, "sha256": None})
        self.assertEqual(out["result"], ("UNET", None, None))
        self.assertEqual(out["ui"][lock.UI_KEY][0]["models"][0]["id"], 7)


if __name__ == "__main__":
    unittest.main()

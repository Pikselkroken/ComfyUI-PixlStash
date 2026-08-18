"""What ``_local_path`` will and won't hand to a torch loader.

``folder_path`` and ``relpath`` arrive over the network, so ``_local_path``
refuses anything that leaves the registered folder, is not a ``.safetensors``
file, was not ``present`` at the last scan, or does not match the recorded size
— including the case where there is no recorded size to match against. Same
concern ``test_path_traversal.py`` pins for the Saver, from the other
direction.

Note what the containment check is and isn't: both halves of the path come off
the *same* wire, so it is not a boundary against a server that wants to name
``/etc/shadow`` — it catches the ordinary bug where a relpath escapes its own
folder. The size check is what stops the realistic failure, a same-named but
unrelated file on a ComfyUI host that isn't the PixlStash host.

The sha256 guard in ``resolve`` is covered here too: that value comes out of a
saved workflow and is interpolated into both a request path and a cache
filename.
"""

import os
import shutil
import tempfile
import unittest

import _bootstrap as boot

adapter_loader = boot.load("nodes.adapter_loader")

TMP = tempfile.mkdtemp(prefix="pixlstash_adapter_test_")
FOLDER = os.path.join(TMP, "loras")
OUTSIDE = os.path.join(TMP, "secrets")


def setUpModule():
    os.makedirs(FOLDER, exist_ok=True)
    os.makedirs(OUTSIDE, exist_ok=True)
    for path in (
        os.path.join(FOLDER, "good.safetensors"),
        os.path.join(FOLDER, "notes.txt"),
        os.path.join(OUTSIDE, "stolen.safetensors"),
    ):
        with open(path, "wb") as fh:
            fh.write(FILE_BYTES)


def tearDownModule():
    shutil.rmtree(TMP, ignore_errors=True)


FILE_BYTES = b"x"


def record(*locations, **extra):
    """A shelf record whose ``file_size`` matches the fixture files by default.

    Every fixture is the same one byte, so the default lets the location tests
    exercise containment without restating the size; the size checks below
    override it.
    """
    return {"locations": list(locations), "file_size": len(FILE_BYTES), **extra}


def loc(relpath, state="present", folder=FOLDER):
    return {"folder_path": folder, "relpath": relpath, "state": state}


class LocalPathTests(unittest.TestCase):
    def test_returns_the_join_when_everything_holds(self):
        path = adapter_loader._local_path(record(loc("good.safetensors")))
        self.assertEqual(
            path, os.path.normpath(os.path.join(FOLDER, "good.safetensors"))
        )

    def test_refuses_relpath_escaping_its_folder(self):
        self.assertIsNone(
            adapter_loader._local_path(record(loc("../secrets/stolen.safetensors")))
        )

    def test_refuses_absolute_relpath(self):
        # os.path.join drops the folder entirely for an absolute second arg —
        # the containment check is what catches it.
        self.assertIsNone(
            adapter_loader._local_path(
                record(loc(os.path.join(OUTSIDE, "stolen.safetensors")))
            )
        )

    def test_refuses_non_safetensors_extension(self):
        self.assertIsNone(adapter_loader._local_path(record(loc("notes.txt"))))

    def test_ignores_locations_that_are_not_present(self):
        for state in ("missing", "unreachable", "not_downloaded"):
            with self.subTest(state=state):
                self.assertIsNone(
                    adapter_loader._local_path(
                        record(loc("good.safetensors", state=state))
                    )
                )

    def test_ignores_a_present_location_whose_file_is_gone(self):
        self.assertIsNone(
            adapter_loader._local_path(record(loc("vanished.safetensors")))
        )

    def test_ignores_a_directory(self):
        # os.path.getsize succeeds on a directory and returns its block size,
        # so without an isfile check a directory could match a size and be
        # handed to the torch loader as if it were a file.
        d = os.path.join(FOLDER, "adir.safetensors")
        os.makedirs(d, exist_ok=True)
        self.addCleanup(os.rmdir, d)
        size = os.path.getsize(d)
        self.assertIsNone(
            adapter_loader._local_path(record(loc("adir.safetensors"), file_size=size))
        )

    def test_skips_a_bad_location_and_takes_the_next_good_one(self):
        path = adapter_loader._local_path(
            record(
                loc("../secrets/stolen.safetensors"),
                loc("good.safetensors"),
            )
        )
        self.assertEqual(
            path, os.path.normpath(os.path.join(FOLDER, "good.safetensors"))
        )

    def test_no_locations(self):
        self.assertIsNone(adapter_loader._local_path({}))


class SizeCheckTests(unittest.TestCase):
    """The shelf's paths are the *PixlStash host's* paths.

    When ComfyUI runs elsewhere, the same path here may hold an unrelated file.
    Loading that silently would be the worst thing this node could do, so the
    recorded ``file_size`` has to match — and when there is no usable size to
    check against, the copy is refused rather than trusted. Either way the
    caller falls through to the download, which verifies the digest, so a
    refusal costs a copy and never correctness.
    """

    REAL = None

    @classmethod
    def setUpClass(cls):
        cls.REAL = os.path.getsize(os.path.join(FOLDER, "good.safetensors"))

    def test_accepts_when_the_size_matches(self):
        self.assertIsNotNone(
            adapter_loader._local_path(
                record(loc("good.safetensors"), file_size=self.REAL)
            )
        )

    def test_rejects_a_same_named_file_of_a_different_size(self):
        self.assertIsNone(
            adapter_loader._local_path(
                record(loc("good.safetensors"), file_size=999_999)
            )
        )

    def test_refuses_when_the_shelf_records_no_size(self):
        # Unverifiable is exactly the case the guard exists for; trusting the
        # path here would turn it off for every record with a null size.
        self.assertIsNone(
            adapter_loader._local_path(record(loc("good.safetensors"), file_size=None))
        )
        # Absent entirely, not just null.
        self.assertIsNone(
            adapter_loader._local_path({"locations": [loc("good.safetensors")]})
        )

    def test_a_size_that_is_not_an_int_is_still_checked(self):
        # JSON decodes 1.2e9 to a float and some clients stringify; an
        # isinstance(x, int) test would silently skip the check for both.
        self.assertIsNotNone(
            adapter_loader._local_path(
                record(loc("good.safetensors"), file_size=float(self.REAL))
            )
        )
        self.assertIsNotNone(
            adapter_loader._local_path(
                record(loc("good.safetensors"), file_size=str(self.REAL))
            )
        )
        self.assertIsNone(
            adapter_loader._local_path(
                record(loc("good.safetensors"), file_size="999999")
            )
        )

    def test_a_nonsense_size_refuses_rather_than_skipping_the_check(self):
        for bad in (True, "big", [], {}, -1):
            with self.subTest(bad=bad):
                self.assertIsNone(
                    adapter_loader._local_path(
                        record(loc("good.safetensors"), file_size=bad)
                    )
                )

    def test_zero_is_refused_even_when_the_local_file_is_also_empty(self):
        # The one length at which "two different files don't collide on exact
        # byte size by accident" is false: an interrupted download, a touched
        # placeholder and a failed copy are all 0 bytes and all match.
        empty = os.path.join(FOLDER, "empty.safetensors")
        with open(empty, "wb"):
            pass
        self.addCleanup(os.remove, empty)
        self.assertIsNone(
            adapter_loader._local_path(record(loc("empty.safetensors"), file_size=0))
        )


class SymlinkTests(unittest.TestCase):
    """Symlinking a big model into a models directory is ordinary practice.

    The containment check is lexical for exactly this reason: resolving
    symlinks would refuse every such file as if it were a traversal attempt,
    and log it at ERROR while doing so.
    """

    def setUp(self):
        self.target_dir = os.path.join(TMP, "elsewhere")
        os.makedirs(self.target_dir, exist_ok=True)
        self.target = os.path.join(self.target_dir, "real.safetensors")
        with open(self.target, "wb") as fh:
            fh.write(FILE_BYTES)
        self.link = os.path.join(FOLDER, "linked.safetensors")
        if os.path.lexists(self.link):
            os.remove(self.link)
        os.symlink(self.target, self.link)
        self.addCleanup(os.remove, self.link)

    def test_a_symlink_inside_the_folder_is_usable(self):
        path = adapter_loader._local_path(record(loc("linked.safetensors")))
        self.assertEqual(path, os.path.normpath(self.link))

    def test_a_relpath_that_escapes_is_still_refused(self):
        # The lexical check must not have bought symlink support by giving up
        # on the traversal it exists for.
        self.assertIsNone(
            adapter_loader._local_path(record(loc("../secrets/stolen.safetensors")))
        )


class MalformedRecordTests(unittest.TestCase):
    """Server data shapes the node has no reason to trust."""

    def test_a_non_list_locations_is_not_a_crash(self):
        for locations in ({"a": 1}, "somewhere", 7):
            with self.subTest(locations=locations):
                self.assertIsNone(
                    adapter_loader._local_path({"locations": locations, "file_size": 1})
                )

    def test_a_non_dict_location_is_skipped(self):
        real = os.path.getsize(os.path.join(FOLDER, "good.safetensors"))
        self.assertEqual(
            adapter_loader._local_path(
                {
                    "file_size": real,
                    "locations": ["/oops", None, loc("good.safetensors")],
                }
            ),
            os.path.normpath(os.path.join(FOLDER, "good.safetensors")),
        )


class TriggerWordsTests(unittest.TestCase):
    """The STRING output must be a string whatever the server sent."""

    def test_string_passes_through(self):
        self.assertEqual(
            adapter_loader._trigger_words({"trigger_words": "a, b"}), "a, b"
        )

    def test_list_is_joined(self):
        self.assertEqual(
            adapter_loader._trigger_words({"trigger_words": ["a", "b"]}), "a, b"
        )

    def test_missing_and_null_become_empty(self):
        self.assertEqual(adapter_loader._trigger_words({}), "")
        self.assertEqual(adapter_loader._trigger_words({"trigger_words": None}), "")


class ShaValidationTests(unittest.TestCase):
    """``adapter_sha256`` reaches a URL path and a cache filename."""

    def _resolve(self, value):
        node = adapter_loader.PixlStashAdapterLoader()
        return node._resolve(value)

    def test_empty_selection_names_the_browse_button(self):
        with self.assertRaises(RuntimeError) as ctx:
            self._resolve("")
        self.assertIn("Browse", str(ctx.exception))

    def test_rejects_non_digest(self):
        for bad in ("../../etc/passwd", "a" * 63, "a" * 65, "g" * 64, "a" * 64 + "/x"):
            with self.subTest(bad=bad):
                with self.assertRaises(RuntimeError) as ctx:
                    self._resolve(bad)
                self.assertIn("hex digest", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()

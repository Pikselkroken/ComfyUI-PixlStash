"""Path containment: a hostile ``filename_prefix`` can't escape the temp dir.

The Picture Saver writes preview PNGs to ComfyUI's temp directory. The actual
sanitisation is done by ``folder_paths.get_save_image_path`` (a ComfyUI helper
that strips traversal/absolute paths and keeps the result inside the given
base). This test pins the *contract* the Saver must keep: it must delegate to
that helper with the raw prefix and write only under the folder the helper
returns. A regression that joined the raw prefix itself (the original bug)
would fail both assertions.
"""

import os
import shutil
import sys
import tempfile
import types
import unittest
from unittest import mock

import _bootstrap as boot

TMP = tempfile.mkdtemp(prefix="pixlstash_test_")

# Faithful-enough stand-in for folder_paths: records the call and always
# returns a folder contained within TMP, mimicking the real helper's
# containment guarantee.
FP = types.ModuleType("folder_paths")
FP.calls = []
FP.get_temp_directory = lambda: TMP


def _get_save_image_path(prefix, outdir):
    FP.calls.append((prefix, outdir))
    return (os.path.join(outdir, "contained"), "img", 1, "", "")


FP.get_save_image_path = _get_save_image_path

# Install numpy/PIL/folder_paths stubs *before* importing picture_saver, which
# binds them at module import time.
for _name, _mod in boot.imaging_modules(FP).items():
    sys.modules[_name] = _mod
picture_saver = boot.load("nodes.picture_saver")


def tearDownModule():
    shutil.rmtree(TMP, ignore_errors=True)


class _Arr:
    def __mul__(self, other):
        return self

    def clip(self, lo, hi):
        return self

    def astype(self, dtype):
        return self


class _Tensor:
    def cpu(self):
        return self

    def numpy(self):
        return _Arr()


class _Images:
    shape = (1, 1, 1, 3)

    def __getitem__(self, idx):
        return _Tensor()


class PathTraversalTests(unittest.TestCase):
    def test_output_stays_inside_temp_dir(self):
        saver = picture_saver.PixlStashPictureSaver()
        hostile = "../../../../tmp/evil"
        FP.calls.clear()

        def fake_upload(client, files, project_id=""):
            return ([], [])

        with mock.patch.object(
            picture_saver.PixlStashPictureSaver,
            "_upload",
            staticmethod(fake_upload),
        ):
            saver.save_pictures(_Images(), hostile, False, url="https://x", token="t")

        # 1) Sanitisation is delegated to ComfyUI's helper, with the raw prefix.
        self.assertEqual(FP.calls, [(hostile, TMP)])

        # 2) The PNG is written under the folder the helper returned, i.e. inside
        #    the temp dir, never at the traversal target.
        written = os.path.join(TMP, "contained", "img_00001.png")
        self.assertTrue(os.path.isfile(written))
        self.assertTrue(
            os.path.realpath(written).startswith(os.path.realpath(TMP) + os.sep)
        )


if __name__ == "__main__":
    unittest.main()

"""Whose a picked picture's id is, across the pages the picker lists.

PixlStash names the library on each page of its picture list, and the picker
writes it beside the ids it picks. Run in Node, so skipped where there is none.
"""

import json
import pathlib
import shutil
import subprocess
import tempfile
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]

DRIVER = """
import { libraryOfPages } from "./picker.js";

const over = (pages) => pages.reduce(libraryOfPages, undefined) ?? "";
console.log(JSON.stringify({
    none_listed: over([]),
    one_page: over(["lib-a"]),
    agreeing: over(["lib-a", "lib-a", "lib-a"]),
    unnamed: over(["", ""]),
    header_lost: over(["lib-a", ""]),
    header_late: over(["", "lib-a"]),
    switched: over(["lib-a", "lib-b"]),
    switched_back: over(["lib-a", "lib-b", "lib-a", "lib-a"]),
    lost_then_back: over(["lib-a", "", "lib-a"]),
}));
"""


@unittest.skipUnless(shutil.which("node"), "needs node")
class PickerLibraryTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        with tempfile.TemporaryDirectory() as tmp:
            pathlib.Path(tmp, "package.json").write_text('{"type": "module"}')
            for name in ("picker.js", "modal_dom.js"):
                shutil.copy(ROOT / "web" / "js" / name, tmp)
            pathlib.Path(tmp, "driver.js").write_text(DRIVER)
            done = subprocess.run(
                ["node", str(pathlib.Path(tmp, "driver.js"))],
                capture_output=True,
                text=True,
                timeout=60,
            )
        if done.returncode:
            raise AssertionError(done.stderr)
        cls.results = json.loads(done.stdout.strip().splitlines()[-1])

    def test_pages_of_one_library_name_it(self):
        self.assertEqual(self.results["one_page"], "lib-a")
        self.assertEqual(self.results["agreeing"], "lib-a")

    def test_nothing_listed_names_no_library(self):
        self.assertEqual(self.results["none_listed"], "")
        self.assertEqual(self.results["unnamed"], "")

    def test_pages_that_do_not_agree_name_none_and_none_comes_back(self):
        # Neither the first library nor the last: a pick across a switch is
        # not known to be either's, and vouching for it under one would let
        # PixlStash read another library's ids as its own.
        for name in (
            "header_lost",
            "header_late",
            "switched",
            "switched_back",
            "lost_then_back",
        ):
            with self.subTest(name=name):
                self.assertEqual(self.results[name], "")


if __name__ == "__main__":
    unittest.main()

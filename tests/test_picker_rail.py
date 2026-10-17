"""The picture picker's rail: PixlStash's groupings, and which one it opens on.

Run in Node, so skipped where there is none.
"""

import json
import pathlib
import shutil
import subprocess
import tempfile
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]

DRIVER = """
import { railGroups, initialFacet } from "./picker.js";

console.log(JSON.stringify({
    groups: railGroups(
        [{ id: 1, name: "Small", image_count: 3 }, { id: 2, name: "Big", image_count: 90 }],
        [{ id: 8, name: "Bo" }, { id: 7, name: "Ann", image_count: 5 }],
        [
            { id: 4, name: "Faces of Ann", picture_count: 50, reference_character: { id: 7 } },
            { id: 5, name: "Style", picture_count: 9 },
        ],
    ),
    not_lists: railGroups(null, { error: "forbidden" }, undefined),
    nothing_wired: initialFacet({}),
    no_filters: initialFacet(undefined),
    project_only: initialFacet({ projectId: "2" }),
    all_wired: initialFacet({ projectId: "2", setId: 5, characterId: "7" }),
    project_and_character: initialFacet({ projectId: "2", characterId: "7" }),
}));
"""


@unittest.skipUnless(shutil.which("node"), "needs node")
class PickerRailTests(unittest.TestCase):
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

    def test_rows_are_biggest_first_with_their_counts(self):
        groups = self.results["groups"]
        self.assertEqual(
            groups["project"],
            [
                {"id": "2", "name": "Big", "count": 90},
                {"id": "1", "name": "Small", "count": 3},
            ],
        )
        # A character PixlStash sent no count for shows none, after the
        # counted ones, rather than claiming to be empty.
        self.assertEqual(
            groups["character"],
            [
                {"id": "7", "name": "Ann", "count": 5},
                {"id": "8", "name": "Bo", "count": None},
            ],
        )

    def test_a_reference_set_is_not_a_picture_set_row(self):
        self.assertEqual(
            self.results["groups"]["set"], [{"id": "5", "name": "Style", "count": 9}]
        )

    def test_a_list_that_did_not_come_is_an_empty_group(self):
        self.assertEqual(
            self.results["not_lists"], {"project": [], "character": [], "set": []}
        )

    def test_opens_on_everything_with_nothing_wired(self):
        self.assertEqual(self.results["nothing_wired"], {"kind": "", "id": ""})
        self.assertEqual(self.results["no_filters"], {"kind": "", "id": ""})

    def test_opens_on_the_narrowest_wired_grouping(self):
        self.assertEqual(self.results["project_only"], {"kind": "project", "id": "2"})
        self.assertEqual(self.results["all_wired"], {"kind": "set", "id": "5"})
        self.assertEqual(
            self.results["project_and_character"], {"kind": "character", "id": "7"}
        )


if __name__ == "__main__":
    unittest.main()

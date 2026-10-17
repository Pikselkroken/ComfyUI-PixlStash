"""Which ids a Picture Loader counts as shown, so a failed preview is asked for again.

``updateNodePreviews`` records the ids it loads so the draw check in
``combo_widgets.js`` does not load them twice, and must not keep that record
when a preview did not arrive. Run in Node, so skipped where there is none.
"""

import json
import pathlib
import shutil
import subprocess
import tempfile
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]

DRIVER = """
import { updateNodePreviews } from "./picker.js";

globalThis.Image = class {};
const creds = { url: "http://example.test", token: "test-token" };
const answer = (failing) => async (url) => {
    if (failing === "throw") throw new Error("connection refused");
    const id = new URLSearchParams(url.split("?")[1]).get("picture_id");
    return id === failing
        ? { ok: false, status: 502 }
        : { ok: true, blob: async () => new Blob(["x"]) };
};
const shown = async (failing, ids = [1, 2]) => {
    globalThis.fetch = answer(failing);
    const node = {};
    await updateNodePreviews(node, ids, creds).catch(() => {});
    return node._pixlstashIds ?? null;
};
console.log(JSON.stringify({
    all: await shown(null),
    one_missing: await shown("2"),
    unreachable: await shown("throw"),
    none: await shown(null, []),
}));
"""


@unittest.skipUnless(shutil.which("node"), "needs node")
class PicturePreviewTests(unittest.TestCase):
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

    def test_previews_that_all_arrived_are_shown(self):
        self.assertEqual(self.results["all"], "1,2")
        self.assertEqual(self.results["none"], "")

    def test_a_preview_that_did_not_arrive_is_asked_for_again(self):
        self.assertIsNone(self.results["one_missing"])
        self.assertIsNone(self.results["unreachable"])


if __name__ == "__main__":
    unittest.main()

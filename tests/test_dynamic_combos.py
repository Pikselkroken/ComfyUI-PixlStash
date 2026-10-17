"""What a dropdown filled from PixlStash shows once its list has arrived.

The Project, Set, Character and Workflow Set pickers and both adapter loaders'
``base_model`` are declared with "(loading…)" as their only value, and
``web/js/combo_widgets.js`` fills them from the server through one getter.
Driven here through the Project Loader, which is that getter and nothing else.
Run in Node against a stand-in for ComfyUI's ``app``, so skipped where there
is none.
"""

import json
import pathlib
import shutil
import subprocess
import tempfile
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]

DRIVER = r"""
import { app } from "../../scripts/app.js";
import "./combo_widgets.js";

const [extension] = app.extensions;
const turns = () => new Promise((resolve) => setTimeout(resolve, 20));
let failing = false;
globalThis.fetch = async () => failing
    ? { ok: false, status: 500, json: async () => ({ error: "boom" }) }
    : { ok: true, json: async () => [{ id: 1, name: "Alpha" }] };

await extension.setup();
const nodeType = { prototype: {} };
await extension.beforeRegisterNodeDef(nodeType, { name: "PixlStashProjectLoader" });

function makeNode(value) {
    const node = Object.create(nodeType.prototype);
    node.widgets = [{ name: "pixlstash_project", value, options: { values: [value] } }];
    node.setDirtyCanvas = () => {};
    node.onNodeCreated();
    const widget = node.widgets[0];
    // ComfyUI reads the list on every draw of the node.
    return () => { widget.options.values; return widget.value; };
}

const results = {};
const fresh = makeNode("(loading…)");
results.while_loading = fresh();
await turns();
results.loaded = fresh();
results.from_the_cache = makeNode("(loading…)")();
results.saved_pick = makeNode("Alpha #1")();

failing = true;
app.settings.get("PixlStash.APIToken").onChange();
const broken = makeNode("(loading…)");
broken();
await turns();
results.failed = broken();
results.none_through_a_failure = fresh();

failing = false;
app.settings.get("PixlStash.APIToken").onChange();
results.retrying = broken();
await turns();
results.recovered = broken();
console.log(JSON.stringify(results));
"""

APP = r"""
const values = { "PixlStash.ServerURL": "http://example.test", "PixlStash.APIToken": "test-token" };
export const app = {
    extensions: [],
    settings: new Map(),
    registerExtension(extension) { this.extensions.push(extension); },
    ui: { settings: {
        addSetting(setting) { app.settings.set(setting.id, setting); },
        getSettingValue: (id, fallback) => values[id] ?? fallback,
    } },
    graph: { links: {}, setDirtyCanvas() {}, getNodeById: () => null },
};
"""


@unittest.skipUnless(shutil.which("node"), "needs node")
class DynamicComboTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        with tempfile.TemporaryDirectory() as tmp:
            js = pathlib.Path(tmp, "web", "js")
            shutil.copytree(ROOT / "web" / "js", js)
            pathlib.Path(tmp, "package.json").write_text('{"type": "module"}')
            pathlib.Path(tmp, "scripts").mkdir()
            pathlib.Path(tmp, "scripts", "app.js").write_text(APP)
            (js / "driver.js").write_text(DRIVER)
            done = subprocess.run(
                ["node", str(js / "driver.js")],
                capture_output=True,
                text=True,
                timeout=60,
            )
        if done.returncode:
            raise AssertionError(done.stderr)
        cls.results = json.loads(done.stdout.strip().splitlines()[-1])

    def test_loading_is_shown_until_the_list_arrives(self):
        self.assertEqual(self.results["while_loading"], "(loading…)")

    def test_a_fresh_node_says_none_once_the_list_arrives(self):
        # The bug: it said "(loading…)" until something was picked by hand.
        self.assertEqual(self.results["loaded"], "— None —")
        self.assertEqual(self.results["from_the_cache"], "— None —")

    def test_a_real_pick_is_kept(self):
        self.assertEqual(self.results["saved_pick"], "Alpha #1")

    def test_a_failed_list_says_why_and_a_retry_clears_it(self):
        self.assertEqual(self.results["failed"], "⚠ boom")
        self.assertEqual(self.results["retrying"], "(loading…)")
        self.assertEqual(self.results["recovered"], "— None —")

    def test_none_is_not_overwritten_by_a_failure(self):
        self.assertEqual(self.results["none_through_a_failure"], "— None —")


if __name__ == "__main__":
    unittest.main()

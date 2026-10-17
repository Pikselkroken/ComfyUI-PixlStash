"""The Multi Adapter Loader says what its people view narrows by.

On a canvas opened from PixlStash the people view lists who PixlStash fits to
that workflow, by its checkpoint's base model, which ``base_model`` (an exact
filter of its own) never shows. ``web/js/combo_widgets.js`` names it on the
node from ``/pixlstash/workflow_people``. Run in Node against a stand-in for
ComfyUI's ``app`` and the DOM, so skipped where there is no ``node``.
"""

import json
import pathlib
import shutil
import subprocess
import tempfile
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]

KEY = "manual:" + "0" * 32

DRIVER = r"""
import { app } from "../../scripts/app.js";
import "./combo_widgets.js";

const [KEY] = JSON.parse(process.argv[2]);
const [extension] = app.extensions;
const turns = () => new Promise((resolve) => setTimeout(resolve, 20));
const element = () => ({
    style: {}, children: [],
    append(...c) { this.children.push(...c); },
    appendChild(c) { this.children.push(c); },
    replaceChildren(...c) { this.children = c; },
    addEventListener() {},
});
globalThis.document = { createElement: element };
let answer = { base_model: "Krea 2", family: "krea2", people: [] };
globalThis.fetch = async (url) => url.startsWith("/pixlstash/workflow_people")
    ? { ok: true, json: async () => answer }
    : { ok: false, status: 404, json: async () => ({}) };
await extension.setup();

const nodeType = { prototype: {} };
await extension.beforeRegisterNodeDef(nodeType, { name: "PixlStashMultiAdapterLoader" });

async function makeNode(show) {
    const node = Object.create(nodeType.prototype);
    const values = { show, adapter_kind: "— Any —", base_model: "(loading…)" };
    for (let row = 1; row <= 8; row++) {
        const n = row === 1 ? "" : `_${row}`;
        Object.assign(values, {
            [`adapter_sha256${n}`]: "", [`strength_model${n}`]: 1, [`strength_clip${n}`]: 1,
        });
    }
    node.widgets = Object.entries(values).map(([name, value]) => ({
        name, value, options: { values: name === "show" ? ["All adapters", "People who fit"] : [value] },
    }));
    node.size = [400, 200];
    node.box = null;
    node.addDOMWidget = (name, kind, box) => { node.box = box; return {}; };
    node.computeSize = () => [400, 200];
    node.setSize = () => {};
    node.setDirtyCanvas = () => {};
    node.onNodeCreated();
    await turns();
    return node;
}
const lines = (node) => node.box.children.map((c) => c.textContent).filter(Boolean);

const results = {};
app.graph.extra = { pixlstash_workflow_id: KEY };
results.people = lines(await makeNode("People who fit"));
results.all = lines(await makeNode("All adapters"));
answer = { base_model: null, family: null, people: [] };
app.reset();
results.unknown = lines(await makeNode("People who fit"));
app.graph.extra = {};
answer = { base_model: "Krea 2", family: "krea2", people: [] };
results.unlinked = lines(await makeNode("People who fit"));
console.log(JSON.stringify(results));
"""

APP = r"""
const values = { "PixlStash.ServerURL": "http://example.test", "PixlStash.APIToken": "test-token" };
const settings = new Map();
export const app = {
    extensions: [],
    registerExtension(extension) { this.extensions.push(extension); },
    ui: { settings: {
        addSetting(setting) { settings.set(setting.id, setting); },
        getSettingValue: (id, fallback) => values[id] ?? fallback,
    } },
    graph: { links: {}, extra: {}, setDirtyCanvas() {}, getNodeById: () => null },
    // What a change of connection settings does: every lookup asked again.
    reset() { settings.get("PixlStash.APIToken").onChange(); },
};
"""


@unittest.skipUnless(shutil.which("node"), "needs node")
class PeopleFitLineTests(unittest.TestCase):
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
                ["node", str(js / "driver.js"), json.dumps([KEY])],
                capture_output=True,
                text=True,
                timeout=60,
            )
        if done.returncode:
            raise AssertionError(done.stderr)
        cls.results = json.loads(done.stdout.strip().splitlines()[-1])

    FITS = "People who fit Krea 2, this workflow's checkpoint."

    def test_the_people_view_names_the_checkpoints_base_model(self):
        self.assertEqual(self.results["people"][0], self.FITS)

    def test_no_line_where_it_would_not_be_true(self):
        # The adapter view is not narrowed by it, PixlStash may not know the
        # checkpoint's base model, and an unlinked canvas has no workflow.
        for case in ("all", "unknown", "unlinked"):
            with self.subTest(case=case):
                self.assertNotIn(self.FITS, self.results[case])
                self.assertFalse(
                    any(
                        line.startswith("People who fit") for line in self.results[case]
                    )
                )


if __name__ == "__main__":
    unittest.main()

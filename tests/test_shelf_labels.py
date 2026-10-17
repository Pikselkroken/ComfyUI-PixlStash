"""A shelf loader's button names the file its hidden widget holds, however it got there.

ComfyUI's API loader makes a node and only then sets its values, which is how
PixlStash's *Open in ComfyUI* fills the Checkpoint and ControlNet loaders it
swaps in. ``web/js/combo_widgets.js`` read the value on creation and on
``onConfigure`` only, so such a loader said "Browse checkpoints…" over the
checkpoint it held unless something reloaded the graph afterwards. Run in Node
against a stand-in for ComfyUI's ``app`` and the DOM, so skipped where there
is no ``node``.
"""

import json
import pathlib
import shutil
import subprocess
import tempfile
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]

DIGEST = "a" * 64

DRIVER = r"""
import { app } from "../../scripts/app.js";
import "./combo_widgets.js";

const [DIGEST] = JSON.parse(process.argv[2]);
const [extension] = app.extensions;
const turns = () => new Promise((resolve) => setTimeout(resolve, 20));
// A row's first child is its button.
const element = () => ({
    style: {}, textContent: "", title: "",
    append(...children) { this.button = children[0]; },
    appendChild() {}, addEventListener() {}, setAttribute() {}, removeAttribute() {}, replaceChildren() {},
});
globalThis.document = { createElement: element };
globalThis.fetch = async (url) => {
    if (url.startsWith("/pixlstash/checkpoints")) {
        return { ok: true, json: async () => ({ checkpoints: [{ id: 12, display_name: "Juggernaut" }] }) };
    }
    if (url.startsWith("/pixlstash/adapter?")) {
        return { ok: true, json: async () => ({ sha256: DIGEST, display_name: "Depth" }) };
    }
    return { ok: false, status: 404, json: async () => ({}) };
};
await extension.setup();

async function makeNode(type, widgets) {
    const nodeType = { prototype: {} };
    await extension.beforeRegisterNodeDef(nodeType, { name: type });
    const node = Object.create(nodeType.prototype);
    node.size = [300, 100];
    node.widgets = Object.entries(widgets).map(([name, value]) => ({ name, value }));
    node.buttons = [];
    node.addDOMWidget = (name, kind, row) => { node.buttons.push(row); return {}; };
    node.setDirtyCanvas = () => {};
    node.onNodeCreated();
    return node;
}
const ctx = new Proxy({}, { get: () => () => {} });
const label = (node) => node.buttons[0].button.textContent;

const results = {};
for (const [type, field, value] of [
    ["PixlStashCheckpointLoader", "checkpoint_id", "12"],
    ["PixlStashControlNetLoader", "control_net_sha256", DIGEST],
    ["PixlStashUpscaleModelLoader", "upscale_model_sha256", DIGEST],
]) {
    const node = await makeNode(type, { [field]: "", shelf: "" });
    const before = label(node);
    // What ComfyUI's API loader does once the node exists.
    node.widgets[0].value = value;
    const undrawn = label(node);
    node.onDrawForeground(ctx);
    await turns();
    results[type] = { before, undrawn, drawn: label(node) };
}
console.log(JSON.stringify(results));
"""

APP = r"""
const values = { "PixlStash.ServerURL": "http://example.test", "PixlStash.APIToken": "test-token" };
export const app = {
    extensions: [],
    registerExtension(extension) { this.extensions.push(extension); },
    ui: { settings: {
        addSetting() {},
        getSettingValue: (id, fallback) => values[id] ?? fallback,
    } },
    graph: { links: {}, setDirtyCanvas() {}, getNodeById: () => null },
};
"""


@unittest.skipUnless(shutil.which("node"), "needs node")
class ShelfLabelTests(unittest.TestCase):
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
                ["node", str(js / "driver.js"), json.dumps([DIGEST])],
                capture_output=True,
                text=True,
                timeout=60,
            )
        if done.returncode:
            raise AssertionError(done.stderr)
        cls.results = json.loads(done.stdout.strip().splitlines()[-1])

    def test_a_checkpoint_set_after_the_node_was_made_is_named(self):
        got = self.results["PixlStashCheckpointLoader"]
        self.assertEqual(got["before"], "Browse checkpoints…")
        self.assertEqual(got["drawn"], "Checkpoint: Juggernaut")

    def test_a_controlnet_set_after_the_node_was_made_is_named(self):
        got = self.results["PixlStashControlNetLoader"]
        self.assertEqual(got["before"], "Browse ControlNets…")
        self.assertEqual(got["drawn"], "ControlNet: Depth")

    def test_an_upscaler_set_after_the_node_was_made_is_named(self):
        got = self.results["PixlStashUpscaleModelLoader"]
        self.assertEqual(got["before"], "Browse upscalers…")
        self.assertEqual(got["drawn"], "Upscaler: Depth")

    def test_nothing_changes_until_the_node_is_drawn(self):
        # Drawn is when: the setter is ComfyUI's, and it tells nobody.
        for got in self.results.values():
            self.assertEqual(got["undrawn"], got["before"])


if __name__ == "__main__":
    unittest.main()

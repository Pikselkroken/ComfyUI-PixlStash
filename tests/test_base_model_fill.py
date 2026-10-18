"""The adapter loaders' ``base_model`` is a family, and fills in from the workflow.

``web/js/combo_widgets.js`` lists the shelf's base-model families as PixlStash
names them, and on a canvas opened from PixlStash fills an unpicked
``base_model`` with the family of that workflow's checkpoint
(``/pixlstash/workflow_people``), so Browse opens on what fits it. Run in Node
against a stand-in for ComfyUI's ``app`` and the DOM, so skipped where there is
no ``node``.
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
import { buildAdapterQuery } from "./adapter_picker.js";

const [KEY] = JSON.parse(process.argv[2]);
const [extension] = app.extensions;
const turns = () => new Promise((resolve) => setTimeout(resolve, 20));
const element = () => ({
    style: {}, children: [], textContent: "",
    append(...c) { this.children.push(...c); },
    appendChild(c) { this.children.push(c); },
    replaceChildren(...c) { this.children = c; },
    addEventListener() {}, setAttribute() {}, removeAttribute() {},
});
globalThis.document = { createElement: element };
let answer = { family: "krea2", family_label: "Krea 2", people: [] };
const adapters = [
    { sha256: "a".repeat(64), base_model: "Krea 2 Turbo", base_model_family_label: "Krea 2" },
    { sha256: "b".repeat(64), base_model: "krea 2 raw", base_model_family_label: "Krea 2" },
    { sha256: "c".repeat(64), base_model: "SDXL 1.0", base_model_family_label: "SDXL" },
    { sha256: "d".repeat(64), base_model: null, base_model_family_label: null },
    // Hidden in the library, so its family is not offered either.
    { sha256: "e".repeat(64), base_model: "Flux.1 Dev", base_model_family_label: "Flux", hidden: true },
];
globalThis.fetch = async (url) => {
    if (url.startsWith("/pixlstash/workflow_people")) return { ok: true, json: async () => answer };
    if (url.startsWith("/pixlstash/adapters")) return { ok: true, json: async () => ({ adapters }) };
    return { ok: false, status: 404, json: async () => ({}) };
};
await extension.setup();

const ROWS = {};
for (let row = 1; row <= 8; row++) {
    const n = row === 1 ? "" : `_${row}`;
    Object.assign(ROWS, { [`adapter_sha256${n}`]: "", [`strength_model${n}`]: 1, [`strength_clip${n}`]: 1 });
}
const TYPES = {
    PixlStashMultiAdapterLoader: { show: "All adapters", ...ROWS },
    PixlStashAdapterLoader: { adapter_sha256: "", shelf: "" },
};

// `saved` is what the graph holds, set after creation as ComfyUI's loaders do.
async function baseModel(type, saved) {
    const nodeType = { prototype: {} };
    await extension.beforeRegisterNodeDef(nodeType, { name: type });
    const node = Object.create(nodeType.prototype);
    const values = { adapter_kind: "— Any —", base_model: "(loading…)", ...TYPES[type] };
    node.widgets = Object.entries(values).map(([name, value]) => ({
        name, value, options: { values: name === "show" ? ["All adapters", "People who fit"] : [value] },
    }));
    node.size = [400, 200];
    node.addDOMWidget = () => ({});
    node.addWidget = () => ({});
    node.computeSize = () => [400, 200];
    node.setSize = () => {};
    node.setDirtyCanvas = () => {};
    node.onNodeCreated();
    const widget = node.widgets.find((w) => w.name === "base_model");
    if (saved !== undefined) widget.value = saved;
    widget.options.values;   // the first draw asks for the list
    await turns();
    const options = widget.options.values;   // drawn again, then read
    return { options, value: widget.value };
}

const results = {};
app.graph.extra = { pixlstash_workflow_id: KEY };
for (const type of Object.keys(TYPES)) {
    results[type] = {
        fresh: await baseModel(type),
        saved_none: await baseModel(type, "— None —"),
        picked: await baseModel(type, "SDXL"),
    };
}
answer = { family: null, family_label: null, people: [] };
app.reset();
results.unknown = await baseModel("PixlStashMultiAdapterLoader");
app.graph.extra = {};
answer = { family: "krea2", family_label: "Krea 2", people: [] };
results.unlinked = await baseModel("PixlStashMultiAdapterLoader");
results.query = buildAdapterQuery({ fileKind: "adapter", baseModel: "Krea 2" });
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
class BaseModelFillTests(unittest.TestCase):
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

    LOADERS = ("PixlStashMultiAdapterLoader", "PixlStashAdapterLoader")

    def test_the_list_is_the_shelfs_families_by_name(self):
        for loader in self.LOADERS:
            with self.subTest(loader=loader):
                self.assertEqual(
                    self.results[loader]["fresh"]["options"],
                    ["— None —", "Krea 2", "SDXL"],
                )

    def test_an_unpicked_base_model_takes_the_workflows_family(self):
        for loader in self.LOADERS:
            for case in ("fresh", "saved_none"):
                with self.subTest(loader=loader, case=case):
                    self.assertEqual(self.results[loader][case]["value"], "Krea 2")

    def test_a_picked_base_model_is_kept(self):
        for loader in self.LOADERS:
            with self.subTest(loader=loader):
                self.assertEqual(self.results[loader]["picked"]["value"], "SDXL")

    def test_browse_asks_for_the_family(self):
        self.assertEqual(
            self.results["query"],
            {"file_kind": "adapter", "base_model_family": "Krea 2"},
        )

    def test_nothing_is_filled_in_where_nothing_says_the_family(self):
        # PixlStash does not know the checkpoint's family, or the canvas was
        # not opened from PixlStash.
        for case in ("unknown", "unlinked"):
            with self.subTest(case=case):
                self.assertEqual(self.results[case]["value"], "— None —")


if __name__ == "__main__":
    unittest.main()

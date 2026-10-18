"""A model the PixlStash library hides is never offered by a shelf picker.

The server still lists a hidden model, flagged ``hidden``, so a node that
already loads one keeps its name; ``web/js/adapter_picker.js`` leaves it out of
what it offers. Run in Node against a stand-in for the DOM, so skipped where
there is no ``node``.
"""

import json
import pathlib
import shutil
import subprocess
import tempfile
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]

DRIVER = r"""
import { openAdapterPicker } from "./adapter_picker.js";

class Element {
    constructor(tag) {
        Object.assign(this, { tag, style: {}, children: [], textContent: "", value: "", clientHeight: 0 });
    }
    append(...c) { this.children.push(...c); }
    appendChild(c) { this.children.push(c); return c; }
    replaceChildren(...c) { this.children = c; }
    addEventListener() {}
    setAttribute() {}
    removeAttribute() {}
    focus() {}
    remove() {}
}
const body = new Element("body");
globalThis.document = {
    body,
    createElement: (tag) => new Element(tag),
    addEventListener() {},
    removeEventListener() {},
};
const text = (node) => [node.textContent, ...node.children.flatMap(text)].filter(Boolean);

const A = "a".repeat(64), B = "b".repeat(64), C = "c".repeat(64), D = "d".repeat(64);
const person = [{ entity_type: "character", entity_id: 1 }];
const SHELF = {
    checkpoints: [
        { id: 1, display_name: "Shown checkpoint" },
        { id: 2, display_name: "Hidden checkpoint", hidden: true },
    ],
    // A stack whose cover alone is hidden, a hidden adapter of a person, and
    // one shown adapter of the same person.
    adapters: [
        { sha256: A, display_name: "Run cover", stack_id: 7, stack_position: 0, hidden: true },
        { sha256: B, display_name: "Run epoch", stack_id: 7, stack_position: 1 },
        { sha256: C, display_name: "Hidden person LoRA", attachments: person, hidden: true },
        { sha256: D, display_name: "Shown person LoRA", attachments: person },
    ],
    vaes: [{ sha256: A, display_name: "Hidden VAE", hidden: true }],
};
// The Multi Adapter Loader's grid narrowed by a base model: `family` names
// what the filtered list answers, `whole` the shelf beside it.
let family = { Hidden: SHELF.adapters.filter(r => r.hidden), None: [] };
let whole = SHELF.adapters;
globalThis.fetch = async (url) => {
    const json = url.startsWith("/pixlstash/checkpoints") ? { checkpoints: SHELF.checkpoints }
        : url.includes("file_kind=vae") ? { adapters: SHELF.vaes }
        : url.includes("base_model_family=") ? { adapters: family[new URL(url, "http://x.test").searchParams.get("base_model_family")] }
        : url.includes("file_kind=adapter") && globalThis.multi ? { adapters: whole }
        : url.startsWith("/pixlstash/adapters") ? { adapters: SHELF.adapters }
        : url.startsWith("/pixlstash/characters") ? [{ id: 1, name: "Someone" }]
        : null;
    return json ? { ok: true, json: async () => json } : { ok: false, status: 404, json: async () => ({}) };
};

const credentials = { url: "http://example.test", token: "test-token", verifySsl: true };
async function open(filters) {
    body.children = [];
    openAdapterPicker({ value: "" }, credentials, filters, () => {});
    await new Promise((resolve) => setTimeout(resolve, 20));
    return text(body);
}

const results = {
    checkpoint: await open({ fileKind: "checkpoint" }),
    adapter: await open({ fileKind: "adapter" }),
    allHidden: await open({ fileKind: "vae" }),
    several: await open({ fileKind: "adapter", several: { picked: [C], limit: 8, people: false } }),
    people: await open({ fileKind: "adapter", several: { picked: [], limit: 8, people: true } }),
};
globalThis.multi = true;
const narrowed = (baseModel) => open({ fileKind: "adapter", baseModel, several: { picked: [], limit: 8, people: false } });
// Only hidden rows fit, while the shelf has shown ones.
results.onlyHiddenFit = await narrowed("Hidden");
// Nothing fits, and the whole shelf is hidden.
whole = SHELF.adapters.map(r => ({ ...r, hidden: true }));
results.shelfAllHidden = await narrowed("None");
console.log(JSON.stringify(results));
"""


@unittest.skipUnless(shutil.which("node"), "needs node")
class HiddenModelTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        with tempfile.TemporaryDirectory() as tmp:
            pathlib.Path(tmp, "package.json").write_text('{"type": "module"}')
            for name in (
                "adapter_rows.js",
                "adapter_picker.js",
                "modal_dom.js",
                "model_mark.js",
            ):
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

    def test_a_hidden_checkpoint_is_not_offered(self):
        shown = self.results["checkpoint"]
        self.assertIn("1 checkpoint", shown)
        self.assertIn("Shown checkpoint", shown)
        self.assertNotIn("Hidden checkpoint", shown)

    def test_a_stack_whose_cover_is_hidden_is_offered_by_its_next_member(self):
        shown = self.results["adapter"]
        self.assertIn("Run epoch", shown)
        self.assertNotIn("Run cover", shown)
        self.assertNotIn("Hidden person LoRA", shown)

    def test_a_shelf_of_hidden_files_says_so_rather_than_that_it_is_empty(self):
        said = " ".join(self.results["allHidden"])
        self.assertIn("hidden in PixlStash", said)
        self.assertNotIn("Your shelf holds no", said)

    def test_a_narrowed_multi_grid_says_when_what_fits_is_hidden(self):
        for case in ("onlyHiddenFit", "shelfAllHidden"):
            with self.subTest(case=case):
                said = " ".join(self.results[case])
                self.assertIn("hidden in PixlStash", said)
                self.assertNotIn("match the node's filters", said)
                self.assertNotIn("Your shelf holds no", said)

    def test_a_hidden_adapter_already_on_a_row_is_named_but_not_offered(self):
        shown = self.results["several"]
        footer = next(t for t in shown if "stays on the node" in t)
        self.assertIn("Hidden person LoRA", footer)
        self.assertEqual(shown.count("Hidden person LoRA"), 0)

    def test_the_people_view_leaves_a_hidden_adapter_out(self):
        shown = self.results["people"]
        self.assertIn("1 person", shown)
        self.assertNotIn("Hidden person LoRA", " ".join(shown))


if __name__ == "__main__":
    unittest.main()

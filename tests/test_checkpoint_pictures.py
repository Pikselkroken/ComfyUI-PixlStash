"""The checkpoint picker shows what each checkpoint has made.

A card's meta line carries the shelf's picture count, and a panel beside the
grid shows the selected checkpoint's pictures, found by every name the
library's pictures recorded for its file. Run in Node against a stand-in for
the DOM, so skipped where there is no ``node``.
"""

import json
import pathlib
import shutil
import subprocess
import tempfile
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]

DRIVER = r"""
import { openAdapterPicker, recordedNamesFor } from "./adapter_picker.js";

class Element {
    constructor(tag) {
        Object.assign(this, { tag, style: {}, children: [], textContent: "", value: "", clientHeight: 0 });
    }
    append(...c) { this.children.push(...c); }
    appendChild(c) { this.children.push(c); return c; }
    replaceChildren(...c) { this.children = c; }
    addEventListener() {}
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

const CHECKPOINTS = [
    { id: 1, filename: "chroma.safetensors", pictures_verified: 80, pictures_by_filename: 6 },
    { id: 2, filename: "unused.safetensors", pictures_verified: 0, pictures_by_filename: 0 },
];
const RECORDED = [
    { value: "SDXL/Chroma.safetensors", name: null },
    { value: "chroma.safetensors", name: null },
    { value: "other.safetensors", name: null },
];
const asked = [];
let refused = false; // a ComfyUI link key, which PixlStash refuses the names route
globalThis.console.warn = () => {};
globalThis.fetch = async (url) => {
    asked.push(url);
    if (refused && url.startsWith("/pixlstash/pictures/comfyui_models")) {
        return { ok: false, status: 502, json: async () => ({ error: "PixlStash: token does not have access to this resource." }) };
    }
    const json = url.startsWith("/pixlstash/checkpoints") ? { checkpoints: CHECKPOINTS }
        : url.startsWith("/pixlstash/pictures/comfyui_models") ? RECORDED
        : url.startsWith("/pixlstash/pictures?") ? [{ id: 5, created_at: new Date(Date.now() - 2 * 86_400_000).toISOString() }]
        : null;
    return json ? { ok: true, json: async () => json, blob: async () => new Blob([]) }
        : { ok: false, status: 404, json: async () => ({}) };
};

const credentials = { url: "http://example.test", token: "test-token", verifySsl: true };
async function open(value) {
    body.children = [];
    asked.length = 0;
    openAdapterPicker({ value }, credentials, { fileKind: "checkpoint" }, () => {});
    await new Promise((resolve) => setTimeout(resolve, 30));
    const lists = asked.filter(u => u.startsWith("/pixlstash/pictures?"))
        .map(u => Object.fromEntries([...new URLSearchParams(u.split("?")[1])]
            .reduce((m, [k, v]) => m.set(k, [...(m.get(k) ?? []), v]), new Map())));
    return { shown: text(body), lists };
}

const results = {
    none: await open(""),
    used: await open("1"),
    unused: await open("2"),
    names: {
        plain: recordedNamesFor({ filename: "a.safetensors" }, ["a.safetensors", "b.safetensors"]),
        windows: recordedNamesFor({ filename: "a.safetensors" }, [{ value: "SD\\A.safetensors" }]),
        copy: recordedNamesFor({ filename: "a.safetensors", locations: [{ relpath: "x/c.ckpt" }] }, ["c.ckpt"]),
        nothing: recordedNamesFor({}, ["a.safetensors"]),
    },
};
refused = true;
results.refused = await open("1");
console.log(JSON.stringify(results));
"""


@unittest.skipUnless(shutil.which("node"), "needs node")
class CheckpointPictureTests(unittest.TestCase):
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

    def test_a_card_says_how_many_pictures_its_checkpoint_made(self):
        shown = self.results["none"]["shown"]
        self.assertIn("Base model not set · 86 pictures", shown)
        # A checkpoint with none reads as today's card.
        self.assertIn("Base model not set", shown)
        self.assertNotIn("0 pictures", " ".join(shown))

    def test_nothing_selected_asks_for_no_pictures(self):
        result = self.results["none"]
        self.assertEqual(result["lists"], [])
        self.assertIn("Pick a checkpoint to see the pictures it made.", result["shown"])

    def test_the_selected_checkpoint_lists_its_pictures_by_every_recorded_name(self):
        result = self.results["used"]
        self.assertEqual(
            sorted(q["sort"][0] for q in result["lists"]), ["DATE", "SCORE"]
        )
        for query in result["lists"]:
            self.assertEqual(
                query["comfyui_model"],
                ["chroma.safetensors", "SDXL/Chroma.safetensors"],
            )
            self.assertEqual(query["limit"], ["9"])
        said = " ".join(result["shown"])
        self.assertIn("86 pictures · last used 2 days ago", said)
        self.assertIn("Newest", said)

    def test_a_refused_names_route_still_lists_by_the_shelf_name(self):
        # PixlStash refuses a ComfyUI link key the recorded-names route.
        result = self.results["refused"]
        self.assertEqual(len(result["lists"]), 2)
        for query in result["lists"]:
            self.assertEqual(query["comfyui_model"], ["chroma.safetensors"])
        said = " ".join(result["shown"])
        self.assertNotIn("⚠", said)
        self.assertIn("86 pictures · last used 2 days ago", said)

    def test_a_checkpoint_with_no_pictures_asks_for_no_list(self):
        result = self.results["unused"]
        self.assertEqual(result["lists"], [])
        self.assertIn(
            "PixlStash has kept no pictures made with this checkpoint yet.",
            result["shown"],
        )
        self.assertNotIn("Newest", " ".join(result["shown"]))

    def test_names_match_by_basename(self):
        names = self.results["names"]
        self.assertEqual(names["plain"], ["a.safetensors"])
        self.assertEqual(names["windows"], ["a.safetensors", "SD\\A.safetensors"])
        self.assertEqual(names["copy"], ["a.safetensors", "x/c.ckpt", "c.ckpt"])
        self.assertEqual(names["nothing"], [])


if __name__ == "__main__":
    unittest.main()

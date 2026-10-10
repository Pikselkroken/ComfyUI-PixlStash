"""*Export to PixlStash* (``web/js/convert_workflow.js``): which document goes.

What is pinned: a workflow never saved, or changed since its save, is exported
as the canvas stands rather than refused; a saved file with no changes goes as
the file's own content, which is what PixlStash matches on; and a tab switch
while ComfyUI converts sends nothing.

The command is run in Node against a stand-in for ComfyUI's ``app``, so this
is skipped where there is no ``node``.
"""

import json
import pathlib
import shutil
import subprocess
import tempfile
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]

CANVAS = {"nodes": [{"id": 1, "type": "canvas"}], "links": []}
FILE = {"nodes": [{"id": 1, "type": "file"}], "links": []}
OUTPUT = {"1": {"class_type": "canvas", "inputs": {}}}

DRIVER = """
import { app } from "../../scripts/app.js";
import { exportActiveWorkflow } from "./convert_workflow.js";

const [canvas, file, output] = JSON.parse(process.argv[2]);
const saved = () => ({ filename: "wf", isTemporary: false, isModified: false, originalContent: JSON.stringify(file) });
const scenarios = {
    never_saved: { wf: { filename: "Unsaved Workflow", isTemporary: true, isModified: true, originalContent: null } },
    changed_since_save: { wf: { ...saved(), isModified: true } },
    saved: { wf: saved() },
    edited_while_converting: { wf: saved(), during: (wf) => { wf.isModified = true; } },
    switched_while_converting: { wf: saved(), during: () => { app.extensionManager.workflow.activeWorkflow = saved(); } },
    no_token: { wf: saved(), token: "" },
    no_workflow_store: { wf: undefined },
};
const results = {};
for (const [name, { wf, during, token = "example-token" }] of Object.entries(scenarios)) {
    const toasts = [];
    let sent = null;
    app.ui = { settings: { getSettingValue: () => token } };
    app.extensionManager = { toast: { add: (t) => toasts.push(t.severity) }, workflow: { activeWorkflow: wf } };
    app.graphToPrompt = async () => { during?.(wf); return { workflow: canvas, output }; };
    globalThis.fetch = async (url, init) => {
        sent = { url, body: JSON.parse(init.body) };
        return { ok: true, json: async () => ({ matched: false }) };
    };
    await exportActiveWorkflow();
    results[name] = { sent, toasts };
}
console.log(JSON.stringify(results));
"""


@unittest.skipUnless(shutil.which("node"), "needs node")
class ExportWorkflowTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        with tempfile.TemporaryDirectory() as tmp:
            js = pathlib.Path(tmp, "web", "js")
            js.mkdir(parents=True)
            pathlib.Path(tmp, "package.json").write_text('{"type": "module"}')
            pathlib.Path(tmp, "scripts").mkdir()
            pathlib.Path(tmp, "scripts", "app.js").write_text(
                "export const app = { registerExtension() {} };\n"
            )
            shutil.copy(ROOT / "web" / "js" / "convert_workflow.js", js)
            (js / "driver.js").write_text(DRIVER)
            done = subprocess.run(
                ["node", str(js / "driver.js"), json.dumps([CANVAS, FILE, OUTPUT])],
                capture_output=True,
                text=True,
                timeout=60,
            )
        if done.returncode:
            raise AssertionError(done.stderr)
        cls.results = json.loads(done.stdout.strip().splitlines()[-1])

    def _sent(self, scenario):
        result = self.results[scenario]
        self.assertEqual(result["toasts"], ["success"])
        self.assertEqual(result["sent"]["url"], "/pixlstash/workflows/convert")
        self.assertEqual(result["sent"]["body"]["output"], OUTPUT)
        return result["sent"]["body"]

    def test_a_workflow_never_saved_is_exported_as_the_canvas_stands(self):
        body = self._sent("never_saved")
        self.assertEqual(body["workflow"], CANVAS)
        self.assertEqual(body["name"], "Unsaved Workflow")

    def test_unsaved_changes_are_exported_not_refused(self):
        self.assertEqual(self._sent("changed_since_save")["workflow"], CANVAS)

    def test_an_unchanged_saved_file_goes_as_the_file(self):
        self.assertEqual(self._sent("saved")["workflow"], FILE)

    def test_an_edit_while_converting_sends_the_canvas(self):
        self.assertEqual(self._sent("edited_while_converting")["workflow"], CANVAS)

    def test_a_frontend_with_no_workflow_store_still_exports(self):
        body = self._sent("no_workflow_store")
        self.assertEqual(body["workflow"], CANVAS)
        self.assertEqual(body["name"], "workflow")

    def test_a_tab_switch_while_converting_sends_nothing(self):
        self.assertEqual(
            self.results["switched_while_converting"],
            {"sent": None, "toasts": ["warn"]},
        )

    def test_no_token_sends_nothing(self):
        self.assertEqual(self.results["no_token"], {"sent": None, "toasts": ["error"]})


if __name__ == "__main__":
    unittest.main()

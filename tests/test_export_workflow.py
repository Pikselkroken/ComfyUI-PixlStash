"""*Save as new workflow in PixlStash* (``web/js/convert_workflow.js``): which document goes.

What is pinned: a workflow never saved, or changed since its save, is exported
as the canvas stands rather than refused; a saved file with no changes goes as
the file's own content, which is what PixlStash matches on; a tab switch while
ComfyUI converts sends nothing; the menu command is the plain export, named
as the second of a pair with *Save to PixlStash* and listed after it; and a
LoRA PixlStash left out because it is somebody's is said.

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
import "./save_workflow.js";

const [canvas, file, output] = JSON.parse(process.argv[2]);
const saved = () => ({ filename: "wf", isTemporary: false, isModified: false, originalContent: JSON.stringify(file) });
const scenarios = {
    // A fresh tab: ComfyUI gives it content and does not call it modified.
    never_saved: { wf: { ...saved(), filename: "Unsaved Workflow", isTemporary: true } },
    changed_since_save: { wf: { ...saved(), isModified: true } },
    saved: { wf: saved(), reply: { matched: true, name: "stored" } },
    saved_but_not_held: { wf: saved() },
    edited_while_converting: { wf: saved(), during: (wf) => { wf.isModified = true; } },
    saved_again_while_converting: { wf: saved(), during: (wf) => { wf.originalContent = "{}"; } },
    switched_while_converting: { wf: saved(), during: () => { app.extensionManager.workflow.activeWorkflow = saved(); } },
    no_token: { wf: saved(), token: "" },
    no_workflow_store: { store: false },
    refused_by_pixlstash: { wf: saved(), ok: false, reply: { error: "Owner token required" } },
    a_persons_lora: { wf: saved(), reply: { matched: false, left_out: [
        { class_type: "LoraLoader", lora: "Anna v2", people: ["Anna"] },
        { class_type: "PixlStashMultiAdapterLoader", lora: "Ben", people: ["Ben"] },
    ] } },
};
const results = {};
for (const [name, s] of Object.entries(scenarios)) {
    const { wf, during, store = true, token = "example-token", ok = true, reply = { matched: false } } = s;
    const toasts = [];
    let sent = null;
    app.ui = { settings: { getSettingValue: (key) => (key === "PixlStash.APIToken" ? token : "") } };
    app.extensionManager = { toast: { add: (t) => toasts.push([t.severity, t.detail]) } };
    if (store) app.extensionManager.workflow = { activeWorkflow: wf };
    app.graphToPrompt = async () => { during?.(wf); return { workflow: canvas, output }; };
    globalThis.fetch = async (url, init) => {
        sent = { url, method: init.method, auth: init.headers.Authorization, body: JSON.parse(init.body) };
        return { ok, status: ok ? 200 : 403, json: async () => reply };
    };
    await exportActiveWorkflow();
    results[name] = { sent, toasts };
}

// The menu command, called as ComfyUI calls it: with its own argument.
const command = app.extensions[0].commands.find((c) => c.id === "PixlStash.ConvertWorkflow");
results.command = { label: command.label, menu: app.extensions[0].menuCommands, sent: null };
app.extensionManager = { workflow: { activeWorkflow: { ...saved(), isModified: true } } };
globalThis.fetch = async (url, init) => {
    results.command.sent = JSON.parse(init.body).workflow;
    return { ok: true, json: async () => ({}) };
};
await command.function({ savedOnly: true });

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
                "export const app = {\n"
                "    extensions: [],\n"
                "    registerExtension(extension) { this.extensions.push(extension); },\n"
                "};\n"
            )
            for name in ("convert_workflow.js", "save_workflow.js", "workflow_link.js"):
                shutil.copy(ROOT / "web" / "js" / name, js)
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

    def _sent(self, scenario, toast="success", says="as a new workflow"):
        """The body *scenario* posted, having ended in one *toast* that *says*."""
        result = self.results[scenario]
        [(severity, detail)] = result["toasts"]
        self.assertEqual(severity, toast)
        self.assertIn(says, detail)
        sent = result["sent"]
        self.assertEqual(sent["url"], "/pixlstash/workflows/convert")
        self.assertEqual(sent["method"], "POST")
        self.assertEqual(sent["auth"], "Bearer example-token")
        self.assertEqual(sent["body"]["output"], OUTPUT)
        return sent["body"]

    def _nothing_sent(self, scenario, toast, says):
        result = self.results[scenario]
        self.assertIsNone(result["sent"])
        [(severity, detail)] = result["toasts"]
        self.assertEqual(severity, toast)
        self.assertIn(says, detail)

    def test_a_workflow_never_saved_is_exported_as_the_canvas_stands(self):
        body = self._sent("never_saved")
        self.assertEqual(body["workflow"], CANVAS)
        self.assertEqual(body["name"], "Unsaved Workflow")

    def test_unsaved_changes_are_exported_not_refused(self):
        self.assertEqual(self._sent("changed_since_save")["workflow"], CANVAS)

    def test_an_unchanged_saved_file_goes_as_the_file(self):
        body = self._sent("saved", says="PixlStash can now run stored.")
        self.assertEqual(body["workflow"], FILE)

    def test_a_file_pixlstash_did_not_hold_is_a_warning(self):
        body = self._sent("saved_but_not_held", toast="warn")
        self.assertEqual(body["workflow"], FILE)

    def test_an_edit_or_a_save_while_converting_sends_the_canvas(self):
        for scenario in ("edited_while_converting", "saved_again_while_converting"):
            with self.subTest(scenario=scenario):
                self.assertEqual(self._sent(scenario)["workflow"], CANVAS)

    def test_a_frontend_with_no_workflow_store_still_exports(self):
        body = self._sent("no_workflow_store")
        self.assertEqual(body["workflow"], CANVAS)
        self.assertEqual(body["name"], "workflow")

    def test_a_tab_switch_while_converting_sends_nothing(self):
        self._nothing_sent("switched_while_converting", "warn", "switched workflows")

    def test_no_token_sends_nothing(self):
        self._nothing_sent("no_token", "error", "API token")

    def test_what_pixlstash_refused_reaches_the_toast(self):
        [(severity, detail)] = self.results["refused_by_pixlstash"]["toasts"]
        self.assertEqual(severity, "error")
        self.assertIn("Owner token required", detail)

    def test_the_menu_command_is_the_plain_export(self):
        command = self.results["command"]
        self.assertEqual(command["label"], "Save as new workflow in PixlStash")
        # One group with *Save to PixlStash* first: two groups came out in
        # whichever order ComfyUI happened to load the two files.
        self.assertEqual(
            command["menu"],
            [
                {
                    "path": ["PixlStash"],
                    "commands": ["PixlStash.SaveWorkflow", "PixlStash.ConvertWorkflow"],
                }
            ],
        )
        # Unsaved changes go, whatever ComfyUI hands the command.
        self.assertEqual(command["sent"], CANVAS)

    def test_a_persons_lora_in_an_ordinary_loader_is_said(self):
        """One sentence for the ordinary loader; the PixlStash loader's own
        rows come off without one, since that is what it is for."""
        toasts = self.results["a_persons_lora"]["toasts"]
        self.assertEqual(len(toasts), 2)
        self.assertEqual(
            toasts[1],
            [
                "warn",
                "Anna v2 is Anna's and is not saved with the workflow. "
                "Pick people in the PixlStash loader beside the model.",
            ],
        )


if __name__ == "__main__":
    unittest.main()

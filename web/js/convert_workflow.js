/**
 * Convert for PixlStash: store ComfyUI's API conversion of the open workflow.
 *
 * PixlStash pulls the workflows ComfyUI has saved, but they are editor
 * documents and only ComfyUI's `graphToPrompt` can turn one into a runnable
 * API graph. This posts both to PixlStash through the
 * `/pixlstash/workflows/convert` proxy.
 *
 * It sends the saved file's content, not the graph on the canvas: PixlStash
 * finds the pulled workflow by comparing documents, and ComfyUI rewrites the
 * editor graph every time it serialises it. So a workflow with unsaved
 * changes is refused — with none, `output` is the conversion of that file.
 */

import { app } from "../../scripts/app.js";

const COMMAND_ID = "PixlStash.ConvertWorkflow";

function notify(severity, detail) {
    const toast = app.extensionManager?.toast;
    if (toast?.add) {
        toast.add({ severity, summary: "PixlStash", detail, life: 8000 });
    } else {
        console.warn(`[PixlStash] ${detail}`);
    }
}

/**
 * Convert the active (saved, unmodified) workflow and post it to PixlStash,
 * toasting the outcome. Shared by the menu command and the opener, which
 * calls it on a file it has just opened.
 */
export async function convertActiveWorkflow() {
    const token = (app.ui.settings.getSettingValue("PixlStash.APIToken", "") ?? "").trim();
    if (!token) {
        notify("error", "Set your PixlStash API token in Settings › PixlStash, then convert again.");
        return;
    }
    const wf = app.extensionManager?.workflow?.activeWorkflow;
    if (!wf || wf.isTemporary || wf.isModified || !wf.originalContent) {
        notify("warn", "Save this workflow first, then convert it.");
        return;
    }
    const name = wf.filename;
    const saved = wf.originalContent;
    try {
        // Throws on a node with no definition (a pack not installed here):
        // that error is the answer, so it goes to the toast as is.
        const { output } = await app.graphToPrompt();
        // An edit, save or tab switch while that ran would pair this output
        // with a different document.
        const now = app.extensionManager?.workflow?.activeWorkflow;
        if (now !== wf || wf.isTemporary || wf.isModified || wf.originalContent !== saved) {
            notify("warn", "The workflow changed while converting. Save it, then convert again.");
            return;
        }
        const resp = await fetch("/pixlstash/workflows/convert", {
            method: "POST",
            headers: { Authorization: `Bearer ${token}`, "Content-Type": "application/json" },
            body: JSON.stringify({ name, workflow: JSON.parse(saved), output }),
        });
        const body = await resp.json().catch(() => ({}));
        if (!resp.ok) throw new Error(body.detail || body.error || `HTTP ${resp.status}`);
        // Not matched means PixlStash stored it as a new workflow beside
        // whatever card the reader meant to convert: say so.
        notify(
            body.matched ? "success" : "warn",
            body.matched
                ? `PixlStash can now run ${body.name || name}.`
                : `PixlStash can now run ${body.name || name}, stored as a new workflow.`,
        );
    } catch (err) {
        console.error("[PixlStash] could not convert workflow", name, err);
        notify("error", `Could not convert ${name}: ${err.message}`);
    }
}

app.registerExtension({
    name: "ComfyUI.PixlStash.ConvertWorkflow",
    commands: [
        {
            id: COMMAND_ID,
            label: "Convert for PixlStash",
            icon: "pi pi-sync",
            function: convertActiveWorkflow,
        },
    ],
    menuCommands: [{ path: ["PixlStash"], commands: [COMMAND_ID] }],
});

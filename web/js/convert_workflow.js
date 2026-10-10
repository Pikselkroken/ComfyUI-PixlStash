/**
 * Export to PixlStash: send the open workflow, with ComfyUI's API conversion
 * of it, to PixlStash.
 *
 * A workflow is an editor document and only ComfyUI's `graphToPrompt` can
 * turn one into a runnable API graph. This posts both to PixlStash through
 * the `/pixlstash/workflows/convert` proxy.
 *
 * Nothing has to be saved first: what goes is the canvas as it stands, which
 * is what `output` was converted from. The one exception is a saved file with
 * no changes, which goes as the file's own content: PixlStash finds a
 * workflow it pulled by comparing documents, and ComfyUI rewrites the editor
 * graph every time it serialises it.
 *
 * The command id, the file and the route keep "convert" in their names: the
 * id is what a keybinding is saved against.
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

/** The file's content when `wf` is a saved file with no changes, else null. */
function savedContent(wf) {
    return (wf && !wf.isTemporary && !wf.isModified && wf.originalContent) || null;
}

/**
 * Convert the active workflow, saved or not, and post it to PixlStash,
 * toasting the outcome. Shared by the menu command and the opener, which
 * calls it on a file it has just opened.
 */
export async function exportActiveWorkflow() {
    const token = (app.ui.settings.getSettingValue("PixlStash.APIToken", "") ?? "").trim();
    if (!token) {
        notify("error", "Set your PixlStash API token in Settings › PixlStash, then export again.");
        return;
    }
    const wf = app.extensionManager?.workflow?.activeWorkflow;
    const name = wf?.filename || "workflow";
    const saved = savedContent(wf);
    try {
        // Throws on a node with no definition (a pack not installed here):
        // that error is the answer, so it goes to the toast as is.
        const { workflow, output } = await app.graphToPrompt();
        // A tab switch while that ran would send another workflow's graph
        // under this one's name.
        if (app.extensionManager?.workflow?.activeWorkflow !== wf) {
            notify("warn", "You switched workflows while exporting. Export again.");
            return;
        }
        // An edit or a save while that ran: the file's content no longer
        // pairs with `output`, the canvas document always does.
        const unchanged = saved !== null && savedContent(wf) === saved;
        const resp = await fetch("/pixlstash/workflows/convert", {
            method: "POST",
            headers: { Authorization: `Bearer ${token}`, "Content-Type": "application/json" },
            body: JSON.stringify({ name, workflow: unchanged ? JSON.parse(saved) : workflow, output }),
        });
        const body = await resp.json().catch(() => ({}));
        if (!resp.ok) throw new Error(body.detail || body.error || `HTTP ${resp.status}`);
        // Not matched means PixlStash holds it beside whatever workflow the
        // reader had in mind, not over it: say so.
        notify(
            "success",
            body.matched
                ? `PixlStash can now run ${body.name || name}.`
                : `Exported ${body.name || name} to PixlStash as a new workflow.`,
        );
    } catch (err) {
        console.error("[PixlStash] could not export workflow", name, err);
        notify("error", `Could not export ${name}: ${err.message}`);
    }
}

app.registerExtension({
    name: "ComfyUI.PixlStash.ConvertWorkflow",
    commands: [
        {
            id: COMMAND_ID,
            label: "Export to PixlStash",
            icon: "pi pi-upload",
            function: exportActiveWorkflow,
        },
    ],
    menuCommands: [{ path: ["PixlStash"], commands: [COMMAND_ID] }],
});

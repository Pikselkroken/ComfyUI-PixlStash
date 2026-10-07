/**
 * Open a PixlStash workflow in ComfyUI.
 *
 * PixlStash's Workflow tab opens ComfyUI at `?pixlstash_workflow=<id>`.
 * ComfyUI takes no workflow from a URL, so this reads the id, fetches the
 * graph through the `/pixlstash/workflow_graph` proxy and hands it to
 * ComfyUI's own API-format loader (`app.loadApiJson`), which rebuilds it as an
 * editor graph in a new tab.
 *
 * The key is taken off the address at once, so reloading the tab does not
 * fetch the graph again over whatever the reader has changed since.
 */

import { app } from "../../scripts/app.js";
import { convertActiveWorkflow } from "./convert_workflow.js";

const PARAM = "pixlstash_workflow";
// A workflow id: `auto:` and the core hash, or `manual:` and a uuid hex.
const KEY_RE = /^(?:auto:[0-9a-f]{64}|manual:[0-9a-f]{32})$/;

// How long to wait for ComfyUI to finish restoring its own tabs.
const STARTUP_TIMEOUT_MS = 30000;

function takeKeyFromUrl() {
    const url = new URL(window.location.href);
    const key = url.searchParams.get(PARAM);
    if (key === null) return null;
    url.searchParams.delete(PARAM);
    window.history.replaceState(window.history.state, "", url.toString());
    return key;
}

/**
 * Resolve once ComfyUI's start-up is over.
 *
 * Extension `setup` runs BEFORE ComfyUI restores the tabs it had open, so a
 * graph loaded straight away is replaced by the previous workflow. The
 * workspace spinner is up for exactly that restore.
 *
 * ponytail: polls the spinner; an event from ComfyUI would be better if it
 * ever grows one.
 */
async function startupFinished() {
    const deadline = Date.now() + STARTUP_TIMEOUT_MS;
    // Let the restore begin before asking whether it is over.
    await new Promise((resolve) => setTimeout(resolve, 0));
    while (app.extensionManager?.spinner && Date.now() < deadline) {
        await new Promise((resolve) => setTimeout(resolve, 100));
    }
}

/**
 * True for a path relative to ComfyUI's `workflows/` directory: no leading
 * slash or backslash, no drive letter, no `..` segment, ends in `.json`.
 */
export function isSafeWorkflowFile(path) {
    if (typeof path !== "string" || !path.toLowerCase().endsWith(".json")) return false;
    if (/^[\\/]/.test(path) || /^[a-zA-Z]:/.test(path) || path.includes("\0")) return false;
    return !path.split(/[\\/]/).some((seg) => seg === ".." || seg === "");
}

/**
 * Open the workflow's own ComfyUI file, bound as ComfyUI's saved workflow so
 * Save writes back. Returns true when opened; false (with the reason logged)
 * means the caller must fall back to the stored graph.
 *
 * The graph is deliberately left untouched: tagging it would mark the user's
 * file modified and write PixlStash data into it on Save.
 */
async function openComfyFile(path) {
    if (!isSafeWorkflowFile(path)) {
        console.info("[PixlStash] comfyui_file is not a safe relative .json path; using stored graph:", path);
        return false;
    }
    try {
        const store = app.extensionManager?.workflow;
        if (!store?.getWorkflowByPath || !store.syncWorkflows || typeof app.loadGraphData !== "function") {
            console.info("[PixlStash] this ComfyUI frontend has no workflow store API; using stored graph");
            return false;
        }
        const full = `workflows/${path}`;
        let wf = store.getWorkflowByPath(full);
        if (!wf) {
            await store.syncWorkflows();
            wf = store.getWorkflowByPath(full);
        }
        if (!wf) {
            console.info("[PixlStash] ComfyUI has no workflow file", full, "; using stored graph");
            return false;
        }
        // Already the visible tab: nothing to do. Otherwise loadGraphData with
        // the workflow object opens (or switches to) that one tab by path.
        if (store.isActive?.(wf)) return true;
        await wf.load();
        const loaded = await app.loadGraphData(wf.activeState, true, true, wf);
        if (loaded === false) {
            console.info("[PixlStash] ComfyUI refused to load", full, "; using stored graph");
            return false;
        }
        return true;
    } catch (err) {
        console.info("[PixlStash] could not open the ComfyUI file; using stored graph:", err);
        return false;
    }
}

function notify(severity, detail) {
    const toast = app.extensionManager?.toast;
    if (toast?.add) {
        toast.add({ severity, summary: "PixlStash", detail, life: 8000 });
    } else {
        console.warn(`[PixlStash] ${detail}`);
    }
}

async function openFromUrl(key) {
    if (!KEY_RE.test(key)) {
        notify("error", "That is not a PixlStash workflow link.");
        return;
    }
    const token = (app.ui.settings.getSettingValue("PixlStash.APIToken", "") ?? "").trim();
    if (!token) {
        notify(
            "error",
            "Set your PixlStash API token in Settings › PixlStash, then open the link from PixlStash again.",
        );
        return;
    }
    let body;
    try {
        const resp = await fetch(
            `/pixlstash/workflow_graph?${new URLSearchParams({ workflow_key: key })}`,
            { headers: { Authorization: `Bearer ${token}` } },
        );
        body = await resp.json().catch(() => ({}));
        if (!resp.ok) throw new Error(body.detail || body.error || `HTTP ${resp.status}`);
    } catch (err) {
        console.error("[PixlStash] could not fetch workflow", key, err);
        notify("error", `Could not open that workflow from PixlStash: ${err.message}`);
        return;
    }
    await startupFinished();
    if (body.comfyui_file && (await openComfyFile(body.comfyui_file))) {
        // PixlStash has no runnable graph for this file: convert the tab just
        // opened (unmodified by construction) with the menu command's code.
        // Not awaited into the open: failures toast and never block the tab.
        if (body.needs_conversion) await convertActiveWorkflow();
        return;
    }
    if (!body.workflow) {
        notify("error", body.detail || "PixlStash could not open that workflow.");
        return;
    }
    app.loadApiJson(body.workflow, `${body.name || "PixlStash workflow"}.json`);
    // Tag the canvas so pictures this graph saves are filed on the manual
    // workflow (PixlStash reads it from the saved `workflow` PNG chunk).
    if (key.startsWith("manual:")) {
        app.graph.extra ??= {};
        app.graph.extra.pixlstash_workflow_id = key;
    }
    // A graph rebuilt from a stored recipe keeps no seed, and can name models
    // PixlStash has forgotten: say so, or the first queue fails unexplained.
    if (body.seedless || body.forgotten) {
        const parts = [];
        if (body.seedless) parts.push("set a seed");
        if (body.forgotten) parts.push(`pick ${body.forgotten} model(s) PixlStash no longer knows`);
        notify("warn", `Rebuilt from a saved recipe: ${parts.join(" and ")} before you queue it.`);
    }
}

app.registerExtension({
    name: "ComfyUI.PixlStash.OpenWorkflow",

    async setup() {
        const key = takeKeyFromUrl();
        // Not awaited: `setup` is awaited by ComfyUI's start-up, and this has
        // to wait for the rest of that start-up to finish.
        if (key !== null) {
            openFromUrl(key).catch((err) => {
                console.error("[PixlStash] could not open workflow", key, err);
                notify("error", `Could not open that workflow: ${err.message}`);
            });
        }
    },
});

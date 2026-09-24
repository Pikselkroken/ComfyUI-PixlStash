/**
 * Open a PixlStash workflow in ComfyUI.
 *
 * PixlStash's Workflow tab opens ComfyUI at `?pixlstash_workflow=<key>`.
 * ComfyUI takes no workflow from a URL, so this reads the key, fetches the
 * graph through the `/pixlstash/workflow_graph` proxy and hands it to
 * ComfyUI's own API-format loader (`app.loadApiJson`), which rebuilds it as an
 * editor graph in a new tab.
 *
 * The key is taken off the address at once, so reloading the tab does not
 * fetch the graph again over whatever the reader has changed since.
 */

import { app } from "../../scripts/app.js";

const PARAM = "pixlstash_workflow";
const KEY_RE = /^[0-9a-f]{64}$/;

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
            "Set your PixlStash API token in Settings › PixlStash to open PixlStash workflows here.",
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
        if (!resp.ok) throw new Error(body.error || `HTTP ${resp.status}`);
    } catch (err) {
        console.error("[PixlStash] could not fetch workflow", key, err);
        notify("error", `Could not open that workflow from PixlStash: ${err.message}`);
        return;
    }
    await startupFinished();
    app.loadApiJson(body.workflow, `${body.name || "PixlStash workflow"}.json`);
}

app.registerExtension({
    name: "ComfyUI.PixlStash.OpenWorkflow",

    async setup() {
        const key = takeKeyFromUrl();
        // Not awaited: `setup` is awaited by ComfyUI's start-up, and this has
        // to wait for the rest of that start-up to finish.
        if (key !== null) openFromUrl(key);
    },
});

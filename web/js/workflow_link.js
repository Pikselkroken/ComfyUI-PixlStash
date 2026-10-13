/**
 * What ties a canvas to the PixlStash workflow it was opened from.
 *
 * *Open in ComfyUI* writes two things into the graph, where ComfyUI saves
 * them with the file: the workflow's id (`extra.pixlstash_workflow_id`) and
 * the file it was opened as (`extra.pixlstash_file`). A save goes back to the
 * workflow as its next version only from THAT file (`save_workflow.js`). The
 * id alone is not enough: ComfyUI writes `extra` into every picture the
 * canvas makes and into every duplicate of the tab, so a picture dragged back
 * in, a duplicated tab or somebody else's file can all carry it. Those reach
 * PixlStash only through *Save as new workflow in PixlStash*, as anything else does.
 *
 * Shared by the opener, the save hook and the export, and importing nothing
 * but ComfyUI's `app`.
 */

import { app } from "../../scripts/app.js";

export const TAG = "pixlstash_workflow_id";
/** The file, under ComfyUI's user folder, that PixlStash opened the workflow as. */
export const FILE_TAG = "pixlstash_file";

/** A workflow id: `auto:` and the core hash, or `manual:` and a uuid hex. */
export const KEY_RE = /^(?:auto:[0-9a-f]{64}|manual:[0-9a-f]{32})$/;

export const PEOPLE_LOADER = "PixlStashMultiAdapterLoader";

/** `show` on the loader PixlStash places: must match SHOW_PEOPLE in the node. */
export const SHOW_PEOPLE = "People who fit";

export function notify(severity, detail, life = 8000) {
    const toast = app.extensionManager?.toast;
    if (toast?.add) {
        toast.add({ severity, summary: "PixlStash", detail, life });
    } else {
        console.warn(`[PixlStash] ${detail}`);
    }
}

export function apiToken() {
    return (app.ui.settings.getSettingValue("PixlStash.APIToken", "") ?? "").trim();
}

/** The whole workflow's graph, also while a subgraph is on the canvas. */
export function rootGraph() {
    return app.rootGraph ?? app.graph;
}

/** The PixlStash workflow the canvas is linked to, or null. */
export function linkedWorkflow(graph = rootGraph()) {
    const key = graph?.extra?.[TAG];
    return typeof key === "string" && KEY_RE.test(key) ? key : null;
}

/** The file the canvas was opened as from PixlStash, or null. */
export function linkedFile(graph = rootGraph()) {
    const file = graph?.extra?.[FILE_TAG];
    return typeof file === "string" && file ? file : null;
}

/**
 * How the canvas stands with PixlStash: `"linked"` when it is the file
 * PixlStash opened, so saving it saves to its workflow; `"copy"` when it
 * carries a workflow's id and is another file, which only *Save to PixlStash*
 * sends; null when it carries none.
 */
export function linkState() {
    if (!linkedWorkflow()) return null;
    const path = app.extensionManager?.workflow?.activeWorkflow?.path ?? "";
    return linkedFile() === path ? "linked" : "copy";
}

/**
 * What each opened file was read at, by its path:
 * `{ version, waiting, opening, saved }`.
 *
 * `version` goes back with a save so PixlStash can say a newer one was
 * replaced; `waiting` says the graph was read with the changes that waited in
 * PixlStash's Workflow tab, which the first save back clears; `opening` names
 * this opening, so PixlStash keeps its saves as one version. In memory only:
 * after a reload a save goes without them. It is still sent, as a version of
 * its own, the "newer version replaced" sentence cannot be said, and changes
 * that waited in PixlStash stay waiting there.
 */
export const openings = new Map();

/** An id for one opening: 32 hex characters. */
export function newOpening() {
    const bytes = crypto.getRandomValues(new Uint8Array(16));
    return [...bytes].map((b) => b.toString(16).padStart(2, "0")).join("");
}

const quiet = new Set();

/**
 * Run `fn` without the save hook answering for `wf`: for the saves this pack
 * makes itself (the file an opened workflow is given, a re-linked canvas),
 * which are not the reader saving their work. Per workflow, so a save of
 * another tab that ends meanwhile is still sent.
 */
export async function quietly(wf, fn) {
    quiet.add(wf);
    try {
        return await fn();
    } finally {
        quiet.delete(wf);
    }
}

export const isQuiet = (wf) => quiet.has(wf);

/**
 * Bring the workflow's saved state up to the canvas before ComfyUI writes
 * it. ComfyUI's own save does this first; a save made here has to as well, or
 * what was just put on the canvas (the link, the loader) is not in the file.
 * The method has had three names across frontend versions.
 */
export function flushCanvas(wf) {
    const tracker = wf?.changeTracker;
    const flush = tracker?.prepareForSave ?? tracker?.captureCanvasState ?? tracker?.checkState;
    if (!flush) {
        // The save still goes ahead, of whatever state ComfyUI last captured.
        console.warn("[PixlStash] this ComfyUI gives no way to bring a workflow's saved state up to the canvas; the file written now may lack the PixlStash link until it is next saved");
        return;
    }
    flush.call(tracker);
}

// A save ComfyUI wrote that PixlStash never received, by file. Kept in the
// browser's storage so it outlives a reload: opening the workflow from
// PixlStash again must not write over the only copy of that work.
const UNSENT = "PixlStash.unsent:";

export function markUnsent(path, unsent) {
    try {
        if (unsent) localStorage.setItem(UNSENT + path, "1");
        else localStorage.removeItem(UNSENT + path);
    } catch (err) {
        console.info("[PixlStash] could not note an unsent save for", path, err);
    }
}

export function isUnsent(path) {
    try {
        return localStorage.getItem(UNSENT + path) === "1";
    } catch (err) {
        console.info("[PixlStash] could not read whether", path, "has an unsent save", err);
        return false;
    }
}

/**
 * One sentence per LoRA PixlStash left out because it is somebody's and sits
 * in an ordinary loader. A person's row of the PixlStash loader is left out
 * too, without a sentence: that is what the loader is for.
 */
export function leftOutSentences(leftOut) {
    return (Array.isArray(leftOut) ? leftOut : [])
        .filter((entry) => entry && entry.class_type !== PEOPLE_LOADER)
        .map((entry) => {
            const whose = (entry.people ?? []).join(" and ") || "somebody";
            return `${entry.lora || "A LoRA"} is ${whose}'s and is not saved with the workflow. ` +
                "Pick people in the PixlStash loader beside the model.";
        });
}

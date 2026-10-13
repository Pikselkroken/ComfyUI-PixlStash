/**
 * Saving a workflow opened from PixlStash saves it back to PixlStash.
 *
 * A file *Open in ComfyUI* made carries its workflow's id and its own path
 * (`workflow_link.js`). When ComfyUI saves that file, this sends the canvas,
 * with ComfyUI's API conversion of it, to the `/pixlstash/workflows/save`
 * proxy, and PixlStash stores it as the workflow's next version. No question
 * is asked: opening it from PixlStash already said which workflow is meant.
 * What happened is said in a toast.
 *
 * **A run opened from the Run popup is saved as a new workflow** (`NEW_TAG`):
 * the first save of it makes that workflow, the canvas is linked to it, and
 * every save after is a version of the new one. It goes to a proxy route of
 * its own (`/pixlstash/workflows/save_new`), so a ComfyUI that has this script
 * and not yet that route (updated, not restarted) stores nothing rather than
 * saving the run over the workflow it came from.
 *
 * **Only that file.** A canvas that carries a workflow's id but is not the
 * file PixlStash opened (a duplicated tab, a picture dragged back in, a copy,
 * somebody else's file) is not sent by saving it, and is told so once.
 *
 * **The hook is the workflow store's own `saveWorkflow` action**, watched
 * with Pinia's `$onAction`. That is ComfyUI's internal interface, not an
 * extension point (checked against frontend 1.53.10), so when it is not there
 * this says so once and the *Save to PixlStash* command in the PixlStash menu
 * does the same by hand. It never stops sending without saying so.
 *
 * **The menu item is greyed out on a canvas that carries no workflow's id**
 * (`greyOutWhenUnlinked`), where there is nothing for it to save over.
 *
 * Autosave calls the same action as Ctrl+S, so it is sent like any save.
 * PixlStash stores nothing for a graph it already holds and keeps the saves
 * of one opening as one version, and a sentence that would repeat with every
 * autosave is said once a minute.
 */

import { app } from "../../scripts/app.js";
import { exportActiveWorkflow } from "./convert_workflow.js";
import {
    FILE_TAG, NEW_TAG, PEOPLE_LOADER, TAG, apiToken, flushCanvas, isQuiet, leftOutSentences,
    linkedFile, linkedWorkflow, markUnsent, notify, openings, quietly, rootGraph,
} from "./workflow_link.js";

const COMMAND_ID = "PixlStash.SaveWorkflow";
// Named for what the command was first called; a keybinding is saved against it.
const NEW_COMMAND_ID = "PixlStash.ConvertWorkflow";
const EXPORT_HINT = "Pick PixlStash › Save as new workflow in PixlStash to send it there.";
const NO_HOOK_SAID = "PixlStash.noSaveHookSaid";

// What was last said for each file, so an autosave does not repeat it.
const REPEAT_MS = 60000;
const said = new Map();

/** Say `detail` about the file at `path`, unless it was just said. */
function sayOnce(path, severity, detail) {
    const key = `${path}\n${detail}`;
    if (Date.now() - (said.get(key) ?? -Infinity) < REPEAT_MS) return;
    said.set(key, Date.now());
    notify(severity, detail);
}

/** The id a saved file's own content is tagged with, or null. */
function tagOfContent(content) {
    try {
        return linkedWorkflow(JSON.parse(content ?? "null"));
    } catch {
        return null;
    }
}

/**
 * Send the canvas to the PixlStash workflow it is linked to, and say what
 * came of it. `wf` is the workflow ComfyUI just saved; `byHand` is the menu
 * command, which is the reader saying which workflow they mean: it sends any
 * canvas that carries a workflow's id, and answers for one that carries none.
 */
export async function saveToPixlStash(wf, { byHand = false } = {}) {
    const store = app.extensionManager?.workflow;
    const active = store?.activeWorkflow;
    const name = wf?.filename || active?.filename || "This workflow";
    if (wf && active && wf.path !== active.path) {
        // Only the canvas can be converted, and it shows another workflow.
        if (tagOfContent(wf.content)) {
            // The file PixlStash opened now holds a save PixlStash never got.
            if (linkedFile(JSON.parse(wf.content)) === wf.path) markUnsent(wf.path, true);
            notify(
                "warn",
                `${name} was saved while another workflow was showing, so it was not sent to ` +
                    "PixlStash. Open it and save it again.",
            );
        }
        return;
    }
    const key = linkedWorkflow();
    const path = active?.path ?? "";
    if (!key) {
        if (byHand) notify("warn", `${name} was not opened from PixlStash. ${EXPORT_HINT}`);
        return;
    }
    if (!byHand && linkedFile() !== path) {
        // It carries a workflow's id and is not the file PixlStash opened.
        sayOnce(
            path,
            "info",
            `${name} is not the file PixlStash opened, so saving it does not save to PixlStash. ` +
                `${EXPORT_HINT} To save it over the workflow it came from, pick ` +
                "PixlStash › Save to PixlStash.",
        );
        return;
    }
    const failed = (reason) => {
        // Only a save of that file is unsent. A by-hand send made no ComfyUI
        // save, so marking the file would refuse opening it later for nothing.
        if (!byHand) markUnsent(path, true);
        sayOnce(
            path,
            "error",
            `Could not save ${name} to PixlStash: ${reason} ` +
                (byHand
                    ? "Nothing was sent; pick PixlStash › Save to PixlStash to try again."
                    : "It is saved in ComfyUI, and the next save tries again."),
        );
    };
    const token = apiToken();
    if (!token) return failed("set your PixlStash API token in Settings › PixlStash.");

    let prompt;
    try {
        // Throws on a node with no definition (a pack not installed here).
        prompt = await app.graphToPrompt();
    } catch (err) {
        return failed(`a node will not convert to a runnable graph (${err.message}).`);
    }
    if (store?.activeWorkflow !== active) {
        // ComfyUI wrote the file and PixlStash did not get it, as when it fails.
        if (!byHand) markUnsent(path, true);
        notify("warn", `You switched workflows while ${name} was being sent to PixlStash. Save it again.`);
        return;
    }
    const opened = openings.get(path) ?? {};
    // Opened from PixlStash's Run popup: a new workflow, not a version.
    const asNew = !!rootGraph().extra?.[NEW_TAG];
    // PixlStash keeps the document as the new workflow's layout, which must
    // not open marked as a run again.
    let editor = prompt.workflow;
    if (asNew && editor?.extra) {
        editor = { ...editor, extra: { ...editor.extra } };
        delete editor.extra[NEW_TAG];
    }
    let resp, body;
    try {
        resp = await fetch(asNew ? "/pixlstash/workflows/save_new" : "/pixlstash/workflows/save", {
            method: "POST",
            headers: { Authorization: `Bearer ${token}`, "Content-Type": "application/json" },
            body: JSON.stringify({
                workflow_key: key,
                workflow: editor,
                output: prompt.output,
                read_version: opened.version ?? null,
                waiting_applied: !!opened.waiting,
                opening: opened.opening ?? null,
            }),
        });
        body = await resp.json().catch(() => ({}));
    } catch (err) {
        return failed(`PixlStash cannot be reached (${err.message}).`);
    }
    if (asNew && [404, 405].includes(resp.status) && !body.error && !body.detail) {
        // ComfyUI's own answer, not PixlStash's through the proxy: the route
        // is not there, so its Python is from before the pack was updated.
        return failed("ComfyUI has to be restarted before it can save this as a new workflow: its PixlStash nodes were updated since it started.");
    }
    if (resp.status === 404) {
        // Somebody else's file, or a workflow deleted since: nothing to save over.
        sayOnce(path, "warn", `${name} is not a workflow in this PixlStash, so nothing was stored. ${EXPORT_HINT}`);
        return;
    }
    if (!resp.ok) return failed(`${body.detail || body.error || `HTTP ${resp.status}`}`);
    markUnsent(path, false);

    const stored = body.name || name;
    const again = !!opened.saved;
    openings.set(path, { ...opened, version: body.version ?? null, waiting: false, saved: true });
    if (body.new_workflow) {
        const why = asNew ? "" : ": the checkpoint changed";
        if (store?.activeWorkflow === active) {
            // The canvas is the new workflow's from here on, and so is its file.
            rootGraph().extra[TAG] = body.workflow_id;
            delete rootGraph().extra[NEW_TAG];
            await quietly(active, async () => {
                flushCanvas(active);
                await store.saveWorkflow(active);
            }).catch((err) => console.warn("[PixlStash] could not re-save the re-linked canvas", err));
            notify("success", `Saved to PixlStash as a new workflow, ${stored}${why}.`);
        } else {
            // Another canvas is showing now: its link is not this save's to change.
            notify(
                "warn",
                `Saved to PixlStash as a new workflow, ${stored}${why}. ` +
                    (asNew
                        ? `${name} would save as another new workflow; open ${stored} from PixlStash to go on with it.`
                        : `${name} is still linked to the workflow it was opened from; open the new one from PixlStash.`),
            );
        }
    } else if (body.unchanged) {
        // Picking people is not a change to the workflow, and neither is a
        // save of what PixlStash already holds. Only the first is said.
        const people = (body.left_out ?? []).some((e) => e?.class_type === PEOPLE_LOADER);
        if (people) sayOnce(path, "info", "Nothing new to save. The people picked here stay in ComfyUI.");
        else if (byHand) notify("info", `Nothing new to save: PixlStash already has ${stored} as it is here.`);
    } else {
        notify(
            body.replaced_newer ? "warn" : "success",
            `Saved to PixlStash: ${stored}, version ${body.version}.` +
                (body.replaced_newer
                    ? " A newer version in PixlStash was replaced; it can be had back there."
                    : ""),
            // The second save of an opening onward is the same news again.
            again && !body.replaced_newer ? 3000 : 8000,
        );
    }
    for (const sentence of leftOutSentences(body.left_out)) sayOnce(path, "warn", sentence);
}

/**
 * *Save As* copies the file, link and all. The copy is another file, so
 * saving it would not be sent anyway; taking the link off it also keeps the
 * pictures it makes from being filed on the workflow it was copied from.
 *
 * Only the copy's content is changed, and that is enough: this runs in the
 * `after` of the store's `saveAs` action, which is synchronous, and ComfyUI's
 * own Save As opens the copy straight after it returns (`openWorkflow`),
 * loading this content into the canvas. So the live graph is the unlinked one.
 */
function unlinkCopy(copy) {
    if (!copy || !tagOfContent(copy.content)) return;
    const state = JSON.parse(copy.content);
    delete state.extra[TAG];
    delete state.extra[FILE_TAG];
    delete state.extra[NEW_TAG];
    copy.content = copy.originalContent = JSON.stringify(state);
    notify("info", `Saved as a copy that is not linked to PixlStash. ${EXPORT_HINT}`);
}

/**
 * Grey *Save to PixlStash* out in the menu on a canvas that carries no
 * workflow's id.
 *
 * ComfyUI's extension interface has no "disabled" for a command, so this
 * reaches the menu's own store through Pinia's registry (`_p._s`, internal to
 * Pinia; checked against frontend 1.53.10) and gives the item PrimeVue's
 * `disabled`, which the menu reads each time it opens. Where any of that is
 * missing the item stays enabled and the command answers with a toast, as it
 * does from a keybinding or the command palette.
 */
function greyOutWhenUnlinked(store) {
    const find = (items) => {
        for (const item of items ?? []) {
            const found = item?.comfyCommand?.id === COMMAND_ID ? item : find(item?.items);
            if (found) return found;
        }
        return null;
    };
    const item = find(store?._p?._s?.get?.("menuItem")?.menuItems);
    if (item) item.disabled = () => !linkedWorkflow();
    else console.info("[PixlStash] this ComfyUI's menu cannot be reached, so Save to PixlStash is not greyed out on a canvas that is not linked");
}

/** Say, once in this browser, that this ComfyUI gives no sign of a save. */
function sayNoHook() {
    try {
        if (localStorage.getItem(NO_HOOK_SAID)) return;
        localStorage.setItem(NO_HOOK_SAID, "1");
    } catch (err) {
        console.info("[PixlStash] could not remember that the missing save hook was said", err);
    }
    notify(
        "warn",
        "This ComfyUI does not tell PixlStash when a workflow is saved. After saving one " +
            "opened from PixlStash, pick PixlStash › Save to PixlStash.",
    );
}

app.registerExtension({
    name: "ComfyUI.PixlStash.SaveWorkflow",
    commands: [
        {
            id: COMMAND_ID,
            label: "Save to PixlStash",
            icon: "pi pi-save",
            function: () => saveToPixlStash(null, { byHand: true }),
        },
        {
            id: NEW_COMMAND_ID,
            label: "Save as new workflow in PixlStash",
            icon: "pi pi-upload",
            // ComfyUI may pass the command its own arguments.
            function: () => exportActiveWorkflow(),
        },
    ],
    // One group, so the pair keeps this order however the files load.
    menuCommands: [{ path: ["PixlStash"], commands: [COMMAND_ID, NEW_COMMAND_ID] }],

    async setup() {
        const store = app.extensionManager?.workflow;
        greyOutWhenUnlinked(store);
        if (typeof store?.$onAction !== "function" || typeof store.saveWorkflow !== "function") {
            sayNoHook();
            return;
        }
        store.$onAction(({ name, args, after }) => {
            if (name === "saveAs") after(unlinkCopy);
            if (name === "saveWorkflow") {
                after(() => {
                    if (isQuiet(args[0])) return;
                    saveToPixlStash(args[0]).catch((err) => {
                        console.error("[PixlStash] could not save back to PixlStash", err);
                        notify("error", `Could not save to PixlStash: ${err.message}`);
                    });
                });
            }
        });
    },
});

/**
 * A sign over the canvas that it is linked to PixlStash.
 *
 * Shown only on the file PixlStash opened (`linkState` is `"linked"`), which
 * is where a plain save goes back to the workflow: without it nothing on
 * screen tells that file from any other. Not on a run opened from the Run
 * popup (`"new"`): a save of that makes a new workflow, which the sign's
 * words would deny.
 *
 * It is this pack's own element. ComfyUI's top-bar badges are read once from
 * each extension when it registers, so they cannot follow the canvas.
 *
 * Kept right after every action of ComfyUI's workflow store (opening, closing
 * and saving a workflow are all actions) and after every graph load. The
 * store's actions are ComfyUI's internal interface, as in `save_workflow.js`;
 * where they cannot be watched the sign follows graph loads alone.
 */

import { app } from "../../scripts/app.js";
import { linkState } from "./workflow_link.js";

const TITLE = "This workflow was opened from PixlStash. Saving it saves it there as the workflow's next version.";

let badge = null;

function makeBadge() {
    const el = document.createElement("div");
    el.id = "pixlstash-link-badge";
    el.title = TITLE;
    // ComfyUI's own palette variables, so it follows the colour theme.
    el.style.cssText =
        "position:absolute;bottom:8px;left:50%;transform:translateX(-50%);z-index:10;pointer-events:auto;" +
        "display:flex;align-items:center;gap:6px;padding:4px 10px;border-radius:999px;" +
        "font-size:12px;line-height:16px;white-space:nowrap;cursor:default;user-select:none;" +
        "background:var(--comfy-menu-bg);color:var(--descrip-text);border:1px solid var(--border-color);";
    const icon = document.createElement("i");
    icon.className = "pi pi-link";
    icon.style.fontSize = "12px";
    el.append(icon, "Linked to PixlStash");
    return el;
}

/**
 * Where the sign goes: the foot of the part of the window the canvas shows
 * through, centred, below the graph and, unless the canvas is very narrow,
 * clear of ComfyUI's canvas menu in the corner. ComfyUI narrows that part for
 * its side and bottom panels, so the sign stays clear of them too. Its class
 * is ComfyUI's own (checked against frontend 1.53.10); without it the sign
 * goes at the foot of the page.
 */
function place(el) {
    (document.querySelector(".graph-canvas-panel") ?? document.body).append(el);
}

/** Show the sign when the canvas is the file PixlStash opened, else hide it. */
export function refreshLinkBadge() {
    const linked = linkState() === "linked";
    if (!badge && !linked) return;
    badge ??= makeBadge();
    // ComfyUI rebuilds that part of the window (focus mode), taking the sign with it.
    if (!badge.isConnected) place(badge);
    badge.style.display = linked ? "flex" : "none";
}

app.registerExtension({
    name: "ComfyUI.PixlStash.LinkBadge",

    afterConfigureGraph() {
        refreshLinkBadge();
    },

    async setup() {
        const store = app.extensionManager?.workflow;
        if (typeof store?.$onAction !== "function") return;
        store.$onAction(({ after, onError }) => {
            after(refreshLinkBadge);
            onError?.(refreshLinkBadge);
        });
    },
});

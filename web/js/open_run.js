/**
 * Open a run from PixlStash's Run popup in ComfyUI.
 *
 * The Run popup's *Open in ComfyUI* has PixlStash build the graph that run
 * would submit (the workflow with the prompt, seed, models, LoRAs, stages and
 * pictures of the popup in it) and hold it for a few minutes. It then opens
 * ComfyUI at `?pixlstash_run=<opening>&pixlstash_of=<workflow id>`, and this
 * reads the two and opens that graph (`openFromUrl` in `open_workflow.js`).
 *
 * What opens is not the workflow: a save of it goes to PixlStash as a NEW
 * workflow and the one it came from is left as it was (`NEW_TAG`,
 * `save_workflow.js`).
 *
 * Its own parameters, not `pixlstash_workflow` with one more beside it: a pack
 * from before this would open the workflow itself from that, without the
 * run's changes, and save over it. PixlStash asks ComfyUI whether this script
 * is loaded before it offers the button.
 */

import { app } from "../../scripts/app.js";
import { openFromUrl } from "./open_workflow.js";
import { notify } from "./workflow_link.js";

const PARAM = "pixlstash_run";
const OF = "pixlstash_of";
// What PixlStash holds a run as: 32 hex characters.
const RUN_RE = /^[0-9a-f]{32}$/;

/** The run in the address, taken off it so a reload does not open it again. */
function takeRunFromUrl() {
    const url = new URL(window.location.href);
    const run = url.searchParams.get(PARAM);
    if (run === null) return null;
    const key = url.searchParams.get(OF) ?? "";
    url.searchParams.delete(PARAM);
    url.searchParams.delete(OF);
    window.history.replaceState(window.history.state, "", url.toString());
    return { run, key };
}

app.registerExtension({
    name: "ComfyUI.PixlStash.OpenRun",

    async setup() {
        const { run, key } = takeRunFromUrl() ?? {};
        if (run == null) return;
        if (!RUN_RE.test(run)) {
            notify("error", "That is not a link from PixlStash's Run popup.");
            return;
        }
        // Not awaited, as for a workflow's own link: it waits for the rest
        // of ComfyUI's start-up.
        openFromUrl(key, false, run).catch((err) => {
            console.error("[PixlStash] could not open the run of", key, err);
            notify("error", `Could not open that run: ${err.message}`);
        });
    },
});

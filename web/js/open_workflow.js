/**
 * Open a PixlStash workflow in ComfyUI.
 *
 * PixlStash's Workflow tab opens ComfyUI at `?pixlstash_workflow=<id>`.
 * ComfyUI takes no workflow from a URL, so this reads the id and fetches the
 * workflow through the `/pixlstash/workflow_graph` proxy. What opens:
 *
 * - **The layout it was last saved from ComfyUI with**, when PixlStash has
 *   one: positions, groups and notes as they were left, with the workflow's
 *   current values written over it. **Only when it is that graph**: the
 *   layout is converted on the canvas and kept if it comes out as exactly
 *   the graph PixlStash sent. PixlStash stores a layout on the word of
 *   whoever saved it, so this check, made here at the moment of use, is what
 *   ties the two together. Otherwise the runnable graph opens, which
 *   ComfyUI's API loader (`app.loadApiJson`) arranges. A layout saved with
 *   a stock node where the graph now has one of this pack's (or the other
 *   way round) is still that layout: the node is replaced where it stands
 *   (`swapNodes`) before the check is made.
 * - **Linked to its workflow**: the id and the file's own path go into the
 *   graph, for an automatic workflow as for a manual one, so a save of this
 *   file goes back to it (`save_workflow.js`) and pictures queued here are
 *   filed on it.
 * - **With a loader for people**, where PixlStash says a run would splice a
 *   person's LoRA: straight after the model source, with nobody picked, so a
 *   workflow opened and queued untouched runs as the workflow alone. One
 *   after every copy, where the workflow loads the same model several times.
 * - **As a saved file** in `pixlstash/` of ComfyUI's workflows, so Ctrl+S
 *   saves and never asks for a filename. ComfyUI makes a tab the file it is
 *   loaded under the name of, so the graph is loaded as `pixlstash/<name>`
 *   and saved once.
 *
 * - **With this pack's nodes** where PixlStash can put one in place of a
 *   stock node (`pixlstash_nodes`): its savers for Save Image and Save Video,
 *   so what is queued here comes back to PixlStash, its Adapter Loader for a
 *   LoRA its shelf holds, its Picture Loader for a picture input PixlStash
 *   knows the picture of, and its Checkpoint Loader where PixlStash and
 *   ComfyUI share a computer. A link that also carries `pixlstash_original`
 *   (PixlStash's *Open original in ComfyUI*) opens the workflow's own nodes.
 *
 * The key is taken off the address at once, so reloading the tab does not
 * fetch the graph again over whatever the reader has changed since.
 */

import { app } from "../../scripts/app.js";
import {
    FILE_TAG, KEY_RE, PEOPLE_LOADER, SHOW_PEOPLE, TAG, apiToken, flushCanvas, isUnsent,
    linkedWorkflow, newOpening, notify, openings, quietly, rootGraph,
} from "./workflow_link.js";

const PARAM = "pixlstash_workflow";
// Beside it in the link: open the workflow's own nodes, none of this pack's.
const ORIGINAL = "pixlstash_original";
// The folder of ComfyUI's workflows that opened workflows are saved in.
const FOLDER = "workflows/pixlstash/";

// How long to wait for ComfyUI to finish restoring its own tabs.
const STARTUP_TIMEOUT_MS = 30000;
// How long to wait for a graph handed to ComfyUI's API loader to be on the canvas.
const LOAD_TIMEOUT_MS = 30000;

function takeKeyFromUrl() {
    const url = new URL(window.location.href);
    const key = url.searchParams.get(PARAM);
    if (key === null) return null;
    const original = url.searchParams.has(ORIGINAL);
    url.searchParams.delete(PARAM);
    url.searchParams.delete(ORIGINAL);
    window.history.replaceState(window.history.state, "", url.toString());
    return { key, original };
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

// Windows refuses these as a file name whatever the extension, case aside.
const RESERVED = /^(con|prn|aux|nul|com[1-9]|lpt[1-9])$/i;

/** A file name ComfyUI's workflows folder takes on any system, from a name. */
export function fileStem(name) {
    const stem = String(name ?? "")
        .replace(/[\\/:*?"<>|\u0000-\u001f]+/g, " ")
        .replace(/\s+/g, " ")
        .replace(/^[ .]+|[ .]+$/g, "");
    if (!stem) return "PixlStash workflow";
    return RESERVED.test(stem) ? `${stem} workflow` : stem;
}

// ComfyUI's API loader puts a subgraph's node ("75:70") on the canvas as
// "75_70", so an id PixlStash sends is looked for under that name too.
// ponytail: not the "75_70_" it takes when a node is already called "75_70".
const nodeById = (graph, id) =>
    graph.getNodeById(id) ?? graph.getNodeById(Number(id)) ??
    graph.getNodeById(String(id).replaceAll(":", "_")) ?? null;

/**
 * Write the values of the API graph `api` over the canvas, node by node.
 *
 * The layout is laid out for this wiring (PixlStash sends it for no other),
 * but its values are from when it was saved: the prompt and seed of that
 * moment, and nothing typed in PixlStash since. `api` is what a run would
 * submit now, so its values are the ones to open with. A node the graph does
 * not hold (a note, a bypassed node) keeps its own.
 *
 * Nodes inside a subgraph ("12:3") are not reached, and a widget fed by
 * another node takes no value here. The caller converts the canvas afterwards
 * and uses the layout only if every value arrived (`differsFrom`), so a value
 * this could not write costs the layout, never the value.
 */
export function writeValues(graph, api) {
    for (const [id, entry] of Object.entries(api ?? {})) {
        const node = nodeById(graph, id);
        if (!node?.widgets) continue;
        for (const [name, value] of Object.entries(entry?.inputs ?? {})) {
            if (Array.isArray(value)) continue;   // a connection, not a value
            const widget = node.widgets.find((w) => w.name === name);
            if (!widget || widget.value === value) continue;
            widget.value = value;
            widget.callback?.(value);
        }
    }
}

/** The link with this id on `graph`, whichever way this LiteGraph keeps them. */
const linkOf = (graph, id) =>
    (typeof graph.links?.get === "function" ? graph.links.get(id) : graph.links?.[id]) ?? null;

/**
 * Make each node on the canvas the class the API graph `api` names for it.
 *
 * A layout holds the nodes it was saved with, and PixlStash may since have
 * put one of this pack's where a stock one was, or the stock one back: a
 * saver, a LoRA loader, the picture or the checkpoint loader. Which it does
 * depends on what its shelf and library hold that day, so without this a
 * layout would be dropped each time that changed.
 *
 * The node is replaced where it stands by one of the class `api` names,
 * under the same id, and wired as PixlStash wires the swap: what it read, by
 * the input's name, and what read it, by the output's position. Its values
 * are the graph's (`writeValues`, which the caller runs next). Returns the
 * ids replaced.
 *
 * Nothing here decides whether the layout is used. A node this could not
 * replace, or a wire the new node has no place for, leaves a canvas that
 * converts to something else, and the caller's check (`differsFrom`) then
 * drops the layout. Nodes inside a subgraph are not reached.
 */
export function swapNodes(graph, api, LiteGraph = globalThis.LiteGraph) {
    const swapped = [];
    for (const [id, entry] of Object.entries(api ?? {})) {
        const old = graph.getNodeById(id) ?? graph.getNodeById(Number(id));
        if (!old || !entry?.class_type || old.type === entry.class_type) continue;
        const fresh = LiteGraph?.createNode?.(entry.class_type);
        if (!fresh) continue;
        // Read before the old node goes: removing it takes its links along.
        const reads = (old.inputs ?? []).flatMap((input) => {
            const link = input.link == null ? null : linkOf(graph, input.link);
            const from = link ? graph.getNodeById(link.origin_id) : null;
            return from ? [{ from, slot: link.origin_slot, name: input.name }] : [];
        });
        const readBy = (old.outputs ?? []).flatMap((output, slot) =>
            (output.links ?? []).flatMap((linkId) => {
                const link = linkOf(graph, linkId);
                const to = link ? graph.getNodeById(link.target_id) : null;
                return to ? [{ slot, to, input: link.target_slot }] : [];
            }),
        );
        graph.remove(old);
        fresh.id = old.id;
        graph.add(fresh);
        fresh.pos = [...old.pos];
        // No narrower than it was laid out, and as tall as its own widgets need.
        if (fresh.size && old.size) fresh.size[0] = Math.max(fresh.size[0], old.size[0]);
        fresh.mode = old.mode;
        if (old.color) fresh.color = old.color;
        if (old.bgcolor) fresh.bgcolor = old.bgcolor;
        // A name the owner gave it stays; the stock node's own name does not.
        if (old.title && old.title !== old.constructor?.title) fresh.title = old.title;
        for (const { from, slot, name } of reads) from.connect(slot, fresh, name);
        for (const { slot, to, input } of readBy) fresh.connect(slot, to, input);
        swapped.push(String(id));
    }
    return swapped;
}

const DIGEST_ROW = /^adapter_sha256(_\d+)?$/;
const isLink = (value) => Array.isArray(value) && value.length === 2 && typeof value[1] === "number";

/**
 * `output` as PixlStash stores a graph: every loader for people that holds
 * nobody taken out, what read it reading what it read (MODEL and CLIP
 * through, `trigger_words` of nobody the empty string). The loader a layout
 * kept is on the canvas and in no graph PixlStash sends.
 */
export function withoutEmptyPeopleLoaders(output) {
    const graph = structuredClone(output ?? {});
    const through = new Map();
    for (const [id, node] of Object.entries(graph)) {
        if (node?.class_type !== PEOPLE_LOADER) continue;
        const inputs = node.inputs ?? {};
        if (Object.entries(inputs).some(([name, value]) => DIGEST_ROW.test(name) && (isLink(value) || value))) continue;
        through.set(id, [inputs.model, inputs.clip, ""]);
    }
    const standIn = (value) => {
        // Through a chain of them, and never round a ring of them.
        for (let step = 0; isLink(value) && through.has(String(value[0])); step++) {
            if (step > through.size) return undefined;
            value = through.get(String(value[0]))[value[1]];
        }
        return value;
    };
    for (const [id, node] of Object.entries(graph)) {
        if (through.has(id)) {
            delete graph[id];
            continue;
        }
        for (const [name, value] of Object.entries(node?.inputs ?? {})) {
            if (!isLink(value) || !through.has(String(value[0]))) continue;
            const found = standIn(value);
            if (found === undefined) delete node.inputs[name];
            else node.inputs[name] = found;
        }
    }
    return graph;
}

/**
 * Whether the canvas holds the value the graph does. Whole numbers exactly:
 * a seed is one, and a tolerance scaled to its size would pass a seed a
 * million away. Only a fraction is allowed the noise of float arithmetic.
 */
const sameValue = (a, b) => {
    if (typeof a !== "number" || typeof b !== "number") return JSON.stringify(a) === JSON.stringify(b);
    if (Number.isInteger(a) && Number.isInteger(b)) return a === b;
    return Math.abs(a - b) <= 1e-9 * Math.max(1, Math.abs(a), Math.abs(b));
};

/**
 * Why the canvas is not the graph PixlStash sent, or null when it is.
 *
 * `output` is what the canvas converts to; `api` is the graph. The same nodes
 * of the same classes, wired the same, and holding every value `api` holds.
 * A value only the canvas has (a widget `api` does not name) is the canvas's.
 */
export function differsFrom(output, api) {
    const canvas = withoutEmptyPeopleLoaders(output);
    const ids = Object.keys(api ?? {});
    const extra = Object.keys(canvas).find((id) => !(id in api));
    if (extra !== undefined) return `node ${extra} is not in the workflow`;
    for (const id of ids) {
        const mine = canvas[id], theirs = api[id];
        if (!mine) return `node ${id} is missing`;
        if (mine.class_type !== theirs.class_type) return `node ${id} is a ${mine.class_type}`;
        const wired = Object.entries(mine.inputs ?? {}).find(([name, value]) => isLink(value) && !isLink(theirs.inputs?.[name]));
        if (wired) return `node ${id} reads ${wired[0]} from another node`;
        for (const [name, value] of Object.entries(theirs.inputs ?? {})) {
            const held = mine.inputs?.[name];
            const same = isLink(value)
                ? isLink(held) && String(held[0]) === String(value[0]) && held[1] === value[1]
                : sameValue(held, value);
            if (!same) return `node ${id} differs in ${name}`;
        }
    }
    return null;
}

/**
 * Load the API graph `workflow` as the tab `loadAs`, and resolve once it is.
 *
 * `await app.loadApiJson()` does not say so by itself: an extension that wraps
 * it and drops its promise (rgthree-comfy does) ends the await with the canvas
 * cleared and no node made yet. What came next was then done to an empty
 * canvas, and saved into whichever tab was showing before. ComfyUI makes the
 * nodes and then, last, shows the graph as a tab of the name it was loaded
 * under: both are waited for. `tabbed` is false where ComfyUI's workflow store
 * cannot be asked what is showing.
 */
async function loadApi(workflow, loadAs, tabbed) {
    await app.loadApiJson(workflow, `${loadAs}.json`);
    const ids = Object.keys(workflow ?? {});
    const path = `workflows/${loadAs}.json`;
    // One node, not every node: ComfyUI makes them all in one go, so there is
    // no canvas with some of them, and a frontend before 1.52 leaves out a node
    // whose class it does not have, which is what the owner came here to fix.
    const loaded = () =>
        (!ids.length || ids.some((id) => nodeById(rootGraph(), id))) &&
        (!tabbed || app.extensionManager?.workflow?.activeWorkflow?.path === path);
    const deadline = Date.now() + LOAD_TIMEOUT_MS;
    while (!loaded()) {
        // Nothing is placed on, or saved from, a canvas that is not this graph.
        if (Date.now() > deadline) {
            throw new Error("ComfyUI did not finish loading its graph. Open it from PixlStash again.");
        }
        await new Promise((resolve) => setTimeout(resolve, 50));
    }
}

/**
 * Put the loader for people on the canvas where `place` says: reading the
 * model source's MODEL (and CLIP), with every input that read those reading
 * the loader's instead. A workflow that loads the same model several times
 * gets one after every copy (`place.places`), or the copies without one would
 * run without the person. A PixlStash from before `places` sends the one
 * place as `place` itself. Returns the first loader, or null with `why` said.
 *
 * Everything is found before anything is changed, so a canvas this cannot be
 * done on is left exactly as it was.
 */
export function placePeopleLoader(graph, place, LiteGraph = globalThis.LiteGraph) {
    const refuse = (why) => {
        notify("warn", `This workflow opened without the PixlStash loader for people: ${why}`);
        return null;
    };
    const places = place?.places?.length ? place.places : place?.model ? [place] : [];
    if (!places.length) {
        return refuse(place?.reason || "PixlStash could not tell where it goes.");
    }
    const splices = places.map(({ model, clip, rewires }) => ({
        model, clip,
        source: model ? nodeById(graph, model.node_id) : null,
        clipSource: clip ? nodeById(graph, clip.node_id) : null,
        rewires: (rewires ?? []).map((rewire) => {
            const reader = nodeById(graph, rewire.node_id);
            return { reader, slot: reader?.findInputSlot?.(rewire.field) ?? -1, type: rewire.type };
        }),
    }));
    const lost = ({ source, clip, clipSource, rewires }) =>
        !source || (clip && !clipSource) || rewires.some((r) => !r.reader || r.slot < 0);
    if (splices.some(lost)) {
        return refuse("the model is loaded where the canvas cannot be rewired (inside a subgraph).");
    }
    const loaders = splices.map(() => LiteGraph?.createNode?.(PEOPLE_LOADER));
    if (!loaders.every(Boolean)) {
        return refuse("this ComfyUI has no PixlStash Multi Adapter Loader. Update the ComfyUI-PixlStash nodes.");
    }
    splices.forEach(({ model, clip, source, clipSource, rewires }, at) => {
        const loader = loaders[at];
        graph.add(loader);
        const show = loader.widgets?.find((w) => w.name === "show");
        if (show) {
            show.value = SHOW_PEOPLE;
            show.callback?.(SHOW_PEOPLE);
        }
        // Beside the model source, where the eye looks for what follows it.
        // This is where it stays in a kept layout; a graph ComfyUI arranged is
        // arranged again by the caller with the loader in it, which gives it a
        // column of its own (here it would sit on top of the next one).
        loader.pos = [source.pos[0] + (source.size?.[0] ?? 300) + 40, source.pos[1]];
        source.connect(model.output, loader, "model");
        if (clipSource) clipSource.connect(clip.output, loader, "clip");
        // MODEL is the loader's output 0 and CLIP its output 1.
        for (const { reader, slot, type } of rewires) loader.connect(type === "CLIP" ? 1 : 0, reader, slot);
    });
    return loaders[0];
}

/**
 * The loader for people a kept layout still holds: one the graph does not,
 * showing people, and running. A loader the owner bypassed or muted is in no
 * graph either, and is theirs: its rows are not this code's to empty.
 */
function keptPeopleLoader(graph, api) {
    return (graph.nodes ?? graph._nodes ?? []).find(
        (node) =>
            node.type === PEOPLE_LOADER && !(String(node.id) in (api ?? {})) && !node.mode &&
            node.widgets?.find((w) => w.name === "show")?.value === SHOW_PEOPLE,
    );
}

/** The PixlStash workflow a file in ComfyUI's workflows is linked to, or null. */
async function workflowOfFile(file) {
    try {
        const content = (await file.load?.())?.content ?? file.content;
        return linkedWorkflow(JSON.parse(content ?? "null"));
    } catch (err) {
        console.info("[PixlStash] could not read", file.path, "to see whose it is:", err);
        return null;
    }
}

/**
 * The file in `pixlstash/` this workflow opens as: `{ path, mine }`, or null
 * when ComfyUI's workflow store cannot be asked.
 *
 * A file already there under the name is this workflow's own from an earlier
 * open only when it carries this workflow's id: that one is `mine`, and is
 * opened over with what PixlStash holds now. Any other file (another workflow
 * of the same name) is left alone and this takes the next name.
 */
async function fileFor(name, key) {
    const store = app.extensionManager?.workflow;
    if (typeof store?.getWorkflowByPath !== "function" || typeof store.saveWorkflow !== "function") {
        console.info("[PixlStash] this ComfyUI's workflow store cannot save an opened workflow as a file");
        return null;
    }
    // What the store lists is as old as its last read of the folder. A file
    // it has not seen would be taken for no file: ComfyUI then refuses the
    // save rather than write over it, and the tab is left unsaved.
    try {
        await store.syncWorkflows?.();
    } catch (err) {
        console.info("[PixlStash] could not read ComfyUI's workflows again before looking for the file", err);
    }
    const stem = fileStem(name);
    for (let n = 1; n < 100; n++) {
        const path = `${FOLDER}${stem}${n === 1 ? "" : ` (${n})`}.json`;
        const there = store.getWorkflowByPath(path);
        if (!there) return { path, mine: null };
        if ((await workflowOfFile(there)) === key) return { path, mine: there };
    }
    return null;
}

/** `path` as ComfyUI names a workflow it loads: no folder of its own, no `.json`. */
const loadName = (path) => path.slice("workflows/".length, -".json".length);

/**
 * Make the tab just loaded a saved file, so Ctrl+S saves it without asking
 * for a name, and return its path (null when the tab is left unsaved).
 *
 * ComfyUI makes a tab the file it was loaded under the name of, so a new
 * file needs only the save. This workflow's own file from an earlier open
 * (`mine`) may be open, or showing: the canvas is loaded into that file's
 * own tab, which is the one way ComfyUI replaces a file whatever state its
 * tab is in, and the tab it was built in is closed.
 *
 * The file's path is written into the graph once the tab is that file: a save
 * is sent back to PixlStash from that file and from no other
 * (`workflow_link.js`), and a tab that is not it is left untagged.
 */
async function saveOpened(mine) {
    const store = app.extensionManager?.workflow;
    const path = mine?.path ?? store?.activeWorkflow?.path;
    if (!path?.startsWith(FOLDER)) return null;
    if (mine) {
        const built = store.activeWorkflow;
        if ((await app.loadGraphData(rootGraph().serialize(), true, true, mine)) === false) return null;
        if (built && built !== mine) await store.closeWorkflow?.(built);
    }
    const opened = store.activeWorkflow;
    if (opened?.path !== path) return null;
    // Only now is the tab this file, so only now is the file's path written
    // into the graph: a save is sent back to PixlStash from that file and from
    // no other (`workflow_link.js`), and a tab that turned out not to be it
    // must not be left pointing at a file that was never written.
    rootGraph().extra[FILE_TAG] = path;
    // Quietly: this save is the file being made, not the reader's work.
    await quietly(opened, async () => {
        flushCanvas(opened);
        await store.saveWorkflow(opened);
    });
    return path;
}

/** An open tab of this workflow's file with changes nobody saved, or null. */
function unsavedTabOf(key) {
    const store = app.extensionManager?.workflow;
    return (store?.openWorkflows ?? []).find(
        (wf) => wf?.path?.startsWith(FOLDER) && wf.isModified && linkedWorkflow(wf.activeState) === key,
    ) ?? null;
}

async function openFromUrl(key, original = false) {
    if (!KEY_RE.test(key)) {
        notify("error", "That is not a PixlStash workflow link.");
        return;
    }
    const token = apiToken();
    if (!token) {
        notify(
            "error",
            "Set your PixlStash API token in Settings › PixlStash, then open the link from PixlStash again.",
        );
        return;
    }
    let body;
    try {
        // `waiting`: the changes waiting in PixlStash's Workflow tab are in
        // the graph, and come back in the version saved here. `all_stages`:
        // a stage cut out of a graph saved back could not be switched on again.
        const query = new URLSearchParams({ workflow_key: key, waiting: "true", all_stages: "true" });
        // This pack's savers and Adapter Loader in place of the stock nodes,
        // unless the link asks for the workflow as it is.
        if (!original) query.set("pixlstash_nodes", "true");
        const resp = await fetch(`/pixlstash/workflow_graph?${query}`, {
            headers: { Authorization: `Bearer ${token}` },
        });
        body = await resp.json().catch(() => ({}));
        if (!resp.ok) throw new Error(body.detail || body.error || `HTTP ${resp.status}`);
    } catch (err) {
        console.error("[PixlStash] could not fetch workflow", key, err);
        notify("error", `Could not open that workflow from PixlStash: ${err.message}`);
        return;
    }
    await startupFinished();
    if (!body.workflow) {
        notify("error", body.detail || "PixlStash could not open that workflow.");
        return;
    }
    const name = body.name || "PixlStash workflow";
    const unsaved = unsavedTabOf(key);
    if (unsaved) {
        // Opening over it would throw the reader's unsaved work away.
        notify(
            "warn",
            `${name} is already open here with changes that are not saved. Save or close it, ` +
                "then open it from PixlStash again.",
        );
        return;
    }

    // Loaded under the file's own name, so ComfyUI makes the tab that file.
    // Over this workflow's own file the canvas is built in a tab beside it
    // first (`saveOpened`), under a name no file has.
    const file = await fileFor(name, key);
    if (file?.mine && isUnsent(file.path)) {
        // The file holds a save PixlStash never received: the only copy.
        notify(
            "warn",
            `${name} has a save in ComfyUI that PixlStash never received. Open it in ComfyUI ` +
                "and save it again to send it, then open it from PixlStash again.",
        );
        return;
    }
    const loadAs = !file ? fileStem(name) : loadName(file.path) + (file.mine ? " (opening)" : "");
    let arranged = false;
    if (body.editor) {
        // Whatever goes wrong with a layout costs the layout and nothing
        // else: one that will not load, will not convert, is of something
        // else, or holds a value this could not write. The graph itself then
        // opens, arranged by ComfyUI.
        let differs;
        try {
            if ((await app.loadGraphData(body.editor, true, true, loadAs)) === false) {
                differs = "ComfyUI would not load it";
            } else {
                swapNodes(rootGraph(), body.workflow);
                writeValues(rootGraph(), body.workflow);
                differs = differsFrom((await app.graphToPrompt()).output, body.workflow);
            }
        } catch (err) {
            differs = `it will not load or convert (${err.message})`;
        }
        if (differs) console.info("[PixlStash] the layout kept for", name, "is not used:", differs);
        arranged = !differs;
    }
    if (!arranged) await loadApi(body.workflow, loadAs, !!file);
    const graph = rootGraph();
    // Linked, so a save here goes back to this workflow and the pictures this
    // graph makes are filed on it (PixlStash reads the tag from their
    // `workflow` chunk).
    graph.extra ??= {};
    graph.extra[TAG] = key;

    // A layout that was used converts to the graph, so a loader for people
    // it kept holds nobody: it is where it was left, and no other is put on.
    const kept = arranged ? keptPeopleLoader(graph, body.workflow) : null;
    if (!kept && body.people_loader && placePeopleLoader(graph, body.people_loader) && !arranged) {
        // The API loader arranged the graph before the loader was on it, in
        // columns with no room for one more: arranged again, after placing.
        graph.arrange?.();
    }

    let path = null;
    try {
        if (file) path = await saveOpened(file.mine);
    } catch (err) {
        console.warn("[PixlStash] could not save the opened workflow as a file", err);
    }
    if (path) {
        openings.set(path, {
            version: body.version ?? null,
            waiting: !!body.waiting_applied,
            opening: newOpening(),
        });
    } else {
        notify(
            "info",
            `${name} is open as an unsaved workflow, so saving it does not save to PixlStash. ` +
                "Save it in ComfyUI, then pick PixlStash › Save to PixlStash to send it.",
        );
    }
    // A graph rebuilt from a stored recipe can hold no seed, or name models
    // PixlStash has forgotten: say so, or the first queue fails unexplained.
    const parts = [];
    if (body.seedless) parts.push("set a seed");
    if (body.forgotten) parts.push(`pick ${body.forgotten} model(s) PixlStash no longer knows`);
    if (parts.length) {
        notify("warn", `Rebuilt from a saved recipe: ${parts.join(" and ")} before you queue it.`);
    }
}

app.registerExtension({
    name: "ComfyUI.PixlStash.OpenWorkflow",

    async setup() {
        const { key, original } = takeKeyFromUrl() ?? {};
        // Not awaited: `setup` is awaited by ComfyUI's start-up, and this has
        // to wait for the rest of that start-up to finish.
        if (key != null) {
            openFromUrl(key, original).catch((err) => {
                console.error("[PixlStash] could not open workflow", key, err);
                notify("error", `Could not open that workflow: ${err.message}`);
            });
        }
    },
});

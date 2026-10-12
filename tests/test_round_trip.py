"""The round trip with PixlStash: a workflow opened from it saves back to it.

``web/js/open_workflow.js`` opens a workflow as a saved file linked to its
PixlStash workflow, with its layout and a loader for people;
``web/js/save_workflow.js`` sends every save of that file back as the
workflow's next version and says what came of it.

What is pinned: which document opens and under what file name, that a layout
is used only when it converts to the graph PixlStash sent, where the loader
for people is wired, that the opener's own save is not sent, that only the
file PixlStash opened saves back, what a save sends and each outcome's toast,
that a copy made with *Save As* is unlinked, and that a ComfyUI without the
save hook says so.

Run in Node against a stand-in for ComfyUI's ``app``, so this is skipped where
there is no ``node``.
"""

import json
import pathlib
import shutil
import subprocess
import tempfile
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]

AUTO = "auto:" + "a" * 64
OTHER = "manual:" + "b" * 32

DRIVER = r"""
import { app } from "../../scripts/app.js";
import { differsFrom, fileStem, placePeopleLoader } from "./open_workflow.js";
import { saveToPixlStash } from "./save_workflow.js";
import { FILE_TAG, TAG, isUnsent, openings, quietly } from "./workflow_link.js";

const [AUTO, OTHER] = JSON.parse(process.argv[2]);
const [opener, saver] = app.extensions;
const results = {};
const turns = () => new Promise((resolve) => setTimeout(resolve, 20));
const LOADER = "PixlStashMultiAdapterLoader";

// --- a stand-in canvas ------------------------------------------------------

function makeNode(id, type, widgets = {}, inputs = [], mode = 0) {
    return {
        id, type, mode, pos: [10, 20], size: [200, 100],
        widgets: Object.entries(widgets).map(([name, value]) => ({ name, value })),
        inputs: inputs.map((name) => ({ name })),
        findInputSlot(name) { return this.inputs.findIndex((i) => i.name === name); },
        connect(slot, target, to) {
            app.wires.push([this.id, slot, target.id, typeof to === "number" ? target.inputs[to].name : to]);
        },
    };
}

function makeGraph(nodes) {
    return {
        nodes, arranged: 0,
        getNodeById(id) { return nodes.find((n) => n.id === id) ?? null; },
        add(node) { node.id = 99; nodes.push(node); },
        arrange() { this.arranged++; },
        serialize() { return { serialized: nodes.map((n) => n.id), extra: { ...this.extra } }; },
    };
}

const chain = () => [
    makeNode(1, "CheckpointLoaderSimple", { ckpt_name: "a.safetensors" }),
    makeNode(3, "KSampler", { seed: 1, steps: 20 }, ["model", "positive"]),
    makeNode(6, "CLIPTextEncode", { text: "old words" }, ["clip"]),
];
const API = {
    1: { class_type: "CheckpointLoaderSimple", inputs: { ckpt_name: "a.safetensors" } },
    3: { class_type: "KSampler", inputs: { seed: 7, steps: 25, model: ["1", 0], positive: ["6", 0] } },
    6: { class_type: "CLIPTextEncode", inputs: { text: "new words", clip: ["1", 1] } },
};
const PLACE = {
    model: { node_id: "1", output: 0 },
    clip: { node_id: "1", output: 1 },
    rewires: [
        { node_id: "3", field: "model", type: "MODEL" },
        { node_id: "6", field: "clip", type: "CLIP" },
    ],
};

/**
 * What the canvas converts to, as far as these graphs go: API's wiring with
 * the canvas's own widget values, running nodes only, and the loader for
 * people (node 9) wired in after the checkpoint when the canvas has one.
 */
function convert(graph) {
    const output = {};
    for (const node of graph.nodes) {
        if (node.mode) continue;   // bypassed or muted: in no graph
        const wired = Object.entries(API[node.id]?.inputs ?? {}).filter(([, value]) => Array.isArray(value));
        const inputs = Object.fromEntries(wired);
        for (const widget of node.widgets) inputs[widget.name] = widget.value;
        output[node.id] = { class_type: node.type, inputs };
    }
    if (output[9]?.class_type === LOADER) {
        Object.assign(output[9].inputs, { model: ["1", 0], clip: ["1", 1] });
        output[3].inputs.model = ["9", 0];
        output[6].inputs.clip = ["9", 1];
    }
    return output;
}

/** ComfyUI, as far as the opener and the save hook reach into it. */
function comfy({ files = {}, open = [], hook = true, unsent = [] } = {}) {
    const toasts = [];
    const kept = new Map(unsent.map((path) => [`PixlStash.unsent:${path}`, "1"]));
    globalThis.localStorage = {
        getItem: (key) => kept.get(key) ?? null,
        setItem: (key, value) => kept.set(key, String(value)),
        removeItem: (key) => kept.delete(key),
    };
    const store = {
        activeWorkflow: null,
        openWorkflows: open,
        saved: [],
        closed: [],
        actions: [],
        getWorkflowByPath: (path) => files[path] ?? null,
        async closeWorkflow(wf) { this.closed.push(wf.path); },
        async saveWorkflow(wf) {
            // What reaches the file is the canvas as it stands.
            this.saved.push([wf.path, { ...app.rootGraph.extra }]);
            // As Pinia does: the listeners' `after` runs with the action's end.
            for (const listen of this.actions) {
                const afters = [];
                listen({ name: "saveWorkflow", args: [wf], after: (fn) => afters.push(fn) });
                afters.forEach((fn) => fn());
            }
        },
    };
    if (hook) store.$onAction = (listen) => store.actions.push(listen);
    app.wires = [];
    app.loads = [];
    app.ui = { settings: { getSettingValue: () => "example-token" } };
    app.extensionManager = { toast: { add: (t) => toasts.push([t.severity, t.detail]) }, workflow: store };
    const load = (how) => async (document, ...rest) => {
        // The last argument is a name, or the workflow file to load into.
        const into = how === "api" ? rest[0].replace(/\.json$/, "") : rest[2];
        const file = typeof into === "object" ? into : null;
        app.loads.push([how, file ? `into ${file.path}` : into]);
        const before = app.rootGraph;
        app.rootGraph = makeGraph(how === "editor" && document.canvas ? document.canvas() : chain());
        // A canvas serialised and loaded again is the same canvas.
        if (file) Object.assign(app.rootGraph, { nodes: before.nodes, extra: document.extra });
        store.activeWorkflow = file ?? { path: `workflows/${into}.json`, filename: into.split("/").pop(), isTemporary: true };
        return true;
    };
    app.loadApiJson = load("api");
    app.loadGraphData = load("editor");
    app.graphToPrompt = async () => ({ workflow: { nodes: [{ id: 1, type: "x" }] }, output: convert(app.rootGraph) });
    globalThis.LiteGraph = {
        createNode: (type) => makeNode(null, type, { show: "All adapters", adapter_sha256: "" }, ["model", "clip"]),
    };
    return { toasts, store, kept };
}

/** Open `key` from PixlStash, which answers `graph`; returns what was sent. */
async function openLink(key, graph) {
    const sent = [];
    globalThis.window = {
        location: { href: `http://comfy.test/?pixlstash_workflow=${key}` },
        history: { replaceState() {} },
    };
    globalThis.fetch = async (url, init) => {
        sent.push([String(url), init.method ?? "GET"]);
        return { ok: true, status: 200, json: async () => graph };
    };
    await opener.setup();
    await turns();
    return sent;
}

const loaders = (graph) => graph.nodes.filter((n) => n.type === LOADER);
const valueOf = (node, name) => node.widgets.find((w) => w.name === name).value;

// --- opening ----------------------------------------------------------------

{
    // Never saved from ComfyUI: the runnable graph, arranged, with the loader.
    const { toasts, store } = comfy();
    await saver.setup();
    const sent = await openLink(AUTO, { name: "Portrait: v2", workflow: API, version: 3, waiting_applied: true, people_loader: PLACE });
    const graph = app.rootGraph;
    const opening = openings.get("workflows/pixlstash/Portrait v2.json");
    results.opened = {
        sent, toasts, loads: app.loads, wires: app.wires,
        show: valueOf(loaders(graph)[0], "show"),
        arranged: graph.arranged, saved: store.saved,
        opening: { ...opening, opening: /^[0-9a-f]{32}$/.test(opening?.opening ?? "") },
    };
}
{
    // Saved from ComfyUI before: the layout, the values of now, its own loader kept.
    const { toasts, store } = comfy();
    const kept = makeNode(9, LOADER, { show: "People who fit", adapter_sha256: "" });
    const editor = { canvas: () => [...chain(), kept] };
    await openLink(AUTO, { name: "Laid out", workflow: API, editor, people_loader: PLACE });
    const graph = app.rootGraph;
    results.laid_out = {
        toasts, loads: app.loads, wires: app.wires, arranged: graph.arranged,
        saved: store.saved.map(([path]) => path), loaders: loaders(graph).length,
        steps: valueOf(graph.getNodeById(3), "steps"), text: valueOf(graph.getNodeById(6), "text"),
    };
}
// A layout that is not of the graph PixlStash sent is not used, whatever it is.
const refused = {
    // Another graph altogether: a node the workflow does not have.
    another_graph: () => [...chain(), makeNode(40, "LoraLoader", { lora_name: "x.safetensors" })],
    // A loader for people that still holds somebody: PixlStash stores none.
    somebody_picked: () => [...chain(), makeNode(9, LOADER, { show: "People who fit", adapter_sha256: "f".repeat(64) })],
    // A layout ComfyUI throws on: it costs the layout, not the open.
    throws: () => { throw new Error("unsupported document"); },
    // A value this could not write over the layout: the widget is not there.
    value_not_written: () => chain().map((n) => (n.id === 3 ? makeNode(3, "KSampler", { seed: 1 }, ["model", "positive"]) : n)),
};
for (const [name, canvas] of Object.entries(refused)) {
    const { toasts } = comfy();
    await openLink(AUTO, { name: "Not it", workflow: API, editor: { canvas } });
    results[`layout_${name}`] = { toasts, loads: app.loads, nodes: app.rootGraph.nodes.map((n) => n.id) };
}
{
    // The owner's own bypassed loader is in no graph either, and is theirs.
    const { toasts } = comfy();
    const theirs = makeNode(9, LOADER, { show: "People who fit", adapter_sha256: "e".repeat(64) }, [], 4);
    await openLink(AUTO, { name: "Bypassed", workflow: API, editor: { canvas: () => [...chain(), theirs] }, people_loader: PLACE });
    results.bypassed_loader = {
        toasts, loads: app.loads, kept: valueOf(theirs, "adapter_sha256"),
        loaders: loaders(app.rootGraph).length, wires: app.wires.length,
    };
}
{
    // PixlStash cannot place the loader: said, and the canvas left as it is.
    const { toasts } = comfy();
    await openLink(AUTO, { name: "Two models", workflow: API, people_loader: { reason: "It loads two models." } });
    results.unplaceable = { toasts, wires: app.wires, nodes: app.rootGraph.nodes.length };
}
{
    // A graph out of a subgraph: PixlStash names its nodes "75:70", and
    // ComfyUI's API loader puts them on the canvas as "75_70".
    const { toasts } = comfy();
    const graph = makeGraph([
        makeNode("75_70", "UNETLoader"),
        makeNode("75_63", "CFGGuider", {}, ["model"]),
    ]);
    const placed = placePeopleLoader(graph, {
        model: { node_id: "75:70", output: 0 },
        clip: null,
        rewires: [{ node_id: "75:63", field: "model", type: "MODEL" }],
    });
    results.renamed = { toasts, placed: !!placed, wires: app.wires };
}
{
    // An extension that wraps the API loader and drops its promise (rgthree
    // does): the await is over with the canvas cleared and no node on it yet.
    const { toasts, store } = comfy();
    const load = app.loadApiJson;
    app.loadApiJson = async (...args) => {
        app.rootGraph = makeGraph([]);
        setTimeout(() => load(...args), 60);
    };
    await openLink(AUTO, { name: "Late", workflow: API, people_loader: PLACE });
    await new Promise((resolve) => setTimeout(resolve, 250));
    results.dropped_promise = { toasts, wires: app.wires.length, saved: store.saved.map(([path]) => path) };
}
{
    // Nobody has a LoRA: no loader, and nothing said about it.
    const { toasts } = comfy();
    await openLink(AUTO, { name: "Nobody", workflow: API, people_loader: null });
    results.nobody = { toasts, nodes: app.rootGraph.nodes.length };
}
{
    // A graph rebuilt from a stored recipe: no seed, and models PixlStash
    // forgot. Said together, before the first queue fails unexplained.
    const { toasts } = comfy();
    await openLink(AUTO, { name: "Recipe", workflow: API, seedless: true, forgotten: 3 });
    results.rebuilt = { toasts };
}
const tagged = (path, key) => ({ path, content: JSON.stringify({ extra: { [TAG]: key } }) });
{
    // The name is another workflow's file: left alone. This workflow's own,
    // open in a tab from an earlier time, is loaded into and saved over.
    const theirs = tagged("workflows/pixlstash/Same.json", OTHER);
    const mine = tagged("workflows/pixlstash/Same (2).json", AUTO);
    const { store } = comfy({ files: { [theirs.path]: theirs, [mine.path]: mine }, open: [mine] });
    await saver.setup();
    const sent = await openLink(AUTO, { name: "Same", workflow: API, version: 2 });
    results.same_name = {
        loads: app.loads, saved: store.saved, closed: store.closed, sent: sent.length,
        version: openings.get(mine.path)?.version,
    };
}
{
    // A file the store has not listed yet (made since it last read the
    // folder): the folder is read again before it is taken for no file.
    const mine = tagged("workflows/pixlstash/Late.json", AUTO);
    const files = {};
    const { store } = comfy({ files });
    store.syncWorkflows = async () => { files[mine.path] = mine; };
    await openLink(AUTO, { name: "Late", workflow: API });
    results.unlisted_file = { loads: app.loads, saved: store.saved.map(([path]) => path) };
}
{
    // Its file holds a save PixlStash never received: the only copy of it.
    const mine = tagged("workflows/pixlstash/Unsent.json", AUTO);
    const { toasts, store } = comfy({ files: { [mine.path]: mine }, unsent: [mine.path] });
    await openLink(AUTO, { name: "Unsent", workflow: API });
    results.unsent_file = { toasts, loads: app.loads, saved: store.saved };
}
{
    // Open here already, with changes nobody saved: not opened over.
    const tab = { path: "workflows/pixlstash/Busy.json", isModified: true, activeState: { extra: { [TAG]: AUTO } } };
    const { toasts } = comfy({ open: [tab] });
    await openLink(AUTO, { name: "Busy", workflow: API });
    results.unsaved_tab = { toasts, loads: app.loads };
}
{
    // A store that cannot be asked for files: the tab stays unsaved, and says so.
    const { toasts, store } = comfy();
    delete store.getWorkflowByPath;
    await openLink(AUTO, { name: "Old", workflow: API });
    results.no_file = { toasts, loads: app.loads, saved: store.saved, file: app.rootGraph.extra[FILE_TAG] ?? null };
}
{
    // ComfyUI will not load the canvas into the file's tab: nothing is
    // written, and the built tab is left untagged rather than pointing at a
    // file that was never written.
    const mine = tagged("workflows/pixlstash/Bad.json", AUTO);
    const { toasts, store } = comfy({ files: { [mine.path]: mine }, open: [mine] });
    app.loadGraphData = async () => false;
    await openLink(AUTO, { name: "Bad", workflow: API });
    results.load_refused = {
        toasts, saved: store.saved.map(([path]) => path),
        file: app.rootGraph.extra[FILE_TAG] ?? null,
    };
}
{
    // No store to save a file with: the tab is still named from the workflow
    // name, sanitised as a file name would be, not the raw server string.
    const { store } = comfy();
    delete store.getWorkflowByPath;
    await openLink(AUTO, { name: "a/b: c?", workflow: API });
    results.no_file_name = { loads: app.loads };
}
// Windows refuses these as file names whatever the extension, case aside.
results.stems = ["a/b: c?", " . ", "Fine name", "CON", "nul", "LPT1", "console"].map(fileStem);
results.differs = {
    same: differsFrom(convert(makeGraph(chain().map((n) => { n.widgets.forEach((w) => { if (w.name in API[n.id].inputs) w.value = API[n.id].inputs[w.name]; }); return n; }))), API),
    rewired: differsFrom({ ...API, 6: { class_type: "CLIPTextEncode", inputs: { text: "new words", clip: ["3", 1] } } }, API),
    extra_link: differsFrom({ ...API, 6: { class_type: "CLIPTextEncode", inputs: { text: ["1", 2], clip: ["1", 1] } } }, API),
    another_class: differsFrom({ ...API, 1: { class_type: "UNETLoader", inputs: { ckpt_name: "a.safetensors" } } }, API),
    float_noise: differsFrom({ 1: { class_type: "x", inputs: { cfg: 0.1 + 0.2 } } }, { 1: { class_type: "x", inputs: { cfg: 0.3 } } }),
    // A seed is a whole number, however large: a neighbour is another seed.
    near_seed: differsFrom({ 1: { class_type: "x", inputs: { seed: 1e15 + 1000 } } }, { 1: { class_type: "x", inputs: { seed: 1e15 } } }),
    same_seed: differsFrom({ 1: { class_type: "x", inputs: { seed: 1e15 } } }, { 1: { class_type: "x", inputs: { seed: 1e15 } } }),
    canvas_only_value: differsFrom({ 1: { class_type: "x", inputs: { cfg: 7, control_after_generate: "fixed" } } }, { 1: { class_type: "x", inputs: { cfg: 7 } } }),
};

// --- saving -----------------------------------------------------------------

const PATH = "workflows/pixlstash/Portrait.json";

/** Saves of a canvas linked to `key`, answered by `reply`. */
async function save({ key = AUTO, file = PATH, path = PATH, reply = { version: 4 }, status = 200, opened, prompt, offline = false, wf, byHand = false, times = 1, during } = {}) {
    const { toasts, store, kept } = comfy();
    const active = { path, filename: "Portrait", isTemporary: false };
    store.activeWorkflow = active;
    app.rootGraph = { extra: { ...(key ? { [TAG]: key } : {}), ...(file ? { [FILE_TAG]: file } : {}) } };
    const canvas = app.rootGraph;
    app.graphToPrompt = prompt ?? (async () => ({ workflow: { nodes: [{ id: 1, type: "x" }] }, output: { 1: {} } }));
    openings.clear();
    if (opened) openings.set(PATH, opened);
    const sent = [];
    globalThis.fetch = async (url, init) => {
        if (offline) throw new Error("fetch failed");
        sent.push({ url, method: init.method, auth: init.headers.Authorization, body: JSON.parse(init.body) });
        during?.(store);
        return { ok: status < 400, status, json: async () => reply };
    };
    for (let n = 0; n < times; n++) await saveToPixlStash(wf === undefined ? active : wf, { byHand });
    return { sent, toasts, saved: store.saved.map(([path]) => path), tag: canvas.extra[TAG], opening: openings.get(PATH), unsent: isUnsent(PATH), stored: [...kept.keys()] };
}

results.saved = await save({ opened: { version: 3, waiting: true, opening: "c".repeat(32) }, times: 2 });
results.new_workflow = await save({ reply: { new_workflow: true, workflow_id: OTHER, name: "Portrait (new checkpoint)", version: 1 } });
results.new_workflow_after_a_switch = await save({
    reply: { new_workflow: true, workflow_id: OTHER, name: "Portrait (new checkpoint)", version: 1 },
    during: (store) => { store.activeWorkflow = { path: "workflows/Else.json", filename: "Else" }; },
});
results.replaced_newer = await save({ reply: { version: 6, replaced_newer: true } });
results.offline = await save({ offline: true, times: 2 });
// A by-hand send that fails made no ComfyUI save, so the file must not be
// left looking like one PixlStash never got.
results.offline_by_hand = await save({ offline: true, byHand: true });
results.will_not_convert = await save({ prompt: async () => { throw new Error("no node Foo"); } });
results.unknown_workflow = await save({ status: 404, reply: { error: "Unknown workflow." } });
results.refused = await save({ status: 413, reply: { error: "Too large" } });
results.people_only = await save({ reply: { version: 4, unchanged: true, left_out: [{ class_type: LOADER }] }, times: 2 });
results.unchanged = await save({ reply: { version: 4, unchanged: true } });
results.unchanged_by_hand = await save({ reply: { version: 4, unchanged: true }, byHand: true });
results.persons_lora = await save({ reply: { version: 5, left_out: [{ class_type: "LoraLoader", lora: "Anna v2", people: ["Anna"] }] }, times: 2 });
results.not_linked = await save({ key: null, file: null });
results.not_linked_by_hand = await save({ key: null, file: null, byHand: true });
// The id without being the file PixlStash opened: a duplicated tab, a
// picture dragged back in, a file renamed or handed over.
results.not_the_file = await save({ file: "workflows/pixlstash/Another.json", times: 2 });
results.no_file_tag = await save({ file: null, path: "workflows/Dragged in.json" });
results.not_the_file_by_hand = await save({ file: null, byHand: true });
results.another_tab = await save({ wf: { path: "workflows/pixlstash/Other.json", filename: "Other", content: JSON.stringify({ extra: { [TAG]: OTHER } }) } });
// The same of the file PixlStash opened (a background tab closed with Save):
// ComfyUI wrote it and PixlStash never got it, so it must not be opened over.
const BEHIND = "workflows/pixlstash/Behind.json";
results.opened_file_behind_another_tab = await save({ wf: { path: BEHIND, filename: "Behind", content: JSON.stringify({ extra: { [TAG]: OTHER, [FILE_TAG]: BEHIND } }) } });
// The reader switched tabs while the canvas was being converted.
results.switched_while_converting = await save({
    prompt: async () => {
        app.extensionManager.workflow.activeWorkflow = { path: "workflows/Else.json", filename: "Else" };
        return { workflow: {}, output: {} };
    },
});

// --- the hook ---------------------------------------------------------------

{
    const { toasts, store } = comfy();
    await saver.setup();
    const active = { path: PATH, filename: "Portrait" };
    store.activeWorkflow = active;
    app.rootGraph = { extra: { [TAG]: AUTO, [FILE_TAG]: PATH } };
    let sends = 0;
    app.graphToPrompt = async () => ({ workflow: { nodes: [] }, output: { 1: {} } });
    globalThis.fetch = async () => { sends++; return { ok: true, status: 200, json: async () => ({ version: 2 }) }; };
    await store.saveWorkflow(active);
    await turns();
    const afterSave = sends;
    await quietly(active, () => store.saveWorkflow(active));
    await turns();
    const afterQuiet = sends;
    // A save of this file that ends while the pack is saving ANOTHER is sent.
    await quietly({ path: "workflows/pixlstash/Else.json" }, () => store.saveWorkflow(active));
    await turns();

    // Save As: the copy ComfyUI makes carries the link until it is taken off.
    const copy = { content: JSON.stringify({ extra: { [TAG]: AUTO, [FILE_TAG]: PATH, ds: 1 } }) };
    copy.originalContent = copy.content;
    for (const listen of store.actions) {
        const afters = [];
        listen({ name: "saveAs", args: [active, "workflows/Copy.json"], after: (fn) => afters.push(fn) });
        afters.forEach((fn) => fn(copy));
    }
    results.hook = { afterSave, afterQuiet, afterOthersQuiet: sends, copy: JSON.parse(copy.content), same: copy.content === copy.originalContent, toasts };
}
{
    const { toasts } = comfy({ hook: false });
    await saver.setup();
    await saver.setup();
    results.no_hook = { toasts, commands: saver.commands.map((c) => c.label), menu: saver.menuCommands };
}
console.log(JSON.stringify(results));
"""


@unittest.skipUnless(shutil.which("node"), "needs node")
class RoundTripTests(unittest.TestCase):
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
            for name in (
                "open_workflow.js",
                "save_workflow.js",
                "workflow_link.js",
            ):
                shutil.copy(ROOT / "web" / "js" / name, js)
            (js / "driver.js").write_text(DRIVER)
            done = subprocess.run(
                ["node", str(js / "driver.js"), json.dumps([AUTO, OTHER])],
                capture_output=True,
                text=True,
                timeout=60,
            )
        if done.returncode:
            raise AssertionError(done.stderr)
        cls.results = json.loads(done.stdout.strip().splitlines()[-1])

    def _one_toast(self, scenario, severity, says):
        [(got, detail)] = self.results[scenario]["toasts"]
        self.assertEqual(got, severity)
        self.assertIn(says, detail)
        return detail

    # --- opening -------------------------------------------------------------

    def test_a_workflow_opens_as_a_saved_file_linked_to_it(self):
        opened = self.results["opened"]
        [(url, method)] = opened["sent"]
        self.assertEqual(method, "GET")
        self.assertTrue(url.startswith("/pixlstash/workflow_graph?"))
        # The waiting changes are in the graph, and every stage.
        self.assertIn("waiting=true", url)
        self.assertIn("all_stages=true", url)
        # Under the file's own name, in `pixlstash/`: no name is asked for.
        path = "workflows/pixlstash/Portrait v2.json"
        self.assertEqual(opened["loads"], [["api", "pixlstash/Portrait v2"]])
        # What reached the file: linked to the workflow, an automatic one as
        # much as a manual one, and to this file and no other.
        self.assertEqual(
            opened["saved"],
            [[path, {"pixlstash_workflow_id": AUTO, "pixlstash_file": path}]],
        )
        self.assertEqual(
            opened["opening"], {"version": 3, "waiting": True, "opening": True}
        )
        self.assertEqual(opened["toasts"], [])

    def test_making_the_file_is_not_a_save_back_to_pixlstash(self):
        """The save hook is listening, and only the graph was fetched."""
        self.assertEqual(len(self.results["opened"]["sent"]), 1)

    def test_the_loader_for_people_goes_after_the_model_source(self):
        opened = self.results["opened"]
        self.assertEqual(opened["show"], "People who fit")
        self.assertEqual(
            opened["wires"],
            [
                [1, 0, 99, "model"],
                [1, 1, 99, "clip"],
                [99, 0, 3, "model"],
                [99, 1, 6, "clip"],
            ],
        )
        # Arranged again, since ComfyUI arranged before the loader was on.
        self.assertEqual(opened["arranged"], 1)

    def test_a_layout_opens_with_the_values_of_now(self):
        laid = self.results["laid_out"]
        self.assertEqual(laid["loads"], [["editor", "pixlstash/Laid out"]])
        self.assertEqual((laid["steps"], laid["text"]), (25, "new words"))
        # The layout is left as the reader arranged it.
        self.assertEqual(laid["arranged"], 0)
        self.assertEqual(laid["toasts"], [])
        self.assertEqual(laid["saved"], ["workflows/pixlstash/Laid out.json"])

    def test_a_layouts_own_loader_is_kept_and_no_other_put_on(self):
        laid = self.results["laid_out"]
        self.assertEqual((laid["loaders"], laid["wires"]), (1, []))

    def test_a_layout_that_is_not_the_graph_is_not_used(self):
        """PixlStash stores a layout on the word of whoever saved it. What
        binds it to the graph is this: it is converted on the canvas and used
        only when it comes out as the graph PixlStash sent."""
        for name in ("another_graph", "somebody_picked", "throws", "value_not_written"):
            with self.subTest(name=name):
                result = self.results[f"layout_{name}"]
                self.assertEqual(
                    result["loads"],
                    [["editor", "pixlstash/Not it"], ["api", "pixlstash/Not it"]],
                )
                # The graph itself is what is on the canvas.
                self.assertEqual(result["nodes"], [1, 3, 6])
                self.assertEqual(result["toasts"], [])

    def test_what_makes_a_canvas_another_graph(self):
        differs = self.results["differs"]
        self.assertIsNone(differs["same"])
        self.assertIsNone(differs["float_noise"])
        self.assertIsNone(differs["canvas_only_value"])
        self.assertIsNone(differs["same_seed"])
        for name in ("rewired", "extra_link", "another_class", "near_seed"):
            with self.subTest(name=name):
                self.assertIsInstance(differs[name], str)

    def test_the_owners_bypassed_loader_is_left_as_it_is(self):
        result = self.results["bypassed_loader"]
        self.assertEqual(result["loads"], [["editor", "pixlstash/Bypassed"]])
        self.assertEqual(result["kept"], "e" * 64)
        # It is not the loader for people: that one is put on beside it.
        self.assertEqual((result["loaders"], result["wires"]), (2, 4))

    def test_a_graph_the_loader_cannot_go_on_says_why(self):
        result = self.results["unplaceable"]
        self._one_toast("unplaceable", "warn", "It loads two models.")
        self.assertEqual((result["wires"], result["nodes"]), ([], 3))

    def test_the_loader_goes_on_a_graph_out_of_a_subgraph(self):
        self.assertEqual(
            self.results["renamed"],
            {
                "toasts": [],
                "placed": True,
                "wires": [["75_70", 0, 99, "model"], [99, 0, "75_63", "model"]],
            },
        )

    def test_a_load_whose_promise_was_dropped_is_waited_for(self):
        """Not placed on, and not saved from, a canvas the graph is not on yet."""
        self.assertEqual(
            self.results["dropped_promise"],
            {"toasts": [], "wires": 4, "saved": ["workflows/pixlstash/Late.json"]},
        )

    def test_no_loader_where_nobody_has_a_lora(self):
        self.assertEqual(self.results["nobody"], {"toasts": [], "nodes": 3})

    def test_a_graph_rebuilt_from_a_recipe_is_warned_about(self):
        """No seed, and models PixlStash forgot: said before the first queue,
        or it fails with no explanation."""
        self._one_toast(
            "rebuilt",
            "warn",
            "Rebuilt from a saved recipe: set a seed and pick 3 model(s) "
            "PixlStash no longer knows before you queue it.",
        )

    def test_another_workflows_file_of_the_same_name_is_left_alone(self):
        same = self.results["same_name"]
        self.assertNotIn(
            "workflows/pixlstash/Same.json", [path for path, _ in same["saved"]]
        )
        self.assertEqual(same["loads"][0], ["api", "pixlstash/Same (2) (opening)"])

    def test_the_workflows_own_file_is_opened_over_in_its_own_tab(self):
        """Built beside it, loaded into the file's tab, and that tab saved:
        the one way ComfyUI replaces a file whatever state its tab is in."""
        same = self.results["same_name"]
        mine = "workflows/pixlstash/Same (2).json"
        self.assertEqual(same["loads"][1], ["editor", f"into {mine}"])
        self.assertEqual(
            same["closed"], ["workflows/pixlstash/Same (2) (opening).json"]
        )
        self.assertEqual(
            same["saved"],
            [[mine, {"pixlstash_workflow_id": AUTO, "pixlstash_file": mine}]],
        )
        self.assertEqual(same["version"], 2)
        # Only the graph was fetched: this save is not one to send back.
        self.assertEqual(same["sent"], 1)

    def test_the_folder_is_read_again_before_a_file_is_taken_for_missing(self):
        """Found as this workflow's own file, so it is loaded into, not
        saved beside under a name ComfyUI would refuse."""
        late = self.results["unlisted_file"]
        mine = "workflows/pixlstash/Late.json"
        self.assertEqual(late["loads"][0], ["api", "pixlstash/Late (opening)"])
        self.assertEqual(late["loads"][1], ["editor", f"into {mine}"])
        self.assertEqual(late["saved"], [mine])

    def test_a_save_pixlstash_never_received_is_not_opened_over(self):
        self._one_toast("unsent_file", "warn", "PixlStash never received")
        result = self.results["unsent_file"]
        self.assertEqual((result["loads"], result["saved"]), ([], []))

    def test_unsaved_changes_are_not_opened_over(self):
        self._one_toast("unsaved_tab", "warn", "changes that are not saved")
        self.assertEqual(self.results["unsaved_tab"]["loads"], [])

    def test_a_store_that_cannot_save_the_file_leaves_an_unsaved_tab(self):
        result = self.results["no_file"]
        self._one_toast("no_file", "info", "Save to PixlStash")
        self.assertEqual((result["loads"], result["saved"]), ([["api", "Old"]], []))
        # No file, so no file to send saves from.
        self.assertIsNone(result["file"])

    def test_a_tab_that_could_not_be_made_the_file_is_left_untagged(self):
        """The file's path is written only once the tab is that file, so a
        failed load does not leave it pointing at a file never written."""
        result = self.results["load_refused"]
        self._one_toast("load_refused", "info", "Save to PixlStash")
        self.assertEqual(result["saved"], [])
        self.assertIsNone(result["file"])

    def test_the_tab_name_is_sanitised_with_no_store_to_save_a_file(self):
        self.assertEqual(self.results["no_file_name"]["loads"], [["api", "a b c"]])

    def test_a_name_becomes_a_file_name(self):
        self.assertEqual(
            self.results["stems"],
            [
                "a b c", "PixlStash workflow", "Fine name",
                "CON workflow", "nul workflow", "LPT1 workflow", "console",
            ],
        )  # fmt: skip

    # --- saving --------------------------------------------------------------

    def test_a_save_sends_both_documents_to_the_workflow_it_was_opened_from(self):
        first, second = self.results["saved"]["sent"]
        self.assertEqual(first["url"], "/pixlstash/workflows/save")
        self.assertEqual(first["method"], "POST")
        self.assertEqual(first["auth"], "Bearer example-token")
        self.assertEqual(
            first["body"],
            {
                "workflow_key": AUTO,
                "workflow": {"nodes": [{"id": 1, "type": "x"}]},
                "output": {"1": {}},
                "read_version": 3,
                "waiting_applied": True,
                "opening": "c" * 32,
            },
        )
        # The next save was read at the version this one made, what waited is
        # no longer waiting, and it is of the same opening.
        self.assertEqual(
            (
                second["body"]["read_version"],
                second["body"]["waiting_applied"],
                second["body"]["opening"],
            ),
            (4, False, "c" * 32),
        )
        self.assertEqual(
            self.results["saved"]["toasts"][0],
            ["success", "Saved to PixlStash: Portrait, version 4."],
        )
        self.assertFalse(self.results["saved"]["unsent"])

    def test_a_changed_checkpoint_links_the_canvas_to_the_new_workflow(self):
        result = self.results["new_workflow"]
        self._one_toast(
            "new_workflow",
            "success",
            "Saved to PixlStash as a new workflow, Portrait (new checkpoint): "
            "the checkpoint changed.",
        )
        self.assertEqual(result["tag"], OTHER)
        # The file is saved again with the new link, and that is not sent.
        self.assertEqual(result["saved"], ["workflows/pixlstash/Portrait.json"])
        self.assertEqual(len(result["sent"]), 1)
        # PixlStash names the new workflow: no name is sent for it.
        self.assertNotIn("name", result["sent"][0]["body"])

    def test_another_canvas_is_never_linked_to_the_new_workflow(self):
        """The reader switched tabs while the save was on its way."""
        result = self.results["new_workflow_after_a_switch"]
        self._one_toast("new_workflow_after_a_switch", "warn", "still linked")
        self.assertEqual((result["tag"], result["saved"]), (AUTO, []))

    def test_a_replaced_newer_version_is_said(self):
        detail = self._one_toast("replaced_newer", "warn", "version 6")
        self.assertIn("newer version in PixlStash was replaced", detail)

    def test_an_unreachable_pixlstash_is_said_once_and_remembered(self):
        """Two saves, one toast: an autosave must not repeat it. And the file
        is noted as holding a save PixlStash never received."""
        detail = self._one_toast("offline", "error", "cannot be reached")
        self.assertIn("the next save tries again", detail)
        self.assertTrue(self.results["offline"]["unsent"])

    def test_a_by_hand_failure_is_not_a_comfyui_save_left_unsent(self):
        """The menu command saves nothing to ComfyUI, so a failure must not
        mark the file, or opening it later is refused for nothing."""
        detail = self._one_toast("offline_by_hand", "error", "cannot be reached")
        self.assertIn("try again", detail)
        self.assertNotIn("It is saved in ComfyUI", detail)
        self.assertFalse(self.results["offline_by_hand"]["unsent"])

    def test_a_node_that_will_not_convert_is_said(self):
        self._one_toast("will_not_convert", "error", "no node Foo")
        self.assertEqual(self.results["will_not_convert"]["sent"], [])

    def test_a_workflow_pixlstash_does_not_have_offers_export(self):
        self._one_toast("unknown_workflow", "warn", "Export to PixlStash")

    def test_what_pixlstash_refused_reaches_the_toast(self):
        self._one_toast("refused", "error", "Too large")

    def test_picking_people_is_nothing_new_to_save(self):
        """Said once for two saves: autosave must not repeat it."""
        self.assertEqual(
            self.results["people_only"]["toasts"],
            [["info", "Nothing new to save. The people picked here stay in ComfyUI."]],
        )

    def test_a_save_of_nothing_new_is_quiet_unless_asked_for(self):
        self.assertEqual(self.results["unchanged"]["toasts"], [])
        self._one_toast("unchanged_by_hand", "info", "Nothing new to save")

    def test_a_persons_lora_in_an_ordinary_loader_is_named_once(self):
        """Two saves: each is said, the sentence about the LoRA once."""
        toasts = self.results["persons_lora"]["toasts"]
        self.assertEqual(
            [detail for severity, detail in toasts if severity == "warn"],
            [
                "Anna v2 is Anna's and is not saved with the workflow. "
                "Pick people in the PixlStash loader beside the model."
            ],
        )
        self.assertEqual(len(toasts), 3)

    def test_a_canvas_that_is_not_linked_sends_nothing(self):
        self.assertEqual(
            (self.results["not_linked"]["sent"], self.results["not_linked"]["toasts"]),
            ([], []),
        )
        self._one_toast("not_linked_by_hand", "warn", "Export to PixlStash")
        self.assertEqual(self.results["not_linked_by_hand"]["sent"], [])

    def test_only_the_file_pixlstash_opened_saves_back(self):
        """The id alone is in every picture the canvas made and every
        duplicate of its tab. Saving one of those sends nothing, and says so
        once; the menu command is the reader saying they mean it."""
        for scenario in ("not_the_file", "no_file_tag"):
            with self.subTest(scenario=scenario):
                self.assertEqual(self.results[scenario]["sent"], [])
                self._one_toast(scenario, "info", "not the file PixlStash opened")
        by_hand = self.results["not_the_file_by_hand"]
        self.assertEqual(len(by_hand["sent"]), 1)
        self.assertEqual(by_hand["sent"][0]["body"]["workflow_key"], AUTO)

    def test_a_file_saved_behind_another_tab_is_said_not_dropped(self):
        self._one_toast("another_tab", "warn", "saved while another workflow")
        self.assertEqual(self.results["another_tab"]["sent"], [])
        # It is not the file PixlStash opened, so no save of it is owed.
        self.assertEqual(self.results["another_tab"]["stored"], [])

    def test_a_save_that_was_not_sent_is_remembered_however_it_was_missed(self):
        """ComfyUI wrote the opened file and PixlStash never got it: behind
        another tab, or the tab switched mid-send. Opening the workflow from
        PixlStash again must not write over that file, as when a send fails."""
        behind = self.results["opened_file_behind_another_tab"]
        self._one_toast(
            "opened_file_behind_another_tab", "warn", "saved while another workflow"
        )
        self.assertEqual(
            behind["stored"], ["PixlStash.unsent:workflows/pixlstash/Behind.json"]
        )
        switched = self.results["switched_while_converting"]
        self._one_toast("switched_while_converting", "warn", "You switched workflows")
        self.assertEqual(switched["sent"], [])
        self.assertTrue(switched["unsent"])

    # --- the hook ------------------------------------------------------------

    def test_comfyuis_save_is_what_sends(self):
        hook = self.results["hook"]
        self.assertEqual((hook["afterSave"], hook["afterQuiet"]), (1, 1))
        # Quiet is per file: the pack saving another does not drop this one.
        self.assertEqual(hook["afterOthersQuiet"], 2)

    def test_a_copy_made_with_save_as_is_unlinked(self):
        hook = self.results["hook"]
        self.assertEqual(hook["copy"], {"extra": {"ds": 1}})
        self.assertTrue(hook["same"])
        self.assertIn(
            ["info", "Saved as a copy that is not linked to PixlStash."],
            [
                [severity, detail.split(" Use ")[0]]
                for severity, detail in hook["toasts"]
            ],
        )

    def test_a_comfyui_without_the_hook_says_so_once_and_offers_the_command(self):
        """Loaded twice, said once: not on every page load."""
        result = self.results["no_hook"]
        self._one_toast("no_hook", "warn", "Save to PixlStash")
        self.assertEqual(result["commands"], ["Save to PixlStash"])
        self.assertEqual(
            result["menu"],
            [{"path": ["PixlStash"], "commands": ["PixlStash.SaveWorkflow"]}],
        )


if __name__ == "__main__":
    unittest.main()

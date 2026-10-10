/**
 * PixlStash adapter-picker modal.
 *
 * Exported API
 * ────────────
 * openAdapterPicker(valueWidget, credentials, filters, onPicked)
 *
 *   valueWidget — the hidden widget written on confirm (`adapter_sha256`,
 *                 `vae_sha256`, `clip_sha256`, `checkpoint_id`)
 *   credentials — { url, token, verifySsl }
 *   filters     — { fileKind, kind, baseModel, characterId, setId, workflowSetId,
 *                   several? }
 *   onPicked    — called with the chosen shelf record after confirm
 *
 * With `filters.several` the same grid ticks several adapters, for the
 * Multi Adapter Loader: `{ picked, limit, people, onView }`, where `picked` is
 * the digests already on the node, in row order. Nothing is written to a
 * widget then (`valueWidget` is null); `onPicked` gets the ticked digests, in
 * the order they will fill the rows, and a Map from any of them that a people
 * card's choice swapped in to the digest it replaced. `people` opens it on the people view —
 * one card per person who has an adapter that passes the filters — and
 * `onView(people)` is told on confirm if the switch in the modal was left on
 * the other view. Cancelling changes nothing on the node, the view included.
 *
 * One modal for every kind of file on the shelf, because they differ in three
 * strings and one identity field (see SHELF_KINDS) and in nothing else — the
 * grid, the stack fold, the icon chain and the search box are the same job
 * whichever it is drawing.
 *
 * A sibling of picker.js rather than an extension of it: that one is
 * picture-shaped throughout (fields=grid, picture_id, likeness sorting).
 * What is shared is the modal chrome, the object-URL bookkeeping, and the
 * rule that every string off the server goes in via textContent.
 *
 * `GET /adapters` is unpaginated, so this fetches the filtered shelf once,
 * folds each stack down to its cover (`collapseStacks`) and renders the rest in
 * slices on scroll.  Only the icons of rendered cards are fetched, which is the
 * part that costs a request each.
 */

import { swapTick, toggleTick } from "./adapter_rows.js";
import { el, mkBtn, mkRow, selStyle } from "./modal_dom.js";

const PAGE_SIZE = 48;

/**
 * What differs between the kinds of file the shelf holds.
 *
 * Checkpoints are the odd one: they have their own route, and they are
 * addressed by `id` rather than by hash because `sha256` is null until the
 * server's background hasher has read the file — a 24 GB checkpoint is
 * listable long before that. They also take none of the adapter filters, since
 * `GET /checkpoints` accepts neither `kind` nor an attachment.
 */
const SHELF_KINDS = {
    adapter:      { path: "/pixlstash/adapters",    listKey: "adapters",    title: "PixlStash Adapters",      noun: "adapter" },
    vae:          { path: "/pixlstash/adapters",    listKey: "adapters",    title: "PixlStash VAEs",          noun: "VAE" },
    text_encoder: { path: "/pixlstash/adapters",    listKey: "adapters",    title: "PixlStash Text Encoders", noun: "text encoder" },
    checkpoint:   { path: "/pixlstash/checkpoints", listKey: "checkpoints", title: "PixlStash Checkpoints",   noun: "checkpoint", byId: true },
};

// Keystrokes coalesce before the grid is torn down and rebuilt: without this,
// typing an eight-letter word re-renders eight times and re-requests an icon
// per card each time, every one of which opens a fresh upstream connection.
const SEARCH_DEBOUNCE_MS = 200;

// ---------------------------------------------------------------------------
// Fetch helpers (through the ComfyUI proxy — the browser can't reach a
// self-signed or private PixlStash directly)
// ---------------------------------------------------------------------------

async function proxyFetch(path, credentials, extraParams = {}) {
    const params = new URLSearchParams({
        url:        credentials.url,
        verify_ssl: credentials.verifySsl ? "true" : "false",
        ...extraParams,
    });
    const resp = await fetch(`${path}?${params}`, {
        headers: { "Authorization": `Bearer ${credentials.token}` },
    });
    if (!resp.ok) {
        const body = await resp.json().catch(() => ({}));
        throw new Error(body.error || `HTTP ${resp.status}`);
    }
    return resp.json();
}

async function fetchBlobUrl(path, credentials, extraParams) {
    const params = new URLSearchParams({
        url:        credentials.url,
        verify_ssl: credentials.verifySsl ? "true" : "false",
        ...extraParams,
    });
    const resp = await fetch(`${path}?${params}`, {
        headers: { "Authorization": `Bearer ${credentials.token}` },
    });
    if (!resp.ok) return null;
    return URL.createObjectURL(await resp.blob());
}

/**
 * The face for one card, in the order PixlStash's own shelf resolves it.
 *
 * 1. The model's OWN icon — somebody chose that picture for this file.
 * 2. The face of whoever it is attached to. Almost no adapter carries an icon
 *    (none of the 51 on the shelf this was tested against), while attachments
 *    are common, so this is the step that actually draws a picture. A LoRA of
 *    a person is far better identified by that person's face than by `SA`.
 * 3. `null`, and the caller leaves the generated initials mark alone. A
 *    character with no reference face 404s exactly like one that does not
 *    exist, and an empty square would read as broken rather than as unset.
 *
 * The FIRST attachment wins, matching the ring upstream: a model attached to
 * four characters has one square to draw in, and picking the first is what the
 * shelf does. Sequential rather than raced, so a model with its own icon costs
 * exactly one request.
 *
 * Exported because the node draws the same face as the card does: the Browse
 * button hands the picked record straight back to `combo_widgets.js`.
 */
export async function fetchFaceUrl(record, credentials) {
    if (record.icon_sha256) {
        const url = await fetchBlobUrl("/pixlstash/model_icon", credentials, {
            icon_sha256: record.icon_sha256,
        });
        if (url) return url;
    }
    const attachment = (record.attachments || [])[0];
    if (attachment && attachment.entity_id != null) {
        return fetchBlobUrl("/pixlstash/entity_thumbnail", credentials, {
            entity_type: attachment.entity_type === "character" ? "character" : "set",
            entity_id:   String(attachment.entity_id),
        });
    }
    return null;
}

/**
 * Build the query for `GET /adapters`.
 *
 * character_id and set_id are mutually exclusive upstream (sending both is a
 * 400), so a wired character wins and the set is ignored — the same rule the
 * node's docstring records.
 */
function buildAdapterQuery({ fileKind, kind, baseModel, characterId, setId } = {}) {
    const q = {};
    if (baseModel) q.base_model = baseModel;
    // `GET /checkpoints` takes base_model, q, sort and direction and nothing
    // else — sending it a file_kind or an attachment is a 422, not a filter.
    if (SHELF_KINDS[fileKind]?.byId) return q;
    q.file_kind = fileKind || "adapter";
    if (kind)      q.kind         = kind;
    if (characterId)  q.character_id = characterId;
    else if (setId)   q.set_id      = setId;
    return q;
}

/**
 * The sha256s in one hand-made workflow set's LoRA slot. `GET /adapters` has
 * no workflow-set filter, so the grid is narrowed client-side by these.
 */
async function fetchWorkflowSetLoras(credentials, workflowSetId) {
    const data = await proxyFetch("/pixlstash/workflow_sets", credentials);
    const set = (data?.hand_made ?? []).find(s => String(s.id) === String(workflowSetId));
    if (!set) throw new Error(`Workflow set #${workflowSetId} does not exist any more.`);
    return new Set((set.members ?? []).filter(m => m.slot === "lora").map(m => m.sha256));
}

/** Does this record have a copy the server last saw on disk? */
function isPresent(record) {
    const locations = record.locations;
    if (!Array.isArray(locations)) return false;
    return locations.some(l => l && l.state === "present");
}

/**
 * One card per stack, drawn by its cover — not one per file.
 *
 * A trained LoRA lands on the shelf as every epoch it saved, and the shelf
 * folds those into a *stack* whose cover (``stack_position`` 0) is the file the
 * owner would actually load.  `GET /adapters` returns the members, not the
 * fold, so a shelf of 12 runs arrives as 80 rows — 80 cards and 80 icon
 * requests for 12 things worth picking.  Same rule as the shelf's own
 * `collapseStacks`: a member with no position sorts LAST, matching the
 * server's `ORDER BY stack_position IS NULL, stack_position`, so an
 * unpositioned row is never drawn as the face of a run.
 *
 * Members are kept on the cover as `_members` for the count badge only. There
 * is no expand-the-strip here: this picker exists to choose files to load, and
 * the cover is the file of a run by definition. The Multi Adapter Loader's rows
 * fold the same way, so a row offers what the grid would.
 */
export function collapseStacks(rows) {
    const covers = new Map();   // stack_id → the member with the lowest position
    const counts = new Map();
    for (const row of rows) {
        if (row.stack_id == null) continue;
        counts.set(row.stack_id, (counts.get(row.stack_id) ?? 0) + 1);
        const cover = covers.get(row.stack_id);
        if (!cover || (row.stack_position ?? Infinity) < (cover.stack_position ?? Infinity)) {
            covers.set(row.stack_id, row);
        }
    }
    return rows
        .filter(row => row.stack_id == null || covers.get(row.stack_id) === row)
        .map(row => row.stack_id == null ? row : { ...row, _members: counts.get(row.stack_id) });
}

/** The character a record is attached to first, as an id, or `null`. */
export function personOf(record) {
    const attachment = (record?.attachments || []).find(
        a => a && a.entity_type === "character" && a.entity_id != null);
    return attachment ? String(attachment.entity_id) : null;
}

/**
 * One entry per person, each with the adapters of theirs in `rows`.
 *
 * The same shape of fold as `collapseStacks`, by first character attachment,
 * so an adapter attached to two people is offered under the first and not
 * twice. `names` maps a character id
 * to a name; a person the list has no name for is still a person.
 */
export function foldPeople(rows, names = new Map()) {
    const people = new Map();
    for (const row of rows) {
        const id = personOf(row);
        if (id == null) continue;
        if (!people.has(id)) {
            people.set(id, { id, name: names.get(id) || `Character #${id}`, adapters: [] });
        }
        people.get(id).adapters.push(row);
    }
    return [...people.values()].sort((a, b) => a.name.localeCompare(b.name));
}

/**
 * A record's trigger words as text for a prompt, or "".
 *
 * The same unwrapping as `_trigger_words` in nodes/adapter_loader.py, so the
 * chip on a row reads as the `trigger_words` output will: the field arrives as
 * a list, as a JSON list in a string, or as plain words.
 */
export function triggerText(record) {
    let value = record?.trigger_words;
    if (typeof value === "string" && value.trim().startsWith("[")) {
        try {
            const decoded = JSON.parse(value);
            if (Array.isArray(decoded)) value = decoded;
        } catch { /* not JSON: plain words that happen to start with a bracket */ }
    }
    if (Array.isArray(value)) {
        return value.filter(v => v != null).map(v => String(v).trim()).filter(Boolean).join(", ");
    }
    return value ? String(value).trim() : "";
}

/** What a card (and the Browse button) calls a record. */
export function nameOf(record) {
    return record?.display_name || record?.filename || null;
}

// value → shelf record, for the button labels and the node's thumbnail. Held
// for the life of the page, which is right for the things read off it here — a
// name, a face and, on the Multi Adapter Loader's rows, a person and a trigger
// word, none of which changes while a graph is open in the ordinary way — and
// would not be for `locations`: the grid re-fetches those rather than reading
// them from here, because a drive can come back mid-session.
const _recordCache = new Map();

/**
 * The shelf record of an already-selected file, or `null`.
 *
 * A saved workflow carries only the hash (or the checkpoint id), so a reloaded
 * node has nothing to put on its button but that, and nothing to draw. This is
 * the lookup that turns it back into a name and a face — one small request for
 * a hash-addressed file, and for a checkpoint the list route, since the server
 * has no by-id one.
 *
 * The value is normalised first, the same way the Python loaders normalise it
 * (`shelf_file.resolve` trims and lowercases). A workflow carrying a padded or
 * upper-case digest loads perfectly well, and without this the *label* lookup
 * would be the one thing that fails — the proxy rejects a non-lowercase digest
 * by design — leaving a working node captioned with a hash.
 *
 * Never throws: a failure here costs a nicer label and a picture and nothing
 * else, so an unreachable server or an expired token leaves the hash on the
 * button rather than raising into a canvas redraw. Failures are not cached, so
 * fixing the token and reloading the graph is enough to get names back.
 */
export async function shelfRecordFor(rawValue, credentials, fileKind) {
    const shelf = SHELF_KINDS[fileKind] ?? SHELF_KINDS.adapter;
    const value = String(rawValue ?? "").trim().toLowerCase();
    // Refused here rather than by the proxy: a value that cannot address a row
    // has no row to find, and asking anyway spends a request to be told so.
    const usable = shelf.byId ? /^[1-9]\d*$/.test(value) : /^[0-9a-f]{64}$/.test(value);
    if (!usable) return null;

    // Per server: a checkpoint id names a different file on another one, and
    // even a digest's display name is that shelf's own.
    const key = `${credentials.url}|${fileKind}:${value}`;
    if (_recordCache.has(key)) return _recordCache.get(key);

    let record = null;
    try {
        if (shelf.byId) {
            const data = await proxyFetch(shelf.path, credentials, {});
            const rows = data?.[shelf.listKey];
            record = (Array.isArray(rows) ? rows : []).find(r => String(r?.id) === value) ?? null;
        } else {
            record = await proxyFetch("/pixlstash/adapter", credentials, { sha256: value });
        }
    } catch {
        return null;
    }
    if (record) _recordCache.set(key, record);
    return record;
}

/** The people view's search: the person, or anything about their adapters. */
function matchesPerson(person, needle) {
    if (!needle) return true;
    return person.name.toLowerCase().includes(needle)
        || person.adapters.some(a => matchesSearch(a, needle));
}

/** Fields the in-modal search box matches against. */
function matchesSearch(record, needle) {
    if (!needle) return true;
    const hay = [record.display_name, record.filename, triggerText(record), record.base_model]
        .filter(Boolean)
        .join(" ")
        .toLowerCase();
    return hay.includes(needle);
}

// ---------------------------------------------------------------------------
// Main export
// ---------------------------------------------------------------------------

export async function openAdapterPicker(valueWidget, credentials, filters, onPicked) {
    const shelf = SHELF_KINDS[filters?.fileKind] ?? SHELF_KINDS.adapter;
    /** The value written into the widget: a hash, or an id for checkpoints. */
    const idOf = (record) => (shelf.byId ? String(record.id ?? "") : String(record.sha256 ?? ""));

    // Several-selectable (the Multi Adapter Loader): the ticked digests in the
    // order they will fill the rows, and which of the two views is drawn.
    const several = filters?.several ?? null;
    const ticked  = several ? [...several.picked] : [];
    let people    = !!several?.people;
    // A ticked digest that a people card's choice put in place of another →
    // the one it replaced, so the row can keep its strengths.
    const replaced = new Map();

    let selectedValue = String(valueWidget?.value ?? "").trim() || null;
    let selectedRecord = null;

    const itemElements = [];
    let records   = [];   // what the grid draws: shelf records, or people
    let rendered  = 0;    // how much of it is on screen
    // Bumped whenever the grid is torn down, so in-flight icon fetches can
    // tell that the card they were destined for is gone.
    let gridGeneration = 0;
    let dismissed = false;
    let searchTimer = null;

    let all = [];             // the filtered shelf, fetched once
    // Several-selectable only:
    let loaded    = false;
    let shelfRows = [];       // every adapter, whatever the filters say
    let persons   = [];       // `all`, folded by person
    const byDigest = new Map();

    // -----------------------------------------------------------------------
    // Build DOM
    // -----------------------------------------------------------------------

    const overlay = el("div", {
        style: `
            position:fixed; inset:0; background:rgba(0,0,0,.78);
            display:flex; align-items:center; justify-content:center;
            z-index:10000; font-family:sans-serif;
        `,
    });

    const modal = el("div", {
        style: `
            background:#1e1e1e; border-radius:10px; padding:20px;
            width:88vw; max-width:1100px; height:78vh;
            display:flex; flex-direction:column; gap:10px;
            box-shadow:0 6px 40px rgba(0,0,0,.85); color:#e0e0e0;
        `,
    });

    const titleEl  = el("h2", { textContent: shelf.title, style: "margin:0; font-size:1.05em; flex:1;" });
    const countEl  = el("span", { style: "font-size:.85em; color:#aaa;" });
    const closeBtn = mkBtn("✕");
    const header   = mkRow(titleEl, countEl, closeBtn);

    const searchInput = el("input", { type: "text", style: selStyle() + "flex:1;" });
    // The switch between the two views: the node's `show` setting, drawn where
    // the grid it changes is, and written back when the grid is confirmed.
    const peopleBtn = mkBtn("People who fit");
    const allBtn    = mkBtn("All adapters");
    const filterText = el("span", { style: "color:#aaa; font-size:.85em; flex-shrink:0;" });
    const filterRow = mkRow(...(several ? [peopleBtn, allBtn] : []), filterText, searchInput);

    const grid = el("div", {
        style: `
            display:grid;
            grid-template-columns:repeat(auto-fill,minmax(150px,1fr));
            grid-auto-rows:auto;
            gap:10px; overflow-y:auto; flex:1; padding:4px;
            align-content:start;
        `,
    });

    const confirmBtn = mkBtn(several ? "Use" : `Use this ${shelf.noun}`, "#2a7a2a");
    const cancelBtn  = mkBtn("Cancel");
    // Says what confirming will do to the rows. Empty when one file is picked.
    const sayEl  = el("span", { style: "flex:1; font-size:.85em; color:#aaa;" });
    const footer = el("div", { style: "display:flex; align-items:center; justify-content:flex-end; gap:10px; flex-shrink:0;" });
    footer.append(sayEl, cancelBtn, confirmBtn);
    // Nothing to confirm until the shelf is here: confirming an unloaded grid
    // would be confirming a list nobody has seen.
    confirmBtn.disabled = !!several;

    modal.append(header, filterRow, grid, footer);
    overlay.appendChild(modal);
    document.body.appendChild(overlay);

    // -----------------------------------------------------------------------
    // Helpers
    // -----------------------------------------------------------------------

    function highlight(itemEl) {
        const at  = several ? ticked.indexOf(itemEl._value()) : -1;
        const sel = several ? at >= 0 : itemEl._value() === selectedValue;
        itemEl.style.outline       = sel ? "3px solid #4caf50" : "none";
        itemEl.style.outlineOffset = sel ? "2px" : "0";
        if (itemEl._badge) {
            // The number is the row the card will fill.
            itemEl._badge.textContent   = sel ? String(at + 1) : "";
            itemEl._badge.style.display = sel ? "" : "none";
        }
    }

    function close() {
        dismissed = true;
        if (searchTimer) clearTimeout(searchTimer);
        document.removeEventListener("keydown", onKeyDown, true);
        gridGeneration++;
        for (const item of itemElements) {
            if (item._objectUrl) URL.revokeObjectURL(item._objectUrl);
        }
        itemElements.length = 0;
        overlay.remove();
    }

    function onKeyDown(e) {
        if (e.key === "Escape") {
            e.preventDefault();
            close();
        }
    }

    function updateCount() {
        const n = records.length;
        countEl.textContent = people
            ? `${n} ${n === 1 ? "person" : "people"}`
            : `${n} ${shelf.noun}${n === 1 ? "" : "s"}`;
    }

    /** Tick or untick one digest, then redraw every card's number. */
    function toggle(value) {
        toggleTick(ticked, replaced, value, several.limit);
        refreshTicks();
    }

    /**
     * Redraw the ticks, and say in the footer what confirming will do.
     *
     * A tick whose card is not in this view — an adapter with no person, under
     * the people view; one the filters hide, under either — is not dropped by
     * confirming: it stays on the node, and the footer names it.
     */
    function refreshTicks() {
        for (const item of itemElements) highlight(item);
        if (!several || !loaded) return;

        const inView = new Set(people
            ? persons.flatMap(p => p.adapters.map(idOf))
            : all.map(idOf));
        const here   = ticked.filter(d => inView.has(d));
        const hidden = ticked.filter(d => !inView.has(d));
        const n = people
            ? new Set(here.map(d => personOf(byDigest.get(d)))).size
            : here.length;
        const noun = people ? (n === 1 ? "person" : "people") : `adapter${n === 1 ? "" : "s"}`;

        let say = n
            ? `${n} ${noun} picked, applied in the order picked.`
            : (people ? "Nobody picked." : "Nothing picked.");
        if (hidden.length) {
            const one = hidden.length === 1;
            const names = hidden.map(d => nameOf(byDigest.get(d)) || `${d.slice(0, 10)}…`);
            const notPeople = people
                && hidden.every(d => byDigest.has(d) && personOf(byDigest.get(d)) == null);
            const why = notPeople
                ? (one ? "is not a person" : "are not people")
                : (one ? "is not in this view" : "are not in this view");
            say += ` ${new Intl.ListFormat("en").format(names)} ${why} and `
                + `${one ? "stays" : "stay"} on the node.`;
        }
        if (ticked.length >= several.limit) say += ` All ${several.limit} rows are in use.`;
        sayEl.textContent = say;
        // With nothing ticked here, confirming still leaves the hidden rows.
        confirmBtn.textContent = n ? `Use ${n} ${noun}` : (hidden.length ? "Keep the rows" : "Use none");
    }

    /** The parts every card has: the square, its initials, and its tick number. */
    function cardShell(initials) {
        const item = el("div", {
            style: `
                cursor:pointer; background:#252525; border-radius:6px;
                padding:6px; display:flex; flex-direction:column; gap:4px;
            `,
        });
        item._objectUrl = null;

        // Square icon box. padding-top:100% forces height = width.
        const iconBox = el("div", {
            style: "position:relative; width:100%; padding-top:100%; background:#2a2a2a; border-radius:4px; overflow:hidden;",
        });
        const iconInner = el("div", {
            style: `
                position:absolute; inset:0; display:flex;
                align-items:center; justify-content:center;
                color:#666; font-size:1.6em; font-weight:bold;
            `,
        });
        // A record with no icon draws a generated mark, not a broken image.
        iconInner.textContent = initials;
        iconBox.appendChild(iconInner);
        if (several) {
            item._badge = el("div", {
                style: `
                    position:absolute; top:4px; left:4px; min-width:20px; height:20px;
                    padding:0 5px; box-sizing:border-box; border-radius:10px;
                    background:#5cbf62; color:#0d1a0e; font-size:12px; font-weight:700;
                    line-height:20px; text-align:center; display:none;
                `,
            });
            iconBox.appendChild(item._badge);
        }
        item.appendChild(iconBox);
        return { item, iconInner };
    }

    /** Fetch a card's picture and put it in its square, if the card is still there. */
    function loadFace(item, iconInner, fetchUrl) {
        // The grid may be rebuilt (or the modal closed) while this is in
        // flight. An icon that lands on a card no longer in itemElements
        // would never be revoked by close()/resetGrid(), so revoke it here
        // instead — that is the leak, and typing in the search box is the
        // way to hit it.
        const generation = gridGeneration;
        fetchUrl()
            .then(url => {
                if (!url) return;
                if (generation !== gridGeneration || !item.isConnected) {
                    URL.revokeObjectURL(url);
                    return;
                }
                item._objectUrl = url;
                iconInner.replaceChildren(el("img", {
                    src:   url,
                    style: "width:100%; height:100%; object-fit:contain; display:block;",
                }));
            })
            .catch(() => {});
    }

    function makeCard(record) {
        const { item, iconInner } = cardShell(initialsOf(record));
        item._value = () => idOf(record);

        item.appendChild(el("div", {
            textContent: nameOf(record) || idOf(record).slice(0, 12),
            title:       record.filename || "",
            style:       "font-size:.8em; line-height:1.25; overflow-wrap:anywhere;",
        }));

        const meta = [
            record.base_model || "Base model not set",
            record.kind,
            // Say the run is a run: the other files are on the shelf, they are
            // just not separate things to pick here.
            record._members > 1 ? `${record._members} files` : null,
        ].filter(Boolean).join(" · ");
        item.appendChild(el("div", {
            textContent: meta,
            style:       "font-size:.72em; color:#999; overflow-wrap:anywhere;",
        }));

        if (!isPresent(record)) {
            // Still selectable, but say plainly that this one will not load —
            // for a hash-addressed file because PixlStash has no copy to serve
            // either, and for a checkpoint because nothing serves those at all.
            item.appendChild(el("div", {
                textContent: "no copy on disk",
                title:       shelf.byId
                    ? "PixlStash last saw no readable copy of this checkpoint, "
                    + "and it does not serve checkpoint bytes in any case — "
                    + "reconnect the drive or rescan the folder it lives in."
                    : "PixlStash has no reachable copy of this file, so it "
                    + "cannot serve it either — reconnect the drive or rescan "
                    + "the folder it lives in.",
                style:       "font-size:.68em; color:#d0a24c; line-height:1.2;",
            }));
        }

        if (record.icon_sha256 || (record.attachments || []).length) {
            loadFace(item, iconInner, () => fetchFaceUrl(record, credentials));
        }

        item.addEventListener("click", () => {
            if (several) {
                toggle(idOf(record));
                return;
            }
            selectedValue  = idOf(record);
            selectedRecord = record;
            for (const other of itemElements) highlight(other);
        });
        item.addEventListener("dblclick", () => {
            // Several-selectable has no "this one and done": the second click
            // of a double click has already unticked what the first ticked.
            if (several) return;
            selectedValue  = idOf(record);
            selectedRecord = record;
            confirmSelection();
        });

        highlight(item);
        return item;
    }

    /**
     * One person: their face, and the adapter of theirs a tick stands for.
     *
     * With more than one that fits, the card carries the choice. Changing it on
     * a ticked card swaps the digest where it stands, so the row keeps its
     * place.
     */
    function makePersonCard(person) {
        const { item, iconInner } = cardShell(initialsOf({ display_name: person.name }));
        person.chosen = person.adapters.find(a => ticked.includes(idOf(a)))
            ?? person.chosen ?? person.adapters[0];
        item._value = () => idOf(person.chosen);

        item.appendChild(el("div", {
            textContent: person.name,
            style:       "font-size:.8em; font-weight:600; line-height:1.25; overflow-wrap:anywhere;",
        }));

        const meta = el("div", { style: "font-size:.72em; color:#999; overflow-wrap:anywhere;" });
        const trigger = el("div", {
            style: `
                align-self:flex-start; max-width:100%; box-sizing:border-box;
                font:11px/1.5 monospace; background:#1a1c1f; color:#cfe38a;
                border-radius:4px; padding:0 6px; overflow-wrap:anywhere;
            `,
        });
        const describe = () => {
            const adapter = person.chosen;
            meta.textContent = [
                person.adapters.length > 1
                    ? `${person.adapters.length} of their adapters fit`
                    : [nameOf(adapter), adapter.kind].filter(Boolean).join(" · "),
                isPresent(adapter) ? null : "no copy on disk",
            ].filter(Boolean).join(" · ");
            const words = triggerText(adapter);
            trigger.textContent   = words;
            trigger.style.display = words ? "" : "none";
        };

        let select = null;
        if (person.adapters.length > 1) {
            select = el("select", { style: selStyle() + "width:100%; min-width:0;" });
            for (const adapter of person.adapters) {
                select.appendChild(el("option", {
                    value:       idOf(adapter),
                    textContent: nameOf(adapter) || idOf(adapter).slice(0, 12),
                }));
            }
            select.value = idOf(person.chosen);
            // Choosing between their adapters is not ticking the person.
            select.addEventListener("click", e => e.stopPropagation());
            select.addEventListener("change", () => {
                const was = idOf(person.chosen);
                person.chosen = person.adapters.find(a => idOf(a) === select.value) ?? person.chosen;
                swapTick(ticked, replaced, was, idOf(person.chosen));
                describe();
                refreshTicks();
            });
            item.appendChild(select);
        }
        item.append(meta, trigger);
        describe();

        // By face: the person's own, not the icon somebody gave one of their
        // files. No reference face leaves the initials, as on an adapter card.
        loadFace(item, iconInner, () => fetchBlobUrl("/pixlstash/entity_thumbnail", credentials, {
            entity_type: "character",
            entity_id:   person.id,
        }));

        item.addEventListener("click", () => {
            toggle(idOf(person.chosen));
            // Two of their adapters can be ticked (from the other view). With
            // the shown one unticked the card is still a ticked person, and
            // has to show the one that is left.
            const other = person.adapters.find(a => ticked.includes(idOf(a)));
            if (!other || ticked.includes(idOf(person.chosen))) return;
            person.chosen = other;
            if (select) select.value = idOf(other);
            describe();
            highlight(item);
        });
        highlight(item);
        return item;
    }

    /** Append the next slice of the already-fetched list. */
    function renderMore() {
        const slice = records.slice(rendered, rendered + PAGE_SIZE);
        for (const record of slice) {
            const item = people ? makePersonCard(record) : makeCard(record);
            itemElements.push(item);
            grid.appendChild(item);
        }
        rendered += slice.length;

        // Keep filling while the grid isn't scrollable yet. The clientHeight
        // test guards against a not-yet-laid-out modal measuring 0, which
        // would otherwise read as "never scrollable" and render the whole
        // unpaginated shelf in one synchronous burst.
        if (rendered < records.length
            && grid.clientHeight > 0
            && grid.scrollHeight <= grid.clientHeight) {
            renderMore();
        }
    }

    function resetGrid() {
        gridGeneration++;
        for (const item of itemElements) {
            if (item._objectUrl) URL.revokeObjectURL(item._objectUrl);
        }
        itemElements.length = 0;
        grid.replaceChildren();
        rendered = 0;
        if (!records.length) {
            if (people) {
                if (persons.length) showNotice("Nobody matches this search.");
                else showNobodyFits();
                return;
            }
            // "No VAEs match these filters" over an empty shelf reads as a
            // broken node. The two cases are worth telling apart: a search that
            // matched nothing, and a shelf that holds none of this kind at all
            // — which is nearly always a folder PixlStash was never pointed at.
            showNotice(all.length
                ? `No ${shelf.noun}s match this search.`
                // The several-selectable grid has the whole shelf beside the
                // filtered one, so it can tell a filter from an empty shelf.
                : shelfRows.length
                ? `No ${shelf.noun}s match the node's filters (${describeFilters(filters, shelf).replace("Filtered: ", "")}).`
                : `Your shelf holds no ${shelf.noun}s. PixlStash only catalogues `
                  + `the folders registered under Settings › Model folders — add `
                  + `the folder your ${shelf.noun}s live in, then rescan it.`);
            return;
        }
        renderMore();
    }

    function showNotice(message, colour = "#888") {
        grid.replaceChildren(el("div", {
            textContent: message,
            style: `grid-column:1/-1; color:${colour}; padding:12px; text-align:center; font-size:.9em;`,
        }));
    }

    /**
     * The people view with nobody in it.
     *
     * An empty grid would read as a shelf with no people on it. What is nearly
     * always true instead is that people have adapters and none passes the
     * filters, so it says how many there are and for which base models, and
     * offers the other view.
     */
    function showNobodyFits() {
        // Folded as the people list is — stacks first, then by person — so
        // the count is of the same things the grid would have drawn.
        const attached = collapseStacks(shelfRows).filter(r => personOf(r) != null);
        const lines = [];
        if (!attached.length) {
            lines.push(
                "No adapter on your shelf is attached to a person.",
                "Attach one to a character in PixlStash and that person is offered here.",
            );
        } else {
            const { kind, baseModel } = filters;
            const what = kind ? `a ${kind}` : "an adapter";
            lines.push(baseModel ? `Nobody has ${what} for ${baseModel}.` : `Nobody has ${what}.`);

            const counts = new Map();
            for (const r of attached) {
                const key = r.base_model || "no base model";
                counts.set(key, (counts.get(key) ?? 0) + 1);
            }
            const breakdown = [...counts]
                .sort((a, b) => b[1] - a[1])
                .map(([name, count]) => `${name} ${count}`)
                .join(", ");
            const n = attached.length;
            const are = `${n} adapter${n === 1 ? " is" : "s are"} attached to people`;
            lines.push(kind
                ? `${are}, but ${n === 1 ? "it does not fit" : "none fits"}: ${breakdown}.`
                : `${are}, for other base models: ${breakdown}.`);
        }

        const notice = el("div", {
            style: `
                grid-column:1/-1; display:flex; flex-direction:column; align-items:center;
                gap:10px; padding:24px 12px; text-align:center; font-size:.9em; color:#bbb;
            `,
        });
        notice.append(
            el("div", { textContent: lines[0], style: "color:#e8e8e8; font-weight:600;" }),
            el("div", { textContent: lines[1] }),
        );
        const showAll = mkBtn("Show all adapters");
        showAll.addEventListener("click", () => setView(false));
        notice.appendChild(showAll);
        grid.replaceChildren(notice);
    }

    /** Draw which view is on: the switch, what the grid is narrowed to, the search hint. */
    function paintView() {
        peopleBtn.style.background = people ? "#555" : "#2d2d2d";
        allBtn.style.background    = people ? "#2d2d2d" : "#555";
        searchInput.placeholder = people
            ? "Search a person, adapter or trigger word…"
            : "Search name, filename or trigger words…";
        const narrowed = !!(filters?.kind || filters?.baseModel);
        filterText.textContent = people
            ? [filters.baseModel ? `Fits ${filters.baseModel}` : "Any base model", filters.kind]
                .filter(Boolean).join(" · ")
            // Beside a switch that already reads "All adapters", saying it
            // again is noise.
            : (several && !narrowed ? "" : describeFilters(filters, shelf));
    }

    function setView(toPeople) {
        if (people === toPeople) return;
        people = toPeople;
        paintView();
        if (loaded) applySearch();
    }

    function confirmSelection() {
        if (several) {
            close();
            // The view is the node's `show` setting, which is an input like any
            // other: written with the rows, on confirm, and not at all on Cancel.
            if (people !== !!several.people) several.onView?.(people);
            onPicked?.([...ticked], replaced);
            return;
        }
        const picked = selectedRecord ?? records.find(r => idOf(r) === selectedValue) ?? null;
        // Nothing new was chosen (the list failed to load, or the pre-seeded
        // value isn't in it) — close without touching the widget or the label,
        // rather than blanking a label whose value is still set.
        if (!picked) {
            close();
            return;
        }
        valueWidget.value = idOf(picked);
        if (typeof valueWidget.callback === "function") {
            valueWidget.callback(valueWidget.value);
        }
        close();
        onPicked?.(picked);
    }

    // The search box filters the fetched array rather than re-querying — the
    // whole filtered shelf is already here.
    function applySearch() {
        if (dismissed) return;
        const needle = searchInput.value.trim().toLowerCase();
        records = people
            ? persons.filter(p => matchesPerson(p, needle))
            : all.filter(r => matchesSearch(r, needle));
        updateCount();
        resetGrid();
        refreshTicks();
    }

    // -----------------------------------------------------------------------
    // Wire up + load
    // -----------------------------------------------------------------------

    grid.addEventListener("scroll", () => {
        if (grid.scrollTop + grid.clientHeight >= grid.scrollHeight - 250) renderMore();
    });
    closeBtn.addEventListener("click",  close);
    cancelBtn.addEventListener("click", close);
    confirmBtn.addEventListener("click", confirmSelection);
    peopleBtn.addEventListener("click", () => setView(true));
    allBtn.addEventListener("click",    () => setView(false));
    overlay.addEventListener("click", e => { if (e.target === overlay) close(); });
    document.addEventListener("keydown", onKeyDown, true);

    paintView();
    showNotice("Loading…");

    try {
        const data = await proxyFetch(shelf.path, credentials, buildAdapterQuery(filters));
        let rows = data?.[shelf.listKey];
        rows = Array.isArray(rows) ? rows : [];
        if (filters?.workflowSetId) {
            // Before the stack fold, not after: a set may hold an epoch that
            // is not its stack's cover, and folding first would hide it.
            const loras = await fetchWorkflowSetLoras(credentials, filters.workflowSetId);
            rows = rows.filter(r => r && loras.has(r.sha256));
        }
        // A row with no identity cannot be picked, written or resolved again.
        // For a checkpoint that is the not-yet-hashed case, which is ordinary —
        // it has an id, so it is only the hash-addressed kinds that lose rows.
        all = collapseStacks(rows.filter(r => r && idOf(r)));

        if (several) {
            // Beside the filtered shelf, the whole of it: the people view says
            // who does NOT fit, and the footer names a pick the filters hide.
            // Asked for rather than filtered here, because what "fits" a base
            // model is the server's rule (it matches the identified model as
            // well as the string) and a copy of it would drift.
            const narrowed = !!(filters.kind || filters.baseModel);
            const [whole, characters] = await Promise.all([
                narrowed ? proxyFetch(shelf.path, credentials, { file_kind: "adapter" }) : data,
                // Names only: without them a person is still a face and a number.
                proxyFetch("/pixlstash/characters", credentials).catch(() => []),
            ]);
            const wholeRows = whole?.[shelf.listKey];
            shelfRows = (Array.isArray(wholeRows) ? wholeRows : []).filter(r => r && idOf(r));
            for (const r of [...shelfRows, ...all]) byDigest.set(idOf(r), r);
            persons = foldPeople(all, new Map(
                (Array.isArray(characters) ? characters : []).map(c => [String(c.id), c.name]),
            ));
        }
    } catch (err) {
        if (!dismissed) showNotice(`⚠ ${err.message}`, "#f88");
        return;
    }

    // Escape (or a backdrop click) during the fetch has already torn the modal
    // down. Rendering now would request an icon per card for a grid nobody is
    // looking at, and focus a detached input.
    if (dismissed) return;

    loaded = true;
    confirmBtn.disabled = false;
    searchInput.addEventListener("input", () => {
        if (searchTimer) clearTimeout(searchTimer);
        searchTimer = setTimeout(applySearch, SEARCH_DEBOUNCE_MS);
    });

    applySearch();
    searchInput.focus();
}

// ---------------------------------------------------------------------------
// Tiny helpers (local)
// ---------------------------------------------------------------------------

function describeFilters({ kind, baseModel, characterId, setId, workflowSetId } = {}, shelf = { noun: "adapter" }) {
    const parts = [];
    if (workflowSetId) parts.push(`workflow set #${workflowSetId}`);
    if (kind)        parts.push(kind);
    if (baseModel)   parts.push(baseModel);
    if (characterId) parts.push(`character #${characterId}`);
    else if (setId)  parts.push(`set #${setId}`);
    return parts.length ? `Filtered: ${parts.join(" · ")}` : `All ${shelf.noun}s`;
}

/** Two letters for the generated mark shown when a record has no icon. */
export function initialsOf(record) {
    const name = String(record.display_name || record.filename || "?");
    const words = name.replace(/[_\-.]+/g, " ").split(/\s+/).filter(Boolean);
    return (words.slice(0, 2).map(w => w[0]).join("") || "?").toUpperCase();
}

// el / mkBtn / mkRow / selStyle come from ./modal_dom.js — see the import.

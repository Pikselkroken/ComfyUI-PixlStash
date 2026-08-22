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
 *   filters     — { fileKind, kind, baseModel, characterId, setId }
 *   onPicked    — called with the chosen shelf record after confirm
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
 */
async function fetchFaceUrl(record, credentials) {
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
 * is no expand-the-strip here: this picker exists to choose one file to load,
 * and the cover is that file by definition.
 */
function collapseStacks(rows) {
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

/** What a card (and the Browse button) calls a record. */
function nameOf(record) {
    return record?.display_name || record?.filename || null;
}

// value → name, for the button labels. A shelf record's name does not change
// while a graph is open, and several nodes commonly hold the same file.
const _nameCache = new Map();

/**
 * The display name of an already-selected file, or `null`.
 *
 * A saved workflow carries only the hash (or the checkpoint id), so a reloaded
 * node has nothing to put on its button but that. This is the lookup that
 * turns it back into a name — one small request for a hash-addressed file, and
 * for a checkpoint the list route, since the server has no by-id one.
 *
 * Never throws: a failure here costs a nicer label and nothing else, so an
 * unreachable server or an expired token leaves the hash on the button rather
 * than raising into a canvas redraw. Failures are not cached, so fixing the
 * token and reloading the graph is enough to get names back.
 */
export async function shelfNameFor(value, credentials, fileKind) {
    const shelf = SHELF_KINDS[fileKind] ?? SHELF_KINDS.adapter;
    const key = `${fileKind}:${value}`;
    if (_nameCache.has(key)) return _nameCache.get(key);

    let name = null;
    try {
        if (shelf.byId) {
            const data = await proxyFetch(shelf.path, credentials, {});
            const rows = data?.[shelf.listKey];
            name = nameOf((Array.isArray(rows) ? rows : []).find(r => String(r?.id) === value));
        } else {
            name = nameOf(await proxyFetch("/pixlstash/adapter", credentials, { sha256: value }));
        }
    } catch {
        return null;
    }
    if (name) _nameCache.set(key, name);
    return name;
}

/** Fields the in-modal search box matches against. */
function matchesSearch(record, needle) {
    if (!needle) return true;
    const hay = [record.display_name, record.filename, record.trigger_words, record.base_model]
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

    let selectedValue = String(valueWidget.value ?? "").trim() || null;
    let selectedRecord = null;

    const itemElements = [];
    let records   = [];   // the filtered shelf, fetched once
    let rendered  = 0;    // how much of it is on screen
    // Bumped whenever the grid is torn down, so in-flight icon fetches can
    // tell that the card they were destined for is gone.
    let gridGeneration = 0;
    let dismissed = false;
    let searchTimer = null;

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

    const searchInput = el("input", {
        type: "text",
        placeholder: "Search name, filename or trigger words…",
        style: selStyle() + "flex:1;",
    });
    const filterRow = mkRow(
        el("span", { textContent: describeFilters(filters, shelf), style: "color:#aaa; font-size:.85em; flex-shrink:0;" }),
        searchInput,
    );

    const grid = el("div", {
        style: `
            display:grid;
            grid-template-columns:repeat(auto-fill,minmax(150px,1fr));
            grid-auto-rows:auto;
            gap:10px; overflow-y:auto; flex:1; padding:4px;
            align-content:start;
        `,
    });

    const confirmBtn = mkBtn(`Use this ${shelf.noun}`, "#2a7a2a");
    const cancelBtn  = mkBtn("Cancel");
    const footer = el("div", { style: "display:flex; justify-content:flex-end; gap:10px; flex-shrink:0;" });
    footer.append(cancelBtn, confirmBtn);

    modal.append(header, filterRow, grid, footer);
    overlay.appendChild(modal);
    document.body.appendChild(overlay);

    // -----------------------------------------------------------------------
    // Helpers
    // -----------------------------------------------------------------------

    function highlight(itemEl) {
        const sel = itemEl._value === selectedValue;
        itemEl.style.outline       = sel ? "3px solid #4caf50" : "none";
        itemEl.style.outlineOffset = sel ? "2px" : "0";
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
        countEl.textContent = `${records.length} ${shelf.noun}${records.length === 1 ? "" : "s"}`;
    }

    function makeCard(record) {
        const item = el("div", {
            style: `
                cursor:pointer; background:#252525; border-radius:6px;
                padding:6px; display:flex; flex-direction:column; gap:4px;
            `,
        });
        item._value     = idOf(record);
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
        iconInner.textContent = initialsOf(record);
        iconBox.appendChild(iconInner);
        item.appendChild(iconBox);

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
            // The grid may be rebuilt (or the modal closed) while this is in
            // flight. An icon that lands on a card no longer in itemElements
            // would never be revoked by close()/resetGrid(), so revoke it here
            // instead — that is the leak, and typing in the search box is the
            // way to hit it.
            const generation = gridGeneration;
            fetchFaceUrl(record, credentials)
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

        item.addEventListener("click", () => {
            selectedValue  = idOf(record);
            selectedRecord = record;
            for (const other of itemElements) highlight(other);
        });
        item.addEventListener("dblclick", () => {
            selectedValue  = idOf(record);
            selectedRecord = record;
            confirmSelection();
        });

        highlight(item);
        return item;
    }

    /** Append the next slice of the already-fetched list. */
    function renderMore() {
        const slice = records.slice(rendered, rendered + PAGE_SIZE);
        for (const record of slice) {
            const item = makeCard(record);
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
            // "No VAEs match these filters" over an empty shelf reads as a
            // broken node. The two cases are worth telling apart: a search that
            // matched nothing, and a shelf that holds none of this kind at all
            // — which is nearly always a folder PixlStash was never pointed at.
            showNotice(all.length
                ? `No ${shelf.noun}s match this search.`
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

    function confirmSelection() {
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

    // -----------------------------------------------------------------------
    // Wire up + load
    // -----------------------------------------------------------------------

    grid.addEventListener("scroll", () => {
        if (grid.scrollTop + grid.clientHeight >= grid.scrollHeight - 250) renderMore();
    });
    closeBtn.addEventListener("click",  close);
    cancelBtn.addEventListener("click", close);
    confirmBtn.addEventListener("click", confirmSelection);
    overlay.addEventListener("click", e => { if (e.target === overlay) close(); });
    document.addEventListener("keydown", onKeyDown, true);

    showNotice("Loading…");

    let all = [];
    try {
        const data = await proxyFetch(shelf.path, credentials, buildAdapterQuery(filters));
        const rows = data?.[shelf.listKey];
        // A row with no identity cannot be picked, written or resolved again.
        // For a checkpoint that is the not-yet-hashed case, which is ordinary —
        // it has an id, so it is only the hash-addressed kinds that lose rows.
        all = collapseStacks(Array.isArray(rows) ? rows.filter(r => r && idOf(r)) : []);
    } catch (err) {
        if (!dismissed) showNotice(`⚠ ${err.message}`, "#f88");
        return;
    }

    // Escape (or a backdrop click) during the fetch has already torn the modal
    // down. Rendering now would request an icon per card for a grid nobody is
    // looking at, and focus a detached input.
    if (dismissed) return;

    // The search box filters the fetched array rather than re-querying — the
    // whole filtered shelf is already here.
    function applySearch() {
        if (dismissed) return;
        const needle = searchInput.value.trim().toLowerCase();
        records = all.filter(r => matchesSearch(r, needle));
        updateCount();
        resetGrid();
    }
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

function describeFilters({ kind, baseModel, characterId, setId } = {}, shelf = { noun: "adapter" }) {
    const parts = [];
    if (kind)        parts.push(kind);
    if (baseModel)   parts.push(baseModel);
    if (characterId) parts.push(`character #${characterId}`);
    else if (setId)  parts.push(`set #${setId}`);
    return parts.length ? `Filtered: ${parts.join(" · ")}` : `All ${shelf.noun}s`;
}

/** Two letters for the generated mark shown when a record has no icon. */
function initialsOf(record) {
    const name = String(record.display_name || record.filename || "?");
    const words = name.replace(/[_\-.]+/g, " ").split(/\s+/).filter(Boolean);
    return (words.slice(0, 2).map(w => w[0]).join("") || "?").toUpperCase();
}

// el / mkBtn / mkRow / selStyle come from ./modal_dom.js — see the import.

/**
 * PixlStash picture-picker modal.
 *
 * Exported API
 * ────────────
 * openPicker(node, pictureIdsWidget, credentials, initialFilters)
 *
 *   node             — LiteGraph node instance (Picture Loader)
 *   pictureIdsWidget — the widget whose .value will be set on confirm
 *   credentials      — { url, token, verifySsl }
 *   initialFilters   — { projectId, setId, characterId } (may be empty strings)
 *
 * Laid out like PixlStash's own "Choose a picture" dialog: a rail of the
 * groupings the library already keeps (project, character, picture set) on
 * the left, search and the thumbnail grid on the right, and what is chosen
 * plus the verbs in the footer. Unlike PixlStash's it stays multi-select —
 * the Picture Loader takes several pictures — with ctrl-click to add and
 * shift-click for a range, and the choice survives moving between groupings.
 *
 * The modal fetches thumbnails from PixlStash directly (binary, not JSON),
 * pages the list by infinite scroll, and writes confirmed IDs back into the
 * widget. Inline node previews are updated after confirm.
 *
 * Beside the IDs it writes the uuid of the library they were picked from into
 * the node's `library` widget: an ID means nothing outside its library, and
 * PixlStash reads the IDs back out of a picture's workflow only when that
 * names the library it has open.
 */

import { el } from "./modal_dom.js";

// ---------------------------------------------------------------------------
// Proxy fetch helper  (JSON endpoints only — goes through ComfyUI proxy)
// ---------------------------------------------------------------------------

// The header of PixlStash's picture list that says which library its ids are of.
const LIBRARY_HEADER = "X-PixlStash-Library";

/**
 * The library every page listed so far is of, given the one known before
 * this page (`undefined` before the first) and the one this page names ("" for
 * none). One library, or none: pages that do not agree name no library, and
 * none after them brings one back. That is PixlStash having switched library,
 * or the key having changed, while the picker was open, and what is picked
 * across it is not known to be anybody's.
 */
export function libraryOfPages(known, named) {
    return known === undefined || known === named ? named : "";
}

async function proxyFetch(path, credentials, extraParams = {}, seen = null) {
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
    // Whose the listed ids are, for a caller that asked to be told.
    if (seen) seen.library = libraryOfPages(seen.library, resp.headers.get(LIBRARY_HEADER) ?? "");
    return resp.json();
}

// ---------------------------------------------------------------------------
// Thumbnail helper  (binary WebP — streamed through the ComfyUI proxy)
// ---------------------------------------------------------------------------
//
// Goes through /pixlstash/thumbnail rather than fetching PixlStash directly:
// the browser cannot reach a self-signed / private PixlStash instance, but the
// server-side proxy (which honours the VerifySSL setting) can.

async function fetchThumbnailUrl(pictureId, credentials) {
    const params = new URLSearchParams({
        url:        credentials.url,
        verify_ssl: credentials.verifySsl ? "true" : "false",
        picture_id: String(pictureId),
    });
    const resp = await fetch(`/pixlstash/thumbnail?${params}`, {
        headers: { "Authorization": `Bearer ${credentials.token}` },
    });
    if (!resp.ok) return null;
    return URL.createObjectURL(await resp.blob());
}

// ---------------------------------------------------------------------------
// Inline node preview update  (exported for use by combo_widgets.js)
// ---------------------------------------------------------------------------

export async function updateNodePreviews(node, pictureIds, credentials) {
    if (node._pixlstashUrls) {
        for (const u of node._pixlstashUrls) URL.revokeObjectURL(u);
    }
    node._pixlstashUrls = [];

    if (!pictureIds.length) {
        node.imgs = null;
        node.setSizeForImage?.();
        return;
    }

    const urls = (
        await Promise.all(pictureIds.map(id => fetchThumbnailUrl(id, credentials)))
    ).filter(Boolean);

    node._pixlstashUrls = urls;
    node.imgs = urls.map(url => {
        const img = new Image();
        img.src = url;
        return img;
    });
    node.setSizeForImage?.();
    try { node.graph?.setDirtyCanvas(true); } catch { /* canvas may not be ready */ }
}

// ---------------------------------------------------------------------------
// The rail  (pure — exported for the tests)
// ---------------------------------------------------------------------------

/**
 * The rail's rows from PixlStash's three lists, biggest first as PixlStash
 * orders them. A character's reference set is left out of the sets: it is
 * that character's face, and the character is already on the rail.
 *
 * A row PixlStash sent no count for (one too old for `include_counts`) has
 * none rather than 0, and keeps the server's order: a rail of zeros would say
 * every grouping is empty.
 */
export function railGroups(projects, characters, sets) {
    const rows = (list, count) => (Array.isArray(list) ? list : [])
        .map(r => {
            const n = r[count] == null ? NaN : Number(r[count]);
            return { id: String(r.id), name: String(r.name ?? r.id), count: Number.isFinite(n) ? n : null };
        })
        .sort((a, b) => (b.count ?? -1) - (a.count ?? -1));
    return {
        project:   rows(projects, "image_count"),
        character: rows(characters, "image_count"),
        set:       rows((Array.isArray(sets) ? sets : []).filter(s => !s.reference_character), "picture_count"),
    };
}

/**
 * The grouping the picker opens on: the one a wired loader names, the
 * narrowest of them where several are wired, or Everything.
 */
export function initialFacet({ projectId = "", setId = "", characterId = "" } = {}) {
    if (setId)       return { kind: "set",       id: String(setId) };
    if (characterId) return { kind: "character", id: String(characterId) };
    if (projectId)   return { kind: "project",   id: String(projectId) };
    return { kind: "", id: "" };
}

/** A count grouped the way PixlStash groups them: 1 142, not 1,142. */
function grouped(n) {
    return Number(n || 0).toLocaleString("en-GB").replace(/,/g, " ");
}

// ---------------------------------------------------------------------------
// Styling — PixlStash's dark theme, so the two pickers look like one
// ---------------------------------------------------------------------------

const STYLE_ID = "pixlstash-picker-style";
const STYLE = `
.pxp-overlay { position:fixed; inset:0; z-index:10000; display:flex; align-items:center; justify-content:center;
    background:rgba(0,0,0,.6); font-family:Inter,Roboto,system-ui,sans-serif; }
.pxp-modal { width:min(1100px,92vw); height:min(820px,90vh); display:flex; flex-direction:column;
    background:#23282f; color:#f2e5da; color-scheme:dark; border:1px solid #2c323a; border-radius:12px;
    box-shadow:0 12px 48px rgba(0,0,0,.6); overflow:hidden; }
.pxp-title { display:flex; align-items:baseline; gap:12px; padding:20px 20px 16px; border-bottom:1px solid #2c323a; }
.pxp-title h2 { margin:0; font-size:1.3em; font-weight:600; }
.pxp-sub { flex:1; font-family:ui-monospace,monospace; font-size:.85em; color:rgba(242,229,218,.65);
    overflow:hidden; text-overflow:ellipsis; white-space:nowrap; }
.pxp-x { align-self:center; background:none; border:0; color:inherit; font-size:1.3em; cursor:pointer; padding:2px 6px; border-radius:6px; }
.pxp-x:hover { background:rgba(242,229,218,.16); }
.pxp-body { flex:1; display:flex; min-height:0; }
.pxp-rail { width:208px; flex-shrink:0; overflow-y:auto; padding:12px; border-right:1px solid #2c323a; }
.pxp-sec { padding:16px 12px 8px; font-size:.72em; font-weight:600; letter-spacing:.08em; text-transform:uppercase;
    color:rgba(242,229,218,.65); }
.pxp-sec:first-child { padding-top:8px; }
.pxp-facet, .pxp-more { display:flex; align-items:center; gap:8px; width:100%; min-height:36px; padding:8px 12px;
    border:0; border-radius:6px; background:transparent; color:inherit; font:inherit; font-size:.9em;
    text-align:left; cursor:pointer; }
.pxp-facet:hover, .pxp-more:hover { background:rgba(242,229,218,.16); }
.pxp-facet--on { background:rgba(142,166,4,.2); font-weight:500; }
.pxp-facet__label { flex:1; overflow:hidden; text-overflow:ellipsis; white-space:nowrap; }
.pxp-facet__count { font-size:.85em; font-variant-numeric:tabular-nums; color:rgba(242,229,218,.65); }
.pxp-more { font-size:.75em; }
.pxp-main { flex:1; min-width:0; display:flex; flex-direction:column; }
.pxp-head { display:flex; align-items:center; gap:12px; padding:16px 20px; border-bottom:1px solid #2c323a; }
.pxp-search { flex:1; min-width:0; display:flex; align-items:center; gap:8px; padding:0 12px; height:34px;
    background:#2b3138; border:1px solid #3a414a; border-radius:6px; }
.pxp-search:focus-within { border-color:#8EA604; }
.pxp-search span { color:rgba(242,229,218,.65); }
.pxp-search input { flex:1; min-width:0; background:none; border:0; outline:0; color:inherit; font:inherit; font-size:.95em; }
.pxp-select { background:#2b3138; color:inherit; border:1px solid #3a414a; border-radius:6px; padding:6px 8px; font:inherit; font-size:.85em; }
.pxp-select:disabled { opacity:.5; }
.pxp-scroll { flex:1; overflow-y:auto; padding:20px; }
.pxp-grid { display:grid; grid-template-columns:repeat(5,minmax(0,1fr)); gap:12px; }
.pxp-cell { position:relative; isolation:isolate; aspect-ratio:1/1; padding:0; border:0; border-radius:8px;
    background:#2b3138; overflow:hidden; cursor:pointer; color:rgba(242,229,218,.4); font-size:.75em; }
.pxp-cell img { width:100%; height:100%; object-fit:cover; display:block; }
.pxp-cell--on::after { content:""; position:absolute; inset:0; border-radius:inherit; pointer-events:none;
    background:rgba(142,166,4,.2); box-shadow:inset 0 0 0 4px #8EA604; }
.pxp-tick { position:absolute; z-index:1; top:8px; right:8px; width:24px; height:24px; display:flex;
    align-items:center; justify-content:center; border-radius:999px; background:#567309; color:#f7f1ea;
    box-shadow:0 2px 6px rgba(0,0,0,.5); font-size:.9rem; font-weight:700; line-height:1; pointer-events:none; }
.pxp-note:empty { display:none; }
.pxp-note { margin:0 0 12px; font-size:.9em; color:rgba(242,229,218,.65); }
.pxp-note--error { color:#ff8a80; }
.pxp-foot { display:flex; align-items:center; gap:12px; padding:16px 20px; border-top:1px solid #2c323a; }
.pxp-chosen { font-size:.85em; color:rgba(242,229,218,.65); }
.pxp-link { background:none; border:0; padding:0; color:#8EA604; font:inherit; font-size:.85em; cursor:pointer; }
.pxp-spacer { flex:1; }
.pxp-btn { display:inline-flex; align-items:center; gap:8px; height:36px; padding:0 16px; border-radius:6px;
    border:1px solid #3a414a; background:#2b3138; color:inherit; font:inherit; font-size:.95em; cursor:pointer; }
.pxp-btn:hover:not(:disabled) { filter:brightness(1.15); }
.pxp-btn--primary { background:#567309; border-color:#567309; color:#f7f1ea; }
.pxp-btn:disabled { opacity:.45; cursor:default; }
.pxp-btn kbd, .pxp-search kbd { padding:1px 6px; border:1px solid rgba(242,229,218,.35); border-radius:4px;
    font-family:ui-monospace,monospace; font-size:.75em; }
`;

function injectStyle() {
    if (document.getElementById(STYLE_ID)) return;
    document.head.appendChild(el("style", { id: STYLE_ID, textContent: STYLE }));
}

function btn(label, key, primary = false) {
    const b = el("button", { type: "button", className: `pxp-btn${primary ? " pxp-btn--primary" : ""}` });
    b.append(label, el("kbd", { textContent: key }));
    return b;
}

// ---------------------------------------------------------------------------
// Main export
// ---------------------------------------------------------------------------

// How many rows of a rail group show before `All N ›`, as in PixlStash.
const FACET_PREVIEW = 3;
const PAGE_SIZE = 48;
// Search answers in one go; this is where it stops, and the grid says so.
const SEARCH_CAP = 120;

export async function openPicker(node, pictureIdsWidget, credentials, initialFilters) {
    injectStyle();

    // Pre-seed selection from the widget's current value.
    const initialIds = (pictureIdsWidget.value ?? "")
        .split(",")
        .map(s => Number(s.trim()))
        .filter(n => n > 0);
    const selectedIds = new Set(initialIds);

    const itemElements = [];
    let lastClickedIdx = -1;

    // The library the grid's pictures are of, as PixlStash names it on the
    // list itself. Empty where it names none (a key that is not the owner's,
    // an older PixlStash): the pick lands all the same, saying nothing of
    // whose it is.
    const libraryWidget = node.widgets?.find(w => w.name === "library");
    const listed = { library: undefined };

    // First pre-selected ID — we keep paging until we find it, then scroll it
    // into view once.
    let pendingScrollToId = initialIds[0] ?? null;

    const facet = initialFacet(initialFilters);
    const expanded = { character: false, set: false };
    let groups = railGroups([], [], []);
    let totalCount = null;

    // -----------------------------------------------------------------------
    // Build DOM
    // -----------------------------------------------------------------------

    const overlay = el("div", { className: "pxp-overlay" });
    const modal   = el("div", { className: "pxp-modal", role: "dialog", ariaModal: "true", ariaLabel: "Choose a picture" });

    const closeBtn = el("button", { type: "button", className: "pxp-x", textContent: "✕", title: "Close", ariaLabel: "Close" });
    const title = el("div", { className: "pxp-title" });
    title.append(
        el("h2", { textContent: "Choose a picture" }),
        el("span", { className: "pxp-sub", textContent: `for ${node.title || "Picture Loader"}` }),
        closeBtn,
    );

    const rail = el("nav", { className: "pxp-rail", ariaLabel: "Narrow the pictures" });

    // Search + sort
    const searchInput = el("input", { type: "search", placeholder: "Search this library", ariaLabel: "Search this library" });
    const searchBox = el("label", { className: "pxp-search" });
    searchBox.append(el("span", { textContent: "⌕", ariaHidden: "true" }), searchInput);
    const sortSel       = el("select", { className: "pxp-select", title: "Sort" });
    const descendingSel = el("select", { className: "pxp-select", title: "Order" });
    descendingSel.append(
        el("option", { value: "true",  textContent: "↓ Desc" }),
        el("option", { value: "false", textContent: "↑ Asc" }),
    );
    const likenessSel = el("select", { className: "pxp-select", title: "Likeness to" });
    likenessSel.style.display = "none";
    const head = el("div", { className: "pxp-head" });
    head.append(searchBox, sortSel, descendingSel, likenessSel);

    // Grid
    const scroll = el("div", { className: "pxp-scroll" });
    const note   = el("p", { className: "pxp-note", role: "status" });
    const grid   = el("div", { className: "pxp-grid" });
    const capNote = el("p", {
        className: "pxp-note",
        textContent: `Showing the first ${SEARCH_CAP} matches. Narrow it with a grouping, or search for something more particular.`,
    });
    capNote.style.cssText = "display:none; margin-top:20px;";
    scroll.append(note, grid, capNote);

    // Footer
    const chosenEl = el("span", { className: "pxp-chosen" });
    const clearBtn = el("button", { type: "button", className: "pxp-link", textContent: "Clear" });
    const cancelBtn  = btn("Cancel", "Esc");
    const confirmBtn = btn("Use this picture", "↵", true);
    const foot = el("div", { className: "pxp-foot" });
    foot.append(chosenEl, clearBtn, el("span", { className: "pxp-spacer" }), cancelBtn, confirmBtn);

    const main = el("div", { className: "pxp-main" });
    main.append(head, scroll, foot);
    const body = el("div", { className: "pxp-body" });
    body.append(rail, main);
    modal.append(title, body);
    overlay.appendChild(modal);
    document.body.appendChild(overlay);
    searchInput.focus();

    // -----------------------------------------------------------------------
    // Helpers
    // -----------------------------------------------------------------------

    function updateCount() {
        const n = selectedIds.size;
        chosenEl.textContent = n ? `${n} chosen` : "None chosen";
        clearBtn.style.display = n ? "" : "none";
        confirmBtn.firstChild.textContent =
            n > 1 ? `Use these ${n} pictures` : n ? "Use this picture" : "Use no picture";
        // Nothing chosen is still a pick where the node had pictures: it
        // empties the node. Where it had none there is nothing to confirm.
        confirmBtn.disabled = !n && !initialIds.length;
    }

    function highlight(itemEl) {
        const on = selectedIds.has(itemEl._picId);
        itemEl.classList.toggle("pxp-cell--on", on);
        itemEl.ariaPressed = String(on);
        itemEl._tick.style.display = on ? "" : "none";
    }

    function close() {
        document.removeEventListener("keydown", onKeyDown, true);
        for (const item of itemElements) {
            if (item._objectUrl) URL.revokeObjectURL(item._objectUrl);
        }
        overlay.remove();
    }

    function confirm() {
        if (confirmBtn.disabled) return;
        const ids = Array.from(selectedIds);
        // With the IDs and never apart from them: IDs under another pick's
        // library would say they are that library's.
        if (libraryWidget) libraryWidget.value = ids.length ? (listed.library ?? "") : "";
        pictureIdsWidget.value = ids.join(",");
        if (typeof pictureIdsWidget.callback === "function") {
            pictureIdsWidget.callback(pictureIdsWidget.value);
        }
        close();
        updateNodePreviews(node, ids, credentials).catch(() => {});
    }

    function onKeyDown(e) {
        if (e.key === "Escape") {
            e.preventDefault();
            e.stopPropagation();
            close();
        } else if (e.key === "Enter") {
            // Enter in the search box searches; on another button or a
            // select it does what it does there; on a tile, or anywhere else,
            // it takes what is chosen. A tile's own Enter would be a plain
            // click, and throw away a ctrl-click selection just as it is taken.
            e.stopPropagation();
            const t = e.target;
            if (t === searchInput) {
                e.preventDefault();
                resetAndLoad();
            } else if (t.classList?.contains("pxp-cell") || (t.tagName !== "BUTTON" && t.tagName !== "SELECT")) {
                e.preventDefault();
                confirm();
            }
        }
    }

    // -----------------------------------------------------------------------
    // The rail
    // -----------------------------------------------------------------------

    function facetButton(kind, id, label, count) {
        const on = facet.kind === kind && facet.id === id;
        const b = el("button", { type: "button", className: `pxp-facet${on ? " pxp-facet--on" : ""}`, ariaPressed: String(on) });
        b.append(el("span", { className: "pxp-facet__label", textContent: label, title: label }));
        if (count != null) b.append(el("span", { className: "pxp-facet__count", textContent: grouped(count) }));
        b.addEventListener("click", () => {
            facet.kind = kind;
            facet.id = id;
            renderRail();
            resetAndLoad();
        });
        return b;
    }

    function renderRail() {
        const parts = [el("div", { className: "pxp-sec", textContent: "Project" })];
        parts.push(facetButton("", "", "Everything", totalCount));
        for (const p of groups.project) parts.push(facetButton("project", p.id, p.name, p.count));
        for (const [kind, label] of [["character", "Character"], ["set", "Picture set"]]) {
            const rows = groups[kind];
            if (!rows.length) continue;
            // A grouping chosen below the fold (a wired loader's) is shown, not hidden.
            const at = facet.kind === kind ? rows.findIndex(r => r.id === facet.id) : -1;
            const shown = expanded[kind] || at >= FACET_PREVIEW ? rows : rows.slice(0, FACET_PREVIEW);
            parts.push(el("div", { className: "pxp-sec", textContent: label }));
            for (const r of shown) parts.push(facetButton(kind, r.id, r.name, r.count));
            if (shown.length < rows.length) {
                const more = el("button", { type: "button", className: "pxp-more", textContent: `All ${rows.length} ›` });
                more.addEventListener("click", () => { expanded[kind] = true; renderRail(); });
                parts.push(more);
            }
        }
        rail.replaceChildren(...parts);
    }

    renderRail();

    // Projects 403 for a key scoped narrower than the library; the rail then
    // simply has none. Every list is optional: the grid works without them.
    const listOr = (path, params) =>
        proxyFetch(path, credentials, params).catch(err => {
            console.warn(`[PixlStash picker] ${path}:`, err.message);
            return [];
        });
    const charactersReq = listOr("/pixlstash/characters", { include_counts: "true" });
    Promise.all([
        listOr("/pixlstash/projects", { include_counts: "true" }),
        charactersReq,
        // Sets carry `picture_count` always; there is no `include_counts` to ask.
        listOr("/pixlstash/picture_sets"),
    ]).then(([projects, characters, sets]) => {
        groups = railGroups(projects, characters, sets);
        renderRail();
    });
    proxyFetch("/pixlstash/pictures/count", credentials)
        .then(body => {
            const n = Number(body?.count);
            totalCount = Number.isFinite(n) ? n : null;
            renderRail();
        })
        .catch(err => console.warn("[PixlStash picker] pictures/count:", err.message));

    // -----------------------------------------------------------------------
    // Sort mechanisms — fetched from proxy; fall back to sensible defaults
    // -----------------------------------------------------------------------

    const FALLBACK_SORTS = [
        { key: "IMPORTED_AT", label: "Date imported (newest)" },
        { key: "SCORE",       label: "Score (high → low)"     },
        { key: "RANDOM",      label: "Random"                  },
    ];

    function buildSortOptions(mechanisms) {
        // Build <option>s via the DOM (textContent), not innerHTML — the
        // labels/keys come from the server and must not be parsed as HTML.
        sortSel.replaceChildren(
            ...mechanisms
                .filter(m => (m.key ?? m.sort_key ?? m.name) !== "LIKENESS_GROUPS")
                .map(m => {
                    const k = m.key ?? m.sort_key ?? m.name ?? String(m);
                    const l = m.label ?? m.description ?? k;
                    return el("option", { value: String(k), textContent: String(l) });
                }),
        );
    }

    buildSortOptions(FALLBACK_SORTS);
    proxyFetch("/pixlstash/sort_mechanisms", credentials)
        .then(buildSortOptions)
        .catch(err => console.warn("[PixlStash picker] sort_mechanisms:", err.message));

    // The likeness sort's reference character. textContent, not innerHTML —
    // character names are server data.
    charactersReq.then(chars => likenessSel.replaceChildren(
        ...chars.map(c => el("option", { value: String(c.id), textContent: String(c.name ?? c.id) })),
    ));

    // -----------------------------------------------------------------------
    // Loading — a page of the list, or one answer from search
    // -----------------------------------------------------------------------

    // A load started before the grouping, search or sort changed must not
    // land in the grid the reader is now looking at.
    let loadSeq   = 0;
    let loading   = false;
    let offset    = 0;
    let exhausted = false;

    function scopeParams() {
        const p = {};
        if (facet.kind === "project")   p.project_id   = facet.id;
        if (facet.kind === "character") p.character_id = facet.id;
        if (facet.kind === "set")       p.set_id       = facet.id;
        return p;
    }

    function addTile(pic) {
        const itemIdx = itemElements.length;
        const name = String(pic.file_path ?? "").split(/[\\/]/).pop() || `Picture #${pic.id}`;
        const item = el("button", { type: "button", className: "pxp-cell", title: name, ariaLabel: name, textContent: `#${pic.id}` });
        item._picId     = pic.id;
        item._objectUrl = null;
        item._tick      = el("span", { className: "pxp-tick", textContent: "✓", ariaHidden: "true" });
        item.appendChild(item._tick);
        highlight(item);

        fetchThumbnailUrl(pic.id, credentials)
            .then(url => {
                if (!url) { item.firstChild.textContent = "Not available"; return; }
                item._objectUrl = url;
                item.firstChild.replaceWith(el("img", { src: url, alt: "" }));
            })
            .catch(() => {});

        // Click / ctrl-click / shift-click selection
        item.addEventListener("click", e => {
            if (e.shiftKey && lastClickedIdx >= 0) {
                const lo      = Math.min(lastClickedIdx, itemIdx);
                const hi      = Math.max(lastClickedIdx, itemIdx);
                const addMode = !selectedIds.has(pic.id);
                for (let j = lo; j <= hi; j++) {
                    const other = itemElements[j];
                    if (!other) continue;
                    if (addMode) selectedIds.add(other._picId);
                    else         selectedIds.delete(other._picId);
                    highlight(other);
                }
            } else if (e.ctrlKey || e.metaKey) {
                if (selectedIds.has(pic.id)) selectedIds.delete(pic.id);
                else                          selectedIds.add(pic.id);
                highlight(item);
                lastClickedIdx = itemIdx;
            } else {
                selectedIds.clear();
                for (const other of itemElements) highlight(other);
                selectedIds.add(pic.id);
                highlight(item);
                lastClickedIdx = itemIdx;
            }
            updateCount();
        });
        // Double-click: this one, and take it.
        item.addEventListener("dblclick", () => {
            selectedIds.clear();
            selectedIds.add(pic.id);
            updateCount();
            confirm();
        });

        itemElements.push(item);
        grid.appendChild(item);
    }

    async function search(text, seq) {
        const [found] = await Promise.all([
            proxyFetch("/pixlstash/pictures/search", credentials, {
                query: text, threshold: "0.1", limit: SEARCH_CAP + 1, ...scopeParams(),
            }),
            // Search does not name the library it answered from; a one-row
            // read of the list beside it does, so a pick made from search
            // results is vouched for exactly like one made from the list.
            // Where that read fails the search still stands, and its picks
            // are vouched for by nobody.
            proxyFetch("/pixlstash/pictures", credentials, { fields: "grid", limit: 1 }, listed)
                .catch(() => { listed.library = libraryOfPages(listed.library, ""); }),
        ]);
        if (seq !== loadSeq) return;
        const rows = Array.isArray(found) ? found : [];
        capNote.style.display = rows.length > SEARCH_CAP ? "" : "none";
        for (const pic of rows.slice(0, SEARCH_CAP)) addTile(pic);
        exhausted = true;
    }

    async function loadPage() {
        if (loading || exhausted) return;
        loading = true;
        const seq = loadSeq;
        const text = searchInput.value.trim();
        note.className = "pxp-note";
        note.textContent = itemElements.length ? "" : "Loading pictures…";

        try {
            if (text) {
                await search(text, seq);
            } else {
                const extra = {
                    fields:     "grid",
                    limit:      PAGE_SIZE,
                    offset,
                    sort:       sortSel.value,
                    descending: descendingSel.value,
                    ...scopeParams(),
                };
                if (sortSel.value === "CHARACTER_LIKENESS" && likenessSel.value) {
                    extra.reference_character_id = likenessSel.value;
                }
                const pictures = await proxyFetch("/pixlstash/pictures", credentials, extra, listed);
                if (seq !== loadSeq) return;
                if (!Array.isArray(pictures) || pictures.length < PAGE_SIZE) exhausted = true;
                offset += (pictures ?? []).length;
                for (const pic of pictures ?? []) addTile(pic);
            }
            if (seq !== loadSeq) return;
            note.textContent = itemElements.length ? "" :
                text ? "Nothing matches that. Try other words, or another grouping." :
                "No pictures here. Try another grouping, or search.";
        } catch (err) {
            if (seq !== loadSeq) return;
            note.className = "pxp-note pxp-note--error";
            note.textContent = `⚠ ${err.message}`;
            exhausted = true;
        } finally {
            if (seq === loadSeq) {
                loading = false;
                followUp();
            }
        }
    }

    function followUp() {
        // If we need to auto-scroll to a pre-selected item, find it now;
        // otherwise keep paging until it shows up or the list is exhausted.
        if (pendingScrollToId != null) {
            const target = itemElements.find(e => e._picId === pendingScrollToId);
            if (target) {
                pendingScrollToId = null;
                // Defer one frame so layout (thumbnail/img) is settled.
                requestAnimationFrame(() => target.scrollIntoView({ block: "center" }));
            } else if (!exhausted) {
                loadPage();
                return;
            } else {
                pendingScrollToId = null;
            }
        }
        // If the grid isn't scrollable yet and there are more pages, keep loading.
        if (!exhausted && scroll.scrollHeight <= scroll.clientHeight) loadPage();
    }

    function resetAndLoad() {
        loadSeq++;
        for (const item of itemElements) {
            if (item._objectUrl) URL.revokeObjectURL(item._objectUrl);
        }
        grid.replaceChildren();
        capNote.style.display = "none";
        itemElements.length = 0;
        offset    = 0;
        exhausted = false;
        loading   = false;
        lastClickedIdx = -1;
        pendingScrollToId = null; // re-load (e.g. after filter change) cancels auto-scroll
        // Search answers by relevance; the sort is the list's.
        const searching = Boolean(searchInput.value.trim());
        sortSel.disabled = descendingSel.disabled = likenessSel.disabled = searching;
        loadPage();
    }

    scroll.addEventListener("scroll", () => {
        if (scroll.scrollTop + scroll.clientHeight >= scroll.scrollHeight - 250) loadPage();
    });
    sortSel.addEventListener("change", () => {
        likenessSel.style.display = sortSel.value === "CHARACTER_LIKENESS" ? "" : "none";
        resetAndLoad();
    });
    descendingSel.addEventListener("change", resetAndLoad);
    likenessSel.addEventListener("change", resetAndLoad);
    // Clearing the box (its ✕, or deleting the text) goes back to the list.
    searchInput.addEventListener("input", () => { if (!searchInput.value) resetAndLoad(); });

    updateCount();
    loadPage();

    // -----------------------------------------------------------------------
    // Button handlers
    // -----------------------------------------------------------------------

    clearBtn.addEventListener("click", () => {
        selectedIds.clear();
        for (const item of itemElements) highlight(item);
        updateCount();
    });

    closeBtn.addEventListener("click",  close);
    cancelBtn.addEventListener("click", close);
    confirmBtn.addEventListener("click", confirm);
    overlay.addEventListener("click", e => { if (e.target === overlay) close(); });
    document.addEventListener("keydown", onKeyDown, true);
}

// DOM helpers live in ./modal_dom.js — shared with adapter_picker.js.

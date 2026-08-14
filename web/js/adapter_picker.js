/**
 * PixlStash adapter-picker modal.
 *
 * Exported API
 * ────────────
 * openAdapterPicker(adapterWidget, credentials, filters, onPicked)
 *
 *   adapterWidget — the hidden `adapter_sha256` widget written on confirm
 *   credentials   — { url, token, verifySsl }
 *   filters       — { kind, baseModel, characterId, setId }
 *   onPicked      — called with the chosen shelf record after confirm
 *
 * A sibling of picker.js rather than an extension of it: that one is
 * picture-shaped throughout (fields=grid, picture_id, likeness sorting).
 * What is shared is the modal chrome, the object-URL bookkeeping, and the
 * rule that every string off the server goes in via textContent.
 *
 * `GET /adapters` is unpaginated, so this fetches the filtered shelf once and
 * renders it in slices on scroll.  Only the icons of rendered cards are
 * fetched, which is the part that costs a request each.
 */

import { el, mkBtn, mkRow, selStyle } from "./modal_dom.js";

const PAGE_SIZE = 48;

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

async function fetchIconUrl(iconSha256, credentials) {
    const params = new URLSearchParams({
        url:        credentials.url,
        verify_ssl: credentials.verifySsl ? "true" : "false",
        icon_sha256: iconSha256,
    });
    const resp = await fetch(`/pixlstash/model_icon?${params}`, {
        headers: { "Authorization": `Bearer ${credentials.token}` },
    });
    if (!resp.ok) return null;
    return URL.createObjectURL(await resp.blob());
}

/**
 * Build the query for `GET /adapters`.
 *
 * character_id and set_id are mutually exclusive upstream (sending both is a
 * 400), so a wired character wins and the set is ignored — the same rule the
 * node's docstring records.
 */
function buildAdapterQuery({ kind, baseModel, characterId, setId } = {}) {
    const q = { file_kind: "adapter" };
    if (kind)      q.kind        = kind;
    if (baseModel) q.base_model  = baseModel;
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

export async function openAdapterPicker(adapterWidget, credentials, filters, onPicked) {
    let selectedSha = String(adapterWidget.value ?? "").trim() || null;
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

    const titleEl  = el("h2", { textContent: "PixlStash Adapters", style: "margin:0; font-size:1.05em; flex:1;" });
    const countEl  = el("span", { style: "font-size:.85em; color:#aaa;" });
    const closeBtn = mkBtn("✕");
    const header   = mkRow(titleEl, countEl, closeBtn);

    const searchInput = el("input", {
        type: "text",
        placeholder: "Search name, filename or trigger words…",
        style: selStyle() + "flex:1;",
    });
    const filterRow = mkRow(
        el("span", { textContent: describeFilters(filters), style: "color:#aaa; font-size:.85em; flex-shrink:0;" }),
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

    const confirmBtn = mkBtn("Use this adapter", "#2a7a2a");
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
        const sel = itemEl._sha === selectedSha;
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
        countEl.textContent = `${records.length} adapter${records.length === 1 ? "" : "s"}`;
    }

    function makeCard(record) {
        const item = el("div", {
            style: `
                cursor:pointer; background:#252525; border-radius:6px;
                padding:6px; display:flex; flex-direction:column; gap:4px;
            `,
        });
        item._sha       = record.sha256;
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
            textContent: record.display_name || record.filename || record.sha256.slice(0, 12),
            title:       record.filename || "",
            style:       "font-size:.8em; line-height:1.25; overflow-wrap:anywhere;",
        }));

        const meta = [record.base_model || "Base model not set", record.kind].filter(Boolean).join(" · ");
        item.appendChild(el("div", {
            textContent: meta,
            style:       "font-size:.72em; color:#999; overflow-wrap:anywhere;",
        }));

        if (!isPresent(record)) {
            // Still selectable — the node downloads and caches it — but say
            // plainly that the route this needs isn't in a released server
            // yet, rather than leaving the user to discover it at queue time.
            item.appendChild(el("div", {
                textContent: "no copy on disk — download not yet supported",
                title:       "PixlStash has no reachable copy of this file. "
                           + "Fetching it needs a server release that serves "
                           + "adapter bytes, which does not exist yet.",
                style:       "font-size:.68em; color:#d0a24c; line-height:1.2;",
            }));
        }

        if (record.icon_sha256) {
            // The grid may be rebuilt (or the modal closed) while this is in
            // flight. An icon that lands on a card no longer in itemElements
            // would never be revoked by close()/resetGrid(), so revoke it here
            // instead — that is the leak, and typing in the search box is the
            // way to hit it.
            const generation = gridGeneration;
            fetchIconUrl(record.icon_sha256, credentials)
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
            selectedSha    = record.sha256;
            selectedRecord = record;
            for (const other of itemElements) highlight(other);
        });
        item.addEventListener("dblclick", () => {
            selectedSha    = record.sha256;
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
            showNotice("No adapters match these filters.");
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
        const picked = selectedRecord ?? records.find(r => r.sha256 === selectedSha) ?? null;
        // Nothing new was chosen (the list failed to load, or the pre-seeded
        // value isn't in it) — close without touching the widget or the label,
        // rather than blanking a label whose value is still set.
        if (!picked) {
            close();
            return;
        }
        adapterWidget.value = picked.sha256;
        if (typeof adapterWidget.callback === "function") {
            adapterWidget.callback(adapterWidget.value);
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
        const data = await proxyFetch("/pixlstash/adapters", credentials, buildAdapterQuery(filters));
        all = Array.isArray(data?.adapters)
            ? data.adapters.filter(a => a && typeof a.sha256 === "string" && a.sha256)
            : [];
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

function describeFilters({ kind, baseModel, characterId, setId } = {}) {
    const parts = [];
    if (kind)        parts.push(kind);
    if (baseModel)   parts.push(baseModel);
    if (characterId) parts.push(`character #${characterId}`);
    else if (setId)  parts.push(`set #${setId}`);
    return parts.length ? `Filtered: ${parts.join(" · ")}` : "All adapters";
}

/** Two letters for the generated mark shown when a record has no icon. */
function initialsOf(record) {
    const name = String(record.display_name || record.filename || "?");
    const words = name.replace(/[_\-.]+/g, " ").split(/\s+/).filter(Boolean);
    return (words.slice(0, 2).map(w => w[0]).join("") || "?").toUpperCase();
}

// el / mkBtn / mkRow / selStyle come from ./modal_dom.js — see the import.

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
 * The modal fetches thumbnails from PixlStash directly (binary, not
 * JSON), paginates via infinite scroll, supports multi-select with
 * shift-click range, and writes confirmed IDs back into the widget.
 * Inline node previews are updated after confirm.
 */

// ---------------------------------------------------------------------------
// Proxy fetch helper  (JSON endpoints only — goes through ComfyUI proxy)
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

// ---------------------------------------------------------------------------
// Thumbnail helper  (binary WebP — fetched directly from PixlStash)
// ---------------------------------------------------------------------------

async function fetchThumbnailUrl(pictureId, credentials) {
    const resp = await fetch(
        `${credentials.url}/api/v1/pictures/thumbnails/${pictureId}.webp`,
        { headers: { "Authorization": `Bearer ${credentials.token}` } },
    );
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
// Main export
// ---------------------------------------------------------------------------

export async function openPicker(node, pictureIdsWidget, credentials, initialFilters) {
    const { projectId = "", setId = "", characterId = "" } = initialFilters ?? {};

    // Pre-seed selection from the widget's current value.
    const selectedIds = new Set(
        (pictureIdsWidget.value ?? "")
            .split(",")
            .map(s => Number(s.trim()))
            .filter(n => n > 0),
    );

    const itemElements = [];
    let lastClickedIdx = -1;

    // First pre-selected ID (lowest by file order in the widget value) — we
    // keep paging until we find it, then scroll it into view once.
    const initiallySelectedFirst = (pictureIdsWidget.value ?? "")
        .split(",")
        .map(s => Number(s.trim()))
        .find(n => n > 0) ?? null;
    let pendingScrollToId = initiallySelectedFirst;

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

    // Header
    const titleEl  = el("h2", { textContent: "PixlStash Browser", style: "margin:0; font-size:1.05em; flex:1;" });
    const selCount = el("span", { style: "font-size:.85em; color:#aaa;" });
    const clearBtn = mkBtn("Clear");
    const closeBtn = mkBtn("✕");
    const header   = mkRow(titleEl, selCount, clearBtn, closeBtn);

    // Filter / sort row
    const sortSel       = el("select", { style: selStyle() });
    const descendingSel = el("select", { style: selStyle() });
    descendingSel.innerHTML = `
        <option value="true">↓ Desc</option>
        <option value="false">↑ Asc</option>
    `;
    const likenessLabel = el("span", {
        textContent: "Likeness:",
        style: "color:#aaa; font-size:.85em; flex-shrink:0; display:none;",
    });
    const likenessSel = el("select", { style: selStyle() });
    likenessSel.style.display = "none";

    const filterRow = mkRow(
        el("span", { textContent: "Sort:", style: "color:#aaa; font-size:.85em; flex-shrink:0;" }),
        sortSel,
        descendingSel,
        likenessLabel,
        likenessSel,
    );

    // Thumbnail grid
    const grid = el("div", {
        style: `
            display:grid;
            grid-template-columns:repeat(auto-fill,minmax(128px,1fr));
            grid-auto-rows:auto;
            gap:8px; overflow-y:auto; flex:1; padding:4px;
            align-content:start;
        `,
    });

    // Footer
    const confirmBtn = mkBtn("Confirm selection", "#2a7a2a");
    const cancelBtn  = mkBtn("Cancel");
    const footer = el("div", { style: "display:flex; justify-content:flex-end; gap:10px; flex-shrink:0;" });
    footer.append(cancelBtn, confirmBtn);

    modal.append(header, filterRow, grid, footer);
    overlay.appendChild(modal);
    document.body.appendChild(overlay);

    // -----------------------------------------------------------------------
    // Helpers
    // -----------------------------------------------------------------------

    function updateCount() {
        selCount.textContent = `${selectedIds.size} selected`;
    }
    updateCount();

    function highlight(itemEl) {
        const sel   = selectedIds.has(itemEl._picId);
        const inner = itemEl._inner ?? itemEl;
        inner.style.outline       = sel ? "3px solid #4caf50" : "none";
        inner.style.outlineOffset = sel ? "2px" : "0";
    }

    function close() {
        document.removeEventListener("keydown", onKeyDown, true);
        for (const item of itemElements) {
            if (item._objectUrl) URL.revokeObjectURL(item._objectUrl);
        }
        overlay.remove();
    }

    function onKeyDown(e) {
        if (e.key === "Escape") {
            e.preventDefault();
            close();
        }
    }

    // -----------------------------------------------------------------------
    // Sort mechanisms — fetched from proxy; fall back to sensible defaults
    // -----------------------------------------------------------------------

    const FALLBACK_SORTS = [
        { key: "IMPORTED_AT", label: "Date imported (newest)" },
        { key: "SCORE",       label: "Score (high \u2192 low)"     },
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

    // Populate likeness character dropdown
    proxyFetch("/pixlstash/characters", credentials)
        .then(chars => {
            // textContent, not innerHTML — character names are server data.
            likenessSel.replaceChildren(
                ...chars.map(c =>
                    el("option", {
                        value:       String(c.id),
                        textContent: String(c.name ?? c.id),
                    }),
                ),
            );
        })
        .catch(err => console.warn("[PixlStash picker] characters:", err.message));

    // -----------------------------------------------------------------------
    // Paginated picture loading
    // -----------------------------------------------------------------------

    let loading   = false;
    let offset    = 0;
    let exhausted = false;
    const PAGE_SIZE = 48;

    async function loadPage() {
        if (loading || exhausted) return;
        loading = true;

        const extra = {
            fields:     "grid",
            limit:      PAGE_SIZE,
            offset,
            sort:       sortSel.value,
            descending: descendingSel.value,
        };
        if (projectId)   extra.project_id   = projectId;
        if (setId)       extra.set_id        = setId;
        if (characterId) extra.character_id  = characterId;
        if (sortSel.value === "CHARACTER_LIKENESS" && likenessSel.value) {
            extra.reference_character_id = likenessSel.value;
        }

        try {
            const pictures = await proxyFetch("/pixlstash/pictures", credentials, extra);

            if (!Array.isArray(pictures) || pictures.length < PAGE_SIZE) exhausted = true;
            offset += (pictures ?? []).length;

            for (const pic of pictures ?? []) {
                const itemIdx = itemElements.length;

                const item = el("div", { style: "position:relative; cursor:pointer;" });
                item._picId     = pic.id;
                item._idx       = itemIdx;
                item._objectUrl = null;

                // Padding-top:100% forces the outer box to be 1:1 (height = width).
                item.appendChild(el("div", { style: "padding-top:100%; display:block;" }));

                // Inner layer fills the padded area and clips content.
                const inner = el("div", {
                    style: `
                        position:absolute; inset:0; overflow:hidden;
                        border-radius:5px; background:#2a2a2a;
                    `,
                });
                item._inner = inner;
                inner.appendChild(el("div", {
                    textContent: `#${pic.id}`,
                    style: `
                        width:100%; height:100%; display:flex;
                        align-items:center; justify-content:center;
                        color:#555; font-size:.75em;
                    `,
                }));
                item.appendChild(inner);
                highlight(item);

                // Thumbnail — async
                fetchThumbnailUrl(pic.id, credentials)
                    .then(url => {
                        if (!url) return;
                        item._objectUrl = url;
                        const img = el("img", {
                            src:   url,
                            style: "width:100%; height:100%; object-fit:cover; display:block;",
                        });
                        inner.innerHTML = "";
                        inner.appendChild(img);
                        highlight(item);
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

                itemElements.push(item);
                grid.appendChild(item);
            }
        } catch (err) {
            grid.appendChild(el("div", {
                textContent: `⚠ ${err.message}`,
                style: "grid-column:1/-1; color:#f88; padding:12px; text-align:center; font-size:.9em;",
            }));
        } finally {
            loading = false;

            // If we need to auto-scroll to a pre-selected item, find it now;
            // otherwise keep paging until it shows up or the list is exhausted.
            if (pendingScrollToId != null) {
                const target = itemElements.find(e => e._picId === pendingScrollToId);
                if (target) {
                    pendingScrollToId = null;
                    // Defer one frame so layout (thumbnail/img) is settled.
                    requestAnimationFrame(() =>
                        target.scrollIntoView({ block: "center" }));
                } else if (!exhausted) {
                    loadPage();
                    return;
                } else {
                    pendingScrollToId = null;
                }
            }

            // If the grid isn't scrollable yet and there are more pages, keep loading.
            if (!exhausted && grid.scrollHeight <= grid.clientHeight) loadPage();
        }
    }

    function resetAndLoad() {
        grid.innerHTML = "";
        itemElements.length = 0;
        offset    = 0;
        exhausted = false;
        lastClickedIdx = -1;
        pendingScrollToId = null; // re-load (e.g. after filter change) cancels auto-scroll
        loadPage();
    }

    grid.addEventListener("scroll", () => {
        if (grid.scrollTop + grid.clientHeight >= grid.scrollHeight - 250) loadPage();
    });
    sortSel.addEventListener("change", () => {
        const isLikeness = sortSel.value === "CHARACTER_LIKENESS";
        likenessLabel.style.display = isLikeness ? "" : "none";
        likenessSel.style.display   = isLikeness ? "" : "none";
        resetAndLoad();
    });
    descendingSel.addEventListener("change", resetAndLoad);
    likenessSel.addEventListener("change", resetAndLoad);

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
    overlay.addEventListener("click", e => { if (e.target === overlay) close(); });
    document.addEventListener("keydown", onKeyDown, true);

    confirmBtn.addEventListener("click", async () => {
        const ids = Array.from(selectedIds);
        pictureIdsWidget.value = ids.join(",");
        if (typeof pictureIdsWidget.callback === "function") {
            pictureIdsWidget.callback(pictureIdsWidget.value);
        }
        close();
        await updateNodePreviews(node, ids, credentials).catch(() => {});
    });
}

// ---------------------------------------------------------------------------
// Tiny DOM helpers (local)
// ---------------------------------------------------------------------------

function el(tag, props = {}) {
    const node = document.createElement(tag);
    for (const [k, v] of Object.entries(props)) {
        if (k === "style") node.style.cssText = v;
        else               node[k] = v;
    }
    return node;
}

function mkBtn(label, bg = "#3a3a3a") {
    return el("button", {
        textContent: label,
        style: `
            padding:5px 14px; cursor:pointer; background:${bg};
            border:1px solid #555; border-radius:4px;
            color:#ddd; font-size:.85em; flex-shrink:0;
        `,
    });
}

function mkRow(...children) {
    const d = el("div", { style: "display:flex; align-items:center; gap:10px; flex-shrink:0;" });
    d.append(...children);
    return d;
}

function selStyle() {
    return "background:#2d2d2d; color:#ddd; border:1px solid #555; border-radius:4px; padding:4px 8px; font-size:.85em;";
}

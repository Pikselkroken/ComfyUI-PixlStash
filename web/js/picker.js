/**
 * ComfyUI-PixlStash — frontend extension
 *
 * Responsibilities:
 *  1. Register persistent ComfyUI settings for the PixlStash connection
 *     (URL, token, SSL toggle) so they never need to be typed into each node.
 *  2. Auto-populate those settings into newly created PixlStash nodes.
 *  3. Add a "Browse PixlStash…" button widget to the Image Loader node that
 *     opens a paginated, multi-select thumbnail picker modal.
 *  4. After selection, update the `picture_ids` widget and render inline
 *     thumbnail previews directly on the node.
 */

import { app } from "../../scripts/app.js";

// -----------------------------------------------------------------------
// Setting IDs
// -----------------------------------------------------------------------
const SETTING_URL   = "PixlStash.ServerURL";
const SETTING_TOKEN = "PixlStash.APIToken";
const SETTING_SSL   = "PixlStash.VerifySSL";

// Thumbnails per page in the picker grid
const PAGE_SIZE = 48;

// -----------------------------------------------------------------------
// API helpers (runs in the browser — cannot bypass CORS or SSL from here;
// the SSL setting only affects the Python node)
// -----------------------------------------------------------------------

function getConnectionSettings() {
    return {
        baseUrl: (app.ui.settings.getSettingValue(SETTING_URL) ?? "").replace(/\/$/, ""),
        token:   app.ui.settings.getSettingValue(SETTING_TOKEN) ?? "",
    };
}

/**
 * Perform an authenticated fetch against the PixlStash API.
 * Raises descriptive Error objects for all non-2xx responses.
 */
async function psRequest(method, path, options = {}) {
    const { baseUrl, token } = getConnectionSettings();
    if (!baseUrl) throw new Error("PixlStash: Server URL not configured in Settings › PixlStash.");
    if (!token)   throw new Error("PixlStash: API token not configured in Settings › PixlStash.");

    const url = baseUrl + path;
    const response = await fetch(url, {
        method,
        headers: {
            "Authorization": `Bearer ${token}`,
            ...options.headers,
        },
        ...options,
    });

    if (response.status === 401) throw new Error("PixlStash: Invalid or expired API token.");
    if (response.status === 403) throw new Error("PixlStash: Token does not have access to this resource.");
    if (response.status === 404) throw new Error("PixlStash: Resource not found.");
    if (!response.ok) throw new Error(`PixlStash: HTTP ${response.status} from ${url}`);

    return response;
}

/**
 * Fetch a thumbnail for *pictureId* and return an ephemeral object URL.
 * The caller is responsible for revoking it with URL.revokeObjectURL().
 */
async function fetchThumbnailObjectUrl(pictureId) {
    const resp = await psRequest("GET", `/pictures/thumbnails/${pictureId}.webp`);
    const blob = await resp.blob();
    return URL.createObjectURL(blob);
}

// -----------------------------------------------------------------------
// Picker modal
// -----------------------------------------------------------------------

/**
 * Open the PixlStash image browser in a modal overlay.
 *
 * @param {object}  node              — The LiteGraph node instance
 * @param {object}  pictureIdsWidget  — The `picture_ids` widget on the node
 */
async function openPicker(node, pictureIdsWidget) {
    // Parse the widget's current value into the initial selection set.
    const selectedIds = new Set(
        (pictureIdsWidget.value ?? "")
            .split(",")
            .map(s => s.trim())
            .filter(Boolean)
            .map(Number)
    );

    // Track items for shift-click range selection and object-URL cleanup.
    const itemElements = [];
    let lastClickedIdx = -1;

    // ---- Build DOM -------------------------------------------------------

    const overlay = el("div", {
        style: `
            position:fixed; inset:0; background:rgba(0,0,0,.75);
            display:flex; align-items:center; justify-content:center;
            z-index:10000; font-family:sans-serif;
        `,
    });

    const modal = el("div", {
        style: `
            background:#1e1e1e; border-radius:10px; padding:20px;
            width:85vw; max-width:1000px; height:75vh;
            display:flex; flex-direction:column; gap:12px;
            box-shadow:0 4px 32px rgba(0,0,0,.8); color:#e0e0e0;
        `,
    });

    // Header row
    const title    = el("h2", { textContent: "PixlStash Browser", style: "margin:0; font-size:1.1em; flex:1;" });
    const selCount = el("span", { style: "font-size:.9em; color:#aaa;" });
    const clearBtn = btn("Clear selection");
    const closeBtn = btn("✕");
    const header   = row(title, selCount, clearBtn, closeBtn);

    // Filter row
    const setSelect  = el("select", { style: selectStyle() + " flex:1;" });
    setSelect.innerHTML = '<option value="">All sets</option>';
    const sortSelect = el("select", { style: selectStyle() });
    const filterRow  = row(setSelect, sortSelect);

    // Thumbnail grid (scrollable)
    const grid = el("div", {
        style: `
            display:grid;
            grid-template-columns:repeat(auto-fill, minmax(120px,1fr));
            gap:8px; overflow-y:auto; flex:1; padding:4px;
            align-content:start;
        `,
    });

    // Footer row
    const confirmBtn = btn("Confirm selection", "#2a7a2a");
    const cancelBtn  = btn("Cancel");
    const footer     = el("div", {
        style: "display:flex; justify-content:flex-end; gap:10px; flex-shrink:0;",
    });
    footer.append(cancelBtn, confirmBtn);

    modal.append(header, filterRow, grid, footer);
    overlay.appendChild(modal);
    document.body.appendChild(overlay);

    // ---- Helpers ---------------------------------------------------------

    function updateSelCount() {
        selCount.textContent = `${selectedIds.size} selected`;
    }
    updateSelCount();

    function applySelection(itemEl) {
        if (selectedIds.has(itemEl._picId)) {
            itemEl.style.outline = "3px solid #4caf50";
            itemEl.style.outlineOffset = "-3px";
        } else {
            itemEl.style.outline = "none";
        }
    }

    function close() {
        for (const el of itemElements) {
            if (el._objectUrl) URL.revokeObjectURL(el._objectUrl);
        }
        overlay.remove();
    }

    // ---- Load picture sets -----------------------------------------------

    (async () => {
        try {
            const resp = await psRequest("GET", "/picture_sets");
            const sets = await resp.json();
            for (const s of sets) {
                const opt = document.createElement("option");
                opt.value = s.id;
                opt.textContent = `${s.name} (${s.picture_count})`;
                setSelect.appendChild(opt);
            }
        } catch (err) {
            console.warn("[PixlStash] Could not load picture sets:", err.message);
        }
    })();

    // ---- Load sort mechanisms --------------------------------------------

    const fallbackSorts = [
        { key: "score_desc",    label: "Score (high → low)" },
        { key: "imported_desc", label: "Date imported (newest first)" },
        { key: "random",        label: "Random" },
    ];
    sortSelect.innerHTML = fallbackSorts
        .map(s => `<option value="${s.key}">${s.label}</option>`)
        .join("");

    (async () => {
        try {
            const resp = await psRequest("GET", "/sort_mechanisms");
            const mechanisms = await resp.json();
            sortSelect.innerHTML = mechanisms
                .map(m => {
                    const k = m.key ?? m.sort_key ?? m.name ?? String(m);
                    const l = m.label ?? m.description ?? k;
                    return `<option value="${k}">${l}</option>`;
                })
                .join("");
        } catch (err) {
            // Keep the fallback options; dynamic load is best-effort.
            console.warn("[PixlStash] Could not load sort mechanisms:", err.message);
        }
    })();

    // ---- Paginated picture loading ---------------------------------------

    let loading    = false;
    let offset     = 0;
    let exhausted  = false;

    async function loadPage() {
        if (loading || exhausted) return;
        loading = true;

        const setId = setSelect.value;
        const sort  = sortSelect.value;

        const params = new URLSearchParams({ fields: "grid", limit: PAGE_SIZE, offset });
        if (setId) params.set("set_id", setId);

        // Map combo values to API parameters
        if (sort === "score_desc") {
            params.set("sort", "score");
            params.set("descending", "true");
        } else if (sort === "imported_desc") {
            params.set("sort", "imported_at");
            params.set("descending", "true");
        } else if (sort === "random") {
            params.set("sort", "random");
        } else if (sort) {
            params.set("sort", sort);
        }

        try {
            const resp     = await psRequest("GET", `/pictures?${params}`);
            const pictures = await resp.json();

            if (pictures.length < PAGE_SIZE) exhausted = true;
            offset += pictures.length;

            for (const pic of pictures) {
                const itemIdx = itemElements.length;

                const item = el("div", {
                    style: `
                        position:relative; cursor:pointer; border-radius:5px;
                        overflow:hidden; aspect-ratio:1; background:#2a2a2a;
                    `,
                });
                item._picId     = pic.id;
                item._idx       = itemIdx;
                item._objectUrl = null;

                // Placeholder while thumbnail loads
                const placeholder = el("div", {
                    textContent: `#${pic.id}`,
                    style: `
                        width:100%; height:100%; display:flex;
                        align-items:center; justify-content:center;
                        color:#666; font-size:.75em;
                    `,
                });
                item.appendChild(placeholder);
                applySelection(item);

                // Load thumbnail asynchronously
                fetchThumbnailObjectUrl(pic.id)
                    .then(url => {
                        item._objectUrl = url;
                        const img = el("img", {
                            src: url,
                            style: "width:100%; height:100%; object-fit:cover; display:block;",
                        });
                        item.innerHTML = "";
                        item.appendChild(img);
                        applySelection(item);
                    })
                    .catch(() => {
                        placeholder.textContent = `#${pic.id}`;
                    });

                // Click / shift-click selection
                item.addEventListener("click", e => {
                    if (e.shiftKey && lastClickedIdx >= 0) {
                        const lo       = Math.min(lastClickedIdx, itemIdx);
                        const hi       = Math.max(lastClickedIdx, itemIdx);
                        const addMode  = !selectedIds.has(pic.id);
                        for (let j = lo; j <= hi; j++) {
                            const el = itemElements[j];
                            if (!el) continue;
                            if (addMode) selectedIds.add(el._picId);
                            else         selectedIds.delete(el._picId);
                            applySelection(el);
                        }
                    } else {
                        if (selectedIds.has(pic.id)) selectedIds.delete(pic.id);
                        else                          selectedIds.add(pic.id);
                        applySelection(item);
                        lastClickedIdx = itemIdx;
                    }
                    updateSelCount();
                });

                itemElements.push(item);
                grid.appendChild(item);
            }
        } catch (err) {
            const errEl = el("div", {
                textContent: err.message,
                style: "grid-column:1/-1; color:#f88; padding:12px; text-align:center;",
            });
            grid.appendChild(errEl);
        } finally {
            loading = false;
        }
    }

    // Infinite scroll: load next page when near the bottom of the grid
    grid.addEventListener("scroll", () => {
        if (grid.scrollTop + grid.clientHeight >= grid.scrollHeight - 200) {
            loadPage();
        }
    });

    function resetAndLoad() {
        grid.innerHTML  = "";
        itemElements.length = 0;
        offset    = 0;
        exhausted = false;
        lastClickedIdx = -1;
        loadPage();
    }

    setSelect.addEventListener("change", resetAndLoad);
    sortSelect.addEventListener("change", resetAndLoad);

    loadPage();

    // ---- Button handlers -------------------------------------------------

    clearBtn.addEventListener("click", () => {
        selectedIds.clear();
        for (const el of itemElements) applySelection(el);
        updateSelCount();
    });

    closeBtn.addEventListener("click", close);
    cancelBtn.addEventListener("click", close);
    overlay.addEventListener("click", e => { if (e.target === overlay) close(); });

    confirmBtn.addEventListener("click", async () => {
        const ids = Array.from(selectedIds);
        pictureIdsWidget.value = ids.join(",");
        if (typeof pictureIdsWidget.callback === "function") {
            pictureIdsWidget.callback(pictureIdsWidget.value);
        }
        await updateNodePreviews(node, ids);
        close();
    });
}

// -----------------------------------------------------------------------
// Inline node thumbnail preview
// -----------------------------------------------------------------------

/**
 * Load thumbnails for *pictureIds* and store them on the node so that
 * LiteGraph renders them as inline image previews.
 */
async function updateNodePreviews(node, pictureIds) {
    // Revoke any previously allocated object URLs.
    if (node._pixlstashUrls) {
        for (const u of node._pixlstashUrls) URL.revokeObjectURL(u);
    }
    node._pixlstashUrls = [];

    if (!pictureIds.length) {
        node.imgs = null;
        node.setSizeForImage?.();
        return;
    }

    try {
        const urls = await Promise.all(pictureIds.map(id => fetchThumbnailObjectUrl(id)));
        node._pixlstashUrls = urls;

        node.imgs = urls.map(url => {
            const img = new Image();
            img.src = url;
            return img;
        });
        node.setSizeForImage?.();
        app.graph?.setDirtyCanvas(true);
    } catch (err) {
        console.warn("[PixlStash] Could not load node preview thumbnails:", err.message);
    }
}

// -----------------------------------------------------------------------
// Auto-fill connection settings into a newly created node
// -----------------------------------------------------------------------

function autoFillSettings(node) {
    const { baseUrl, token } = getConnectionSettings();
    const sslEnabled = app.ui.settings.getSettingValue(SETTING_SSL, true);

    for (const w of node.widgets ?? []) {
        if (w.name === "pixlstash_url" && !w.value) w.value = baseUrl;
        if (w.name === "api_token"     && !w.value) w.value = token;
        if (w.name === "verify_ssl")                w.value = sslEnabled;
    }
}

// -----------------------------------------------------------------------
// Extension registration
// -----------------------------------------------------------------------

app.registerExtension({
    name: "ComfyUI.PixlStash",

    async setup() {
        app.ui.settings.addSetting({
            id:           SETTING_URL,
            name:         "PixlStash: Server URL",
            type:         "text",
            defaultValue: "https://localhost:8000",
            tooltip:      "Base URL of your PixlStash instance, e.g. https://192.168.1.10:8000",
        });

        app.ui.settings.addSetting({
            id:           SETTING_TOKEN,
            name:         "PixlStash: API Token",
            type:         "text",
            defaultValue: "",
            tooltip:      "Bearer token for authentication. Stored in ComfyUI settings — never saved to workflow JSON.",
        });

        app.ui.settings.addSetting({
            id:           SETTING_SSL,
            name:         "PixlStash: Verify SSL",
            type:         "boolean",
            defaultValue: true,
            tooltip:      "Disable to accept self-signed certificates (Python-side only; a console warning will appear).",
        });
    },

    async beforeRegisterNodeDef(nodeType, nodeData) {
        if (nodeData.name === "PixlStashImageLoader") {
            const orig = nodeType.prototype.onNodeCreated;
            nodeType.prototype.onNodeCreated = function () {
                orig?.apply(this, arguments);
                autoFillSettings(this);

                // Add the "Browse PixlStash…" button widget.
                // serialize:false ensures the button itself is not saved to the workflow.
                this.addWidget("button", "Browse PixlStash\u2026", null, () => {
                    const sslEnabled = app.ui.settings.getSettingValue(SETTING_SSL, true);
                    if (!sslEnabled) {
                        console.warn(
                            "[PixlStash] SSL verification is DISABLED. " +
                            "The Python node will accept self-signed certificates."
                        );
                    }
                    const pictureIdsWidget = this.widgets?.find(w => w.name === "picture_ids");
                    if (!pictureIdsWidget) {
                        alert("PixlStash: could not find the picture_ids widget on this node.");
                        return;
                    }
                    openPicker(this, pictureIdsWidget).catch(err => alert(err.message));
                }, { serialize: false });
            };
        }

        if (nodeData.name === "PixlStashImageSaver") {
            const orig = nodeType.prototype.onNodeCreated;
            nodeType.prototype.onNodeCreated = function () {
                orig?.apply(this, arguments);
                autoFillSettings(this);
            };
        }
    },
});

// -----------------------------------------------------------------------
// Tiny DOM helpers  (keep the modal code readable without a framework)
// -----------------------------------------------------------------------

function el(tag, props = {}) {
    const node = document.createElement(tag);
    for (const [k, v] of Object.entries(props)) {
        if (k === "style") node.style.cssText = v;
        else               node[k] = v;
    }
    return node;
}

function btn(label, bg = "#444") {
    return el("button", {
        textContent: label,
        style: `
            padding:6px 14px; cursor:pointer; background:${bg};
            border:none; border-radius:4px; color:#e0e0e0; font-size:.9em;
        `,
    });
}

function row(...children) {
    const d = el("div", {
        style: "display:flex; align-items:center; gap:10px; flex-shrink:0;",
    });
    d.append(...children);
    return d;
}

function selectStyle() {
    return "background:#2d2d2d; color:#ddd; border:1px solid #555; border-radius:4px; padding:5px 8px;";
}

/**
 * ComfyUI-PixlStash — frontend extension
 *
 * Design
 * ──────
 * The Project / Set / Character pickers are declared on the Python side as
 * standard COMBO inputs with a placeholder value.  After each node is
 * created, we replace `widget.options.values` with a getter that returns a
 * cached list fetched from the PixlStash server.  This way ComfyUI's stock
 * combo widget handles all the UI (search popup, theming, sizing) and we
 * only worry about data.
 *
 * Saved values use the format `"<name> #<id>"` so the human-readable name
 * is shown in the dropdown and the numeric ID can be recovered server-side
 * by a simple regex (see ``nodes/*_loader.py``).
 */

import { app } from "../../scripts/app.js";
import { openPicker, updateNodePreviews } from "./picker.js";

// ---------------------------------------------------------------------------
// Setting IDs
// ---------------------------------------------------------------------------

const S_URL   = "PixlStash.ServerURL";
const S_TOKEN = "PixlStash.APIToken";
const S_SSL   = "PixlStash.VerifySSL";

// Widget height helper (falls back gracefully if LiteGraph isn't loaded yet)
// ---------------------------------------------------------------------------
// Version checking
// ---------------------------------------------------------------------------

/** Minimum required PixlStash server version per node type. */
const NODE_MIN_VERSION = {
    "PixlStashProjectLoader":   "1.2.0",
    "PixlStashCharacterLoader": "1.2.0",
    "PixlStashSetLoader":       "1.2.0",
    "PixlStashPictureLoader":   "1.2.0",
    "PixlStashPictureSaver":    "1.2.0",
    "PixlStashSemanticSearch":  "1.2.0",
    "PixlStashLikenessSearch":  "1.4.0",
};

// Cache: serverUrl → { state: "checking"|"resolved"|"error", version: string|null }
const _versionCache = new Map();

/** Strip pre-release suffixes and return [major, minor, patch]. */
function _parseBaseVersion(str) {
    const m = (str ?? "").match(/^(\d+)\.(\d+)\.(\d+)/);
    return m ? [+m[1], +m[2], +m[3]] : null;
}

/**
 * Returns true when serverVer ≥ requiredVer (comparing only major.minor.patch).
 * Dev / RC tags on serverVer are stripped, treating them as the base release.
 * e.g. "1.4.0.dev2" and "1.4.0rc1" both satisfy a "1.4.0" requirement.
 */
function _versionSatisfies(serverVer, requiredVer) {
    const sv = _parseBaseVersion(serverVer);
    const rv = _parseBaseVersion(requiredVer);
    if (!sv || !rv) return true; // unparseable → don't block
    for (let i = 0; i < 3; i++) {
        if (sv[i] > rv[i]) return true;
        if (sv[i] < rv[i]) return false;
    }
    return true; // equal
}

async function _fetchPixlStashVersion(creds) {
    const params = new URLSearchParams({
        url:        creds.url,
        verify_ssl: creds.verifySsl ? "true" : "false",
    });
    const resp = await fetch(`/pixlstash/version?${params}`, {
        headers: { "Authorization": `Bearer ${creds.token}` },
    });
    if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
    const data = await resp.json();
    // Accept both {"version":"1.4.0"} and plain "1.4.0"
    const version = typeof data === "string" ? data : (data.version ?? null);
    // Sanity-check: must start with digits (semver), not HTML or an error string.
    if (!version || !/^\d+\.\d+/.test(version)) throw new Error(`Unexpected version: ${String(version).slice(0, 40)}`);
    return version;
}

/**
 * Return the current version-compatibility state for a node type.
 * Triggers an async fetch on the first call per server URL; subsequent calls
 * return the cached result.  `onRedraw` is called once when the fetch resolves
 * so callers can trigger a canvas refresh.
 *
 * While checking or on error, returns { ok: true } (don't block normal UI).
 */
function _getVersionState(nodeTypeName, onRedraw) {
    const required = NODE_MIN_VERSION[nodeTypeName];
    if (!required) return { ok: true };

    const creds = getSettingsCredentials();
    if (!creds.url || !creds.token) return { ok: true }; // no credentials → don't block

    const key = creds.url;
    if (!_versionCache.has(key)) {
        _versionCache.set(key, { state: "checking", version: null });
        _fetchPixlStashVersion(creds)
            .then(version => {
                _versionCache.set(key, { state: "resolved", version });
                onRedraw?.();
            })
            .catch(() => {
                _versionCache.set(key, { state: "error", version: null });
            });
    }

    const entry = _versionCache.get(key);
    if (entry.state !== "resolved") return { ok: true }; // still fetching or error

    return {
        ok:       _versionSatisfies(entry.version, required),
        required,
        found:    entry.version,
    };
}

/**
 * Draw a "version too old" banner covering the node body.
 * Returns true if the banner was drawn (node is incompatible), false otherwise.
 * Coordinates are in LiteGraph node-local space (origin = body top-left).
 */
function _drawVersionBanner(ctx, node, nodeTypeName) {
    const vs = _getVersionState(nodeTypeName, () => {
        node.setDirtyCanvas?.(true, true);
        app.graph?.setDirtyCanvas?.(true, true);
    });
    if (vs.ok) return false;

    const W = node.size[0];
    const H = node.size[1];

    ctx.save();
    ctx.fillStyle = "#5c0000";
    ctx.beginPath();
    ctx.roundRect(0, 0, W, H, 4);
    ctx.fill();

    ctx.fillStyle    = "#ffbbbb";
    ctx.font         = "bold 11px Arial";
    ctx.textAlign    = "center";
    ctx.textBaseline = "middle";
    ctx.fillText(`PixlStash ${vs.required} required`, W / 2, H / 2 - 9);
    ctx.fillText(`but ${vs.found ?? "unknown"} found`,  W / 2, H / 2 + 9);

    ctx.restore();
    return true;
}

/**
 * Draw a small "hostname  vX.Y.Z" label in the bottom-right corner of the node.
 * Only shown when the server version has been successfully resolved.
 * Skip drawing when the incompatibility banner already covers the node.
 */
function _drawServerInfo(ctx, node, nodeTypeName) {
    // Piggy-back on _getVersionState to trigger the async fetch if not yet done.
    const creds = getSettingsCredentials();
    if (!creds.url) return;

    const entry = _versionCache.get(creds.url);
    if (!entry || entry.state !== "resolved" || !entry.version) return;

    let hostname;
    try { hostname = new URL(creds.url).hostname; }
    catch { hostname = creds.url; }

    const text = `${hostname}  v${entry.version}`;
    const W = node.size[0];
    const H = node.size[1];
    const PAD = 4;

    ctx.save();
    ctx.font         = "9px Arial";
    ctx.fillStyle    = "rgba(255,255,255,0.35)";
    ctx.textAlign    = "right";
    ctx.textBaseline = "bottom";
    ctx.fillText(text, W - PAD, H - PAD);
    ctx.restore();
}

// ---------------------------------------------------------------------------
// Credential helper — reads from ComfyUI Settings at call time
// ---------------------------------------------------------------------------

function getSettingsCredentials() {
    return {
        url:       (app.ui.settings.getSettingValue(S_URL,   "") ?? "").trim(),
        token:     (app.ui.settings.getSettingValue(S_TOKEN, "") ?? "").trim(),
        verifySsl:  app.ui.settings.getSettingValue(S_SSL,   true) ?? true,
    };
}

/**
 * Extract the numeric ID from a `"<name> #<id>"` combo selection,
 * or return "" for the placeholder / "— None —" sentinels.
 */
function extractId(value) {
    const m = String(value ?? "").match(/#(\d+)\s*$/);
    return m ? m[1] : "";
}

/**
 * Follow the link on `inputName` to the origin node and read the widget
 * value for that output slot, returning the numeric ID portion only.
 */
function getWiredValue(node, inputName) {
    const slotIdx = node.inputs?.findIndex(i => i.name === inputName);
    if (slotIdx < 0) return "";

    const linkId = node.inputs[slotIdx].link;
    if (linkId == null) return "";

    const link = app.graph.links[linkId];
    if (!link) return "";

    const origin = app.graph.getNodeById(link.origin_id);
    if (!origin) return "";

    const outputName = origin.outputs?.[link.origin_slot]?.name;
    const w = origin.widgets?.find(w => w.name === outputName);
    return extractId(w?.value);
}

/**
 * Find the project_id selected in an upstream Project Loader.
 * Checks for an explicit project_id wire into this node.
 */
function findUpstreamProjectId(node) {
    return getWiredValue(node, "pixlstash_project");
}

// ---------------------------------------------------------------------------
// Proxy call helper
// ---------------------------------------------------------------------------

async function proxyFetch(path, credentials, extraParams = {}) {
    const { url, token, verifySsl } = credentials;
    if (!url || !token) {
        throw new Error("Configure URL and API Token in ComfyUI Settings \u203a PixlStash.");
    }
    const params = new URLSearchParams({
        url,
        verify_ssl: verifySsl ? "true" : "false",
        ...extraParams,
    });
    const resp = await fetch(`${path}?${params}`, {
        headers: { "Authorization": `Bearer ${token}` },
    });
    if (!resp.ok) {
        const body = await resp.json().catch(() => ({}));
        throw new Error(body.error || `HTTP ${resp.status}`);
    }
    return resp.json();
}

// ---------------------------------------------------------------------------
// Combo value format helpers
// ---------------------------------------------------------------------------
//
// Selected values are stored as `"<name> #<id>"` so the human-readable name
// stays visible while Python can extract the ID server-side.

const NONE_LABEL    = "— None —";
const LOADING_LABEL = "(loading…)";

const fmt = (id, name) => `${name ?? id} #${id}`;

// ---------------------------------------------------------------------------
// Fetch functions for each picker type — return arrays of formatted strings
// ---------------------------------------------------------------------------

async function fetchProjectOptions() {
    const data = await proxyFetch("/pixlstash/projects", getSettingsCredentials());
    return [NONE_LABEL, ...data.map(p => fmt(p.id, p.name))];
}

async function fetchSetOptions(projectId) {
    const extra = projectId ? { project_id: projectId } : {};
    const data  = await proxyFetch("/pixlstash/picture_sets", getSettingsCredentials(), extra);
    return [
        NONE_LABEL,
        ...data
            .filter(s => !s.reference_character)
            .map(s => fmt(s.id, `${s.name ?? s.id} (${s.picture_count ?? "?"})`)),
    ];
}

async function fetchCharacterOptions(projectId) {
    const extra = projectId ? { project_id: projectId } : {};
    const data  = await proxyFetch("/pixlstash/characters", getSettingsCredentials(), extra);
    return [NONE_LABEL, ...data.map(c => fmt(c.id, c.name))];
}

// ---------------------------------------------------------------------------
// Cache + dynamic options binding
// ---------------------------------------------------------------------------
//
// One cache per (kind, projectId) tuple.  An entry has `items` (the list
// returned to the combo widget) and `state` ('loading' | 'ready' | 'error').
// The first read of `widget.options.values` kicks off the fetch; subsequent
// reads return whatever is cached so the combobox can render synchronously.

const _optsCache = new Map();

function _cacheKey(kind, projectId) {
    return `${kind}|${projectId ?? ""}`;
}

function _kickOffFetch(kind, projectId, node, widget) {
    const key = _cacheKey(kind, projectId);
    const entry = { items: [LOADING_LABEL], state: "loading" };
    _optsCache.set(key, entry);

    const fetcher =
        kind === "projects"   ? fetchProjectOptions()
      : kind === "sets"       ? fetchSetOptions(projectId)
      : /* characters */        fetchCharacterOptions(projectId);

    fetcher
        .then(items => {
            entry.items = items;
            entry.state = "ready";
            node.setDirtyCanvas?.(true, true);
        })
        .catch(err => {
            entry.items = [`⚠ ${err.message}`];
            entry.state = "error";
            node.setDirtyCanvas?.(true, true);
        });
}

/** Drop all cached entries for a kind so the next read re-fetches. */
function _invalidateKind(kind) {
    for (const k of [..._optsCache.keys()]) {
        if (k.startsWith(`${kind}|`)) _optsCache.delete(k);
    }
}

/**
 * Replace `widget.options.values` with a getter returning cached items.
 * `getProjectId` is called on each read so the set/character lists track
 * the current upstream project selection.
 */
function bindDynamicValues(node, widget, kind, getProjectId) {
    Object.defineProperty(widget.options, "values", {
        configurable: true,
        get() {
            const pid = getProjectId();
            const key = _cacheKey(kind, pid);
            let entry = _optsCache.get(key);
            if (!entry) {
                _kickOffFetch(kind, pid, node, widget);
                entry = _optsCache.get(key);
            }
            // Make sure the currently-selected value is in the list so the
            // combo widget displays it even after a workflow reload (before
            // the fetch finishes the cache may not contain it yet).
            const v = widget.value;
            if (v && v !== LOADING_LABEL && !entry.items.includes(v)) {
                return [v, ...entry.items];
            }
            return entry.items;
        },
        set() { /* ignore writes — list is owned by the cache */ },
    });
}

/**
 * Reset all PixlStash combo widgets on downstream nodes to “— None —”
 * and invalidate the sets/characters caches so they re-fetch with the
 * new project filter.
 */
function resetDownstreamFilters(node) {
    _invalidateKind("sets");
    _invalidateKind("characters");

    const visited = new Set();
    const walk = (n) => {
        if (visited.has(n.id)) return;
        visited.add(n.id);
        for (const output of n.outputs ?? []) {
            for (const linkId of output.links ?? []) {
                const link = app.graph.links[linkId];
                if (!link) continue;
                const target = app.graph.getNodeById(link.target_id);
                if (!target) continue;
                let dirty = false;
                for (const w of target.widgets ?? []) {
                    if (w.name === "pixlstash_set" || w.name === "pixlstash_character") {
                        w.value = NONE_LABEL;
                        dirty = true;
                    }
                }
                if (dirty) target.setDirtyCanvas(true, true);
                walk(target);
            }
        }
    };
    walk(node);
}

// ---------------------------------------------------------------------------
// Extension
// ---------------------------------------------------------------------------

app.registerExtension({
    name: "ComfyUI.PixlStash",

    // ------------------------------------------------------------------
    // 1. Register ComfyUI settings
    // ------------------------------------------------------------------
    async setup() {
        app.ui.settings.addSetting({
            id:           S_URL,
            name:         "PixlStash: Server URL",
            type:         "text",
            defaultValue: "http://localhost:8000",
            tooltip:      "Base URL of your PixlStash instance.",
        });
        app.ui.settings.addSetting({
            id:           S_TOKEN,
            name:         "PixlStash: API Token",
            type:         "text",
            defaultValue: "",
            tooltip:      "Bearer token — stored here, never in workflow JSON.",
        });
        app.ui.settings.addSetting({
            id:           S_SSL,
            name:         "PixlStash: Verify SSL",
            type:         "boolean",
            defaultValue: true,
            tooltip:      "Disable to accept self-signed certificates.",
        });
        // Credentials (URL / token / Verify SSL) live only in these ComfyUI
        // settings.  ComfyUI persists them server-side, so the PixlStash nodes
        // read them directly at execution time (see connection.read_credentials).
        // Nothing is injected into the prompt or saved into workflow JSON.
    },

    // ------------------------------------------------------------------
    // 2. Per-node customisation
    // ------------------------------------------------------------------
    async beforeRegisterNodeDef(nodeType, nodeData) {

        // ============================================================
        // Version banner — overlay all PixlStash nodes when server is
        // older than the minimum required version for that node type.
        // ============================================================
        if (nodeData.name in NODE_MIN_VERSION) {
            const _nodeTypeName = nodeData.name;
            const _origFG = nodeType.prototype.onDrawForeground;
            nodeType.prototype.onDrawForeground = function (ctx) {
                _origFG?.call(this, ctx);
                if (!_drawVersionBanner(ctx, this, _nodeTypeName)) {
                    _drawServerInfo(ctx, this, _nodeTypeName);
                }
            };
        }

        // ============================================================
        // Project Loader — dynamic project list + cascade reset
        // ============================================================
        if (nodeData.name === "PixlStashProjectLoader") {
            const orig = nodeType.prototype.onNodeCreated;
            nodeType.prototype.onNodeCreated = function () {
                orig?.call(this);
                const w = this.widgets?.find(x => x.name === "pixlstash_project");
                if (!w) return;
                bindDynamicValues(this, w, "projects", () => null);
                const prevCb = w.callback;
                w.callback = (...args) => {
                    prevCb?.(...args);
                    resetDownstreamFilters(this);
                };
            };
        }

        // ============================================================
        // Set Loader — dynamic set list, filtered by upstream project
        // ============================================================
        if (nodeData.name === "PixlStashSetLoader") {
            const orig = nodeType.prototype.onNodeCreated;
            nodeType.prototype.onNodeCreated = function () {
                orig?.call(this);
                const w = this.widgets?.find(x => x.name === "pixlstash_set");
                if (!w) return;
                bindDynamicValues(this, w, "sets",
                    () => findUpstreamProjectId(this) || null);
            };
        }

        // ============================================================
        // Character Loader — dynamic character list, filtered by upstream project
        // ============================================================
        if (nodeData.name === "PixlStashCharacterLoader") {
            const orig = nodeType.prototype.onNodeCreated;
            nodeType.prototype.onNodeCreated = function () {
                orig?.call(this);
                const w = this.widgets?.find(x => x.name === "pixlstash_character");
                if (!w) return;
                bindDynamicValues(this, w, "characters",
                    () => findUpstreamProjectId(this) || null);
            };
        }

        // ============================================================
        // PixlStash Picture Loader  — Browse button + hide credential widgets
        // ============================================================
        if (nodeData.name === "PixlStashPictureLoader") {
            const orig = nodeType.prototype.onNodeCreated;
            nodeType.prototype.onNodeCreated = function () {
                orig?.call(this);

                const picIdsWidget = this.widgets?.find(w => w.name === "picture_ids");
                if (!picIdsWidget) return;

                // Re-load inline previews when a saved workflow is restored.
                const prevConfigure = this.onConfigure;
                this.onConfigure = function (data) {
                    prevConfigure?.call(this, data);
                    const ids = (picIdsWidget.value ?? "")
                        .split(",")
                        .map(s => Number(s.trim()))
                        .filter(n => n > 0);

                    const creds = getSettingsCredentials();
                    if (creds.url && creds.token) {
                        updateNodePreviews(this, ids, creds).catch(() => {});
                    }
                };

                // Hide the raw text widget \u2014 values are managed by the Browse button.
                picIdsWidget.hidden = true;
                picIdsWidget.computeSize = () => [0, -4];

                this.addWidget(
                    "button",
                    "Browse PixlStash\u2026",
                    null,
                    () => {
                        const creds = getSettingsCredentials();
                        if (!creds.url || !creds.token) {
                            alert("PixlStash: configure URL and API Token in ComfyUI Settings \u203a PixlStash first.");
                            return;
                        }
                        const filters = {
                            projectId:   getWiredValue(this, "pixlstash_project"),
                            setId:       getWiredValue(this, "pixlstash_set"),
                            characterId: getWiredValue(this, "pixlstash_character"),
                        };
                        openPicker(this, picIdsWidget, creds, filters)
                            .catch(err => alert(`PixlStash picker error: ${err.message}`));
                    },
                    { serialize: false },
                );
            };
        }

    },
});

/**
 * ComfyUI-PixlStash — frontend extension
 *
 * Design
 * ──────
 * The Project / Set / Character / Sort picker widgets are registered as
 * *custom widget types* via `getCustomWidgets`.  This is the correct
 * ComfyUI hook for creating widgets whose type names aren't built-in
 * (STRING / INT / COMBO etc.).  Using it means:
 *
 *   • ComfyUI never creates a DOM <textarea> for these inputs.
 *   • `toConcreteWidget()` in the new Vue-based frontend sees an
 *     unknown type and falls through to calling `widget.mouse`,
 *     which is our custom handler.
 *   • The widget value is serialised / deserialised normally.
 *
 * The Connector node and the Picture Loader Browse button are handled
 * separately in `beforeRegisterNodeDef` because they modify
 * *existing* built-in widgets, not create new ones.
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
const WH = () => (typeof LiteGraph !== "undefined" ? (LiteGraph.NODE_WIDGET_HEIGHT ?? 20) : 20);
const MARGIN = 15;

// ---------------------------------------------------------------------------
// Logo — preloaded once, drawn in every PixlStash node title bar
// ---------------------------------------------------------------------------

const _logo = new Image();
_logo.src = new URL("../img/Logo.png", import.meta.url).href;

const LOGO_H = 14; // height in pixels inside the title bar

/** Draw the logo at the left edge of the node title bar if loaded. */
function drawLogoInTitle(ctx, node) {
    if (_logo.complete && _logo.naturalWidth > 0) {
        const aspect = _logo.naturalWidth / _logo.naturalHeight;
        const w = Math.round(LOGO_H * aspect);
        const titleH = LiteGraph.NODE_TITLE_HEIGHT ?? 20;
        ctx.save();
        ctx.globalAlpha = 0.85;
        ctx.drawImage(_logo, 6, (titleH - LOGO_H) / 2, w, LOGO_H);
        ctx.restore();
    }
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
 * Follow the link on `inputName` to the origin node and read the widget
 * value for that output slot.  Used to get upstream IDs in Picture Loader.
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
    return w?.value ?? "";
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
// Fetch functions for each picker type
// ---------------------------------------------------------------------------

// Wraps a fetch function to prepend a “— None —” entry.
const withNoneOption = (fetchFn) => (node) =>
    fetchFn(node).then(items => [{ value: "", label: "— None —" }, ...items]);

async function fetchProjects(node) {
    return proxyFetch("/pixlstash/projects", getSettingsCredentials())
        .then(data => data.map(p => ({ value: String(p.id), label: p.name ?? String(p.id) })));
}

async function fetchSets(node) {
    const pid   = findUpstreamProjectId(node);
    const extra = pid ? { project_id: pid } : {};
    return proxyFetch("/pixlstash/picture_sets", getSettingsCredentials(), extra)
        .then(data => data
            .filter(s => !s.reference_character)
            .map(s => ({
                value: String(s.id),
                label: `${s.name ?? s.id} (${s.picture_count ?? "?"})`,
            })));
}

async function fetchCharacters(node) {
    const pid   = findUpstreamProjectId(node);
    const extra = pid ? { project_id: pid } : {};
    return proxyFetch("/pixlstash/characters", getSettingsCredentials(), extra)
        .then(data => data.map(c => ({ value: String(c.id), label: c.name ?? String(c.id) })));
}

async function fetchAllCharacters(_node) {
    return proxyFetch("/pixlstash/characters", getSettingsCredentials())
        .then(data => data.map(c => ({ value: String(c.id), label: c.name ?? String(c.id) })));
}

async function fetchSortMechanisms(node) {
    return proxyFetch("/pixlstash/sort_mechanisms", getSettingsCredentials())
        .then(data => data
            .filter(m => (m.key ?? m.sort_key ?? m.name) !== "LIKENESS_GROUPS")
            .map(m => ({
                value: m.key   ?? m.sort_key ?? m.name ?? String(m),
                label: m.label ?? m.description ?? m.key ?? String(m),
            })));
}

// ---------------------------------------------------------------------------
// Canvas drawing helpers
// ---------------------------------------------------------------------------

function truncateText(ctx, text, maxWidth) {
    if (ctx.measureText(text).width <= maxWidth) return text;
    let t = text;
    while (t.length > 1 && ctx.measureText(t + "…").width > maxWidth) t = t.slice(0, -1);
    return t + "…";
}

// ---------------------------------------------------------------------------
// Custom widget factory  (used by getCustomWidgets)
// ---------------------------------------------------------------------------

/**
 * Build a live-picker widget object.
 *
 * @param {string}   inputName  — widget name / serialisation key
 * @param {string}   defaultVal — default value
 * @param {Function} fetchFn    — async (node) → [{value, label}]
 * @returns {object} LiteGraph widget
 */
function buildPickerWidget(inputName, defaultVal, fetchFn) {
    let _label    = "";
    let _loading  = false;
    let _error    = null;

    const widget = {
        type:  "PS_DROPDOWN",
        name:  inputName,
        value: defaultVal,
        // Serialise the selected ID, not the display label
        serialize: true,

        draw(ctx, node, widgetWidth, y, widgetHeight) {
            const H = WH(); // fixed height — don't stretch to fill node
            ctx.save();

            // Background
            ctx.fillStyle   = LiteGraph.WIDGET_BGCOLOR   ?? "#2a2a2a";
            ctx.strokeStyle = LiteGraph.WIDGET_OUTLINE_COLOR ?? "#555";
            ctx.lineWidth   = 1;
            ctx.beginPath();
            ctx.roundRect(MARGIN, y, widgetWidth - MARGIN * 2, H, 3);
            ctx.fill();
            ctx.stroke();

            ctx.font         = "11px Arial";
            ctx.textBaseline = "middle";

            // If we have a value but no label yet (e.g. first draw after load),
            // kick off a background label refresh without blocking the draw.
            if (this.value && !_label && !_loading && !_error) {
                _loading = true;
                fetchFn(node)
                    .then(items => {
                        _loading = false;
                        const match = items.find(i => i.value === String(this.value));
                        if (match) { _label = match.label; _error = null; }
                        node.setDirtyCanvas(true, true);
                    })
                    .catch(() => { _loading = false; });
            }

            if (_loading) {
                ctx.fillStyle = "#888";
                ctx.textAlign = "center";
                ctx.fillText("Loading…", widgetWidth / 2, y + H / 2);
            } else if (_error) {
                ctx.fillStyle = "#f88";
                ctx.textAlign = "left";
                ctx.fillText(
                    truncateText(ctx, `⚠ ${_error}`, widgetWidth - MARGIN * 2 - 8),
                    MARGIN + 6, y + H / 2,
                );
            } else {
                const label = _label || this.value || "(click to select)";
                ctx.fillStyle = (this.value) ? (LiteGraph.WIDGET_TEXT_COLOR ?? "#ddd") : "#666";
                ctx.textAlign = "left";
                ctx.fillText(
                    truncateText(ctx, label, widgetWidth - MARGIN * 2 - 24),
                    MARGIN + 6, y + H / 2,
                );
                ctx.fillStyle = "#999";
                ctx.textAlign = "right";
                ctx.fillText("▼", widgetWidth - MARGIN - 5, y + H / 2);
            }

            ctx.restore();
        },

        mouse(event, pos, node) {
            // In new ComfyUI the event is a PointerEvent (type = "pointerdown")
            // In old ComfyUI it's a MouseEvent (type = "mousedown")
            // We only act on the initial press, not on release.
            if (event.type !== "pointerdown" && event.type !== "mousedown") return false;

            _loading = true;
            _error   = null;
            node.setDirtyCanvas(true, true);

            // Capture the event for ContextMenu positioning
            const capturedEvent = event;

            fetchFn(node)
                .then(items => {
                    _loading = false;

                    if (!items?.length) {
                        _error = "No items found";
                        node.setDirtyCanvas(true, true);
                        return;
                    }

                    const menuItems = items.map(item => ({
                        content:  item.label,
                        callback: () => {
                            widget.value = item.value;
                            _label       = item.label;
                            _error       = null;
                            if (typeof widget.callback === "function") {
                                widget.callback(widget.value);
                            }
                            if (typeof widget.onValueChange === "function") {
                                widget.onValueChange(widget.value, node);
                            }
                            node.setDirtyCanvas(true, true);
                        },
                    }));

                    new LiteGraph.ContextMenu(menuItems, { event: capturedEvent });
                    node.setDirtyCanvas(true, true);
                })
                .catch(err => {
                    _loading = false;
                    _error   = err.message;
                    node.setDirtyCanvas(true, true);
                });

            return true;
        },

        computeSize(width) {
            return [width, WH()];
        },

        /** Clear selection — called when an upstream filter changes. */
        reset() {
            widget.value = "";
            _label       = "";
            _error       = null;
        },

        /**
         * Optional hook called after the user confirms a new value.
         * Signature: (newValue: string, node: LGraphNode) => void
         */
        onValueChange: null,

        // Called on node load (onConfigure) to resolve the saved ID back to a label.
        // Retries are scheduled because links may not be restored yet when
        // onConfigure fires.
        refreshLabel(node) {
            if (!widget.value) return;

            const attempt = (attemptsLeft) => {
                fetchFn(node)
                    .then(items => {
                        const match = items.find(i => i.value === String(widget.value));
                        if (match) {
                            _label = match.label;
                            _error = null;
                            node.setDirtyCanvas(true, true);
                        } else if (attemptsLeft > 0) {
                            // Item not in list yet — graph may still be loading
                            setTimeout(() => attempt(attemptsLeft - 1), 500);
                        }
                    })
                    .catch(() => {
                        // Connector probably not wired yet; retry a few times
                        if (attemptsLeft > 0) {
                            setTimeout(() => attempt(attemptsLeft - 1), 500);
                        }
                    });
            };

            // First attempt after a short delay so graph links are restored
            setTimeout(() => attempt(5), 200);
        },
    };

    return widget;
}

// ---------------------------------------------------------------------------
// Cascade reset — called when an upstream project/set changes
// ---------------------------------------------------------------------------

/**
 * Walk all output links of `node` and reset every PS_DROPDOWN widget found
 * on the immediate and transitive downstream nodes.
 * This clears stale set_id / character_id selections after a project change.
 */
function resetDownstreamFilters(node) {
    for (const output of node.outputs ?? []) {
        for (const linkId of output.links ?? []) {
            const link = app.graph.links[linkId];
            if (!link) continue;
            const target = app.graph.getNodeById(link.target_id);
            if (!target) continue;
            let dirty = false;
            for (const w of target.widgets ?? []) {
                if (w.type === "PS_DROPDOWN" && typeof w.reset === "function") {
                    w.reset();
                    dirty = true;
                }
            }
            if (dirty) target.setDirtyCanvas(true, true);
            // Recurse: Project → Set Loader → Character Loader
            resetDownstreamFilters(target);
        }
    }
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
        // Inject credentials from Settings into PictureLoader / PictureSaver
        // nodes just before execution so they reach Python without appearing
        // in the UI or being saved into exported workflow JSON.
        const _origQueuePrompt = app.api.queuePrompt?.bind(app.api);
        if (_origQueuePrompt) {
            app.api.queuePrompt = async function (number, promptData) {
                const output = promptData?.output;
                if (output) {
                    const creds     = getSettingsCredentials();
                    const injectFor = [
                        "PixlStashPictureLoader",
                        "PixlStashPictureSaver",
                    ];
                    for (const nodeId in output) {
                        if (injectFor.includes(output[nodeId].class_type)) {
                            output[nodeId].inputs.url        = creds.url;
                            output[nodeId].inputs.token      = creds.token;
                            output[nodeId].inputs.verify_ssl = creds.verifySsl;
                        }
                    }
                }
                return _origQueuePrompt(number, promptData);
            };
        }
    },

    // ------------------------------------------------------------------
    // 2. Register custom widget types
    //    Called before any nodes are created, so ComfyUI uses our
    //    handlers when it sees these types in INPUT_TYPES.
    // ------------------------------------------------------------------
    getCustomWidgets(app) {
        const make = (fetchFn) => (node, inputName, inputData) => {
            const defaultVal = inputData[1]?.default ?? "";
            const widget     = buildPickerWidget(inputName, defaultVal, fetchFn);
            node.addCustomWidget(widget);

            // When a workflow is loaded, the saved value is restored before
            // onConfigure fires.  Hook it to resolve the ID back to a label.
            const prevConfigure = node.onConfigure;
            node.onConfigure = function (data) {
                prevConfigure?.call(this, data);
                widget.refreshLabel(this);
            };

            return { widget, minWidth: 160, minHeight: WH() };
        };

        // Project ID widget gets a cascade-reset hook so that changing the
        // project automatically clears any selected set_id / character_id
        // on all downstream nodes.
        const makeProject = () => (node, inputName, inputData) => {
            const defaultVal = inputData[1]?.default ?? "";
            const widget     = buildPickerWidget(inputName, defaultVal, withNoneOption(fetchProjects));
            widget.onValueChange = (_val, srcNode) => resetDownstreamFilters(srcNode);
            node.addCustomWidget(widget);
            const prevConfigure = node.onConfigure;
            node.onConfigure = function (data) {
                prevConfigure?.call(this, data);
                widget.refreshLabel(this);
            };
            return { widget, minWidth: 160, minHeight: WH() };
        };

        return {
            PIXLSTASH_PROJECT_ID: makeProject(),
            PIXLSTASH_SET_ID:     make(withNoneOption(fetchSets)),
            PIXLSTASH_CHAR_ID:    make(withNoneOption(fetchCharacters)),
            PIXLSTASH_SORT:       make(fetchSortMechanisms),
            PIXLSTASH_CHARACTER:  make(fetchAllCharacters),
        };
    },

    // ------------------------------------------------------------------
    // 3. Per-node customisation (non-custom-widget changes only)
    // ------------------------------------------------------------------
    async beforeRegisterNodeDef(nodeType, nodeData) {

        // Logo in the title bar — applied to every PixlStash node
        if (nodeData.name?.startsWith("PixlStash")) {
            const origTitle = nodeType.prototype.onDrawTitle;
            nodeType.prototype.onDrawTitle = function (ctx) {
                origTitle?.call(this, ctx);
                drawLogoInTitle(ctx, this);
            };
        }

        // ============================================================
        // PixlStash Picture Loader  — Browse button + hide credential widgets
        // ============================================================
        if (nodeData.name === "PixlStashPictureLoader") {
            const orig = nodeType.prototype.onNodeCreated;
            nodeType.prototype.onNodeCreated = function () {
                orig?.call(this);

                // Hide credential widgets — injected at run time by queuePrompt.
                for (const name of ["url", "token", "verify_ssl"]) {
                    const w = this.widgets?.find(w => w.name === name);
                    if (w) { w.hidden = true; w.computeSize = () => [0, -4]; }
                }

                const picIdsWidget = this.widgets?.find(w => w.name === "picture_ids");
                if (!picIdsWidget) return;

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

        // ============================================================
        // PixlStash Picture Saver  — hide credential widgets
        // ============================================================
        if (nodeData.name === "PixlStashPictureSaver") {
            const orig = nodeType.prototype.onNodeCreated;
            nodeType.prototype.onNodeCreated = function () {
                orig?.call(this);
                for (const name of ["url", "token", "verify_ssl"]) {
                    const w = this.widgets?.find(w => w.name === name);
                    if (w) { w.hidden = true; w.computeSize = () => [0, -4]; }
                }
            };
        }
    },
});

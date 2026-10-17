/**
 * The generated mark a shelf model wears when it has no picture: two initials
 * on a coloured tile, the same tile PixlStash's own shelf draws for it.
 *
 * A PORT of `generatedMark` in PixlStash's `frontend/src/utils/modelShelf.js`,
 * and it has to stay one: the point is that a file reads as the same square in
 * the loader as on the shelf. PixlStash's `tests/test_comfyui_pack_model_mark.py`
 * runs both over the same rows and fails on any difference, so change the two
 * together.
 *
 * The colour is keyed on a hash of the row's folded base model (every spelling
 * of one base model is one colour), the initials on the name the shelf shows:
 * the given name, else the base model a checkpoint's filename matched, else the
 * filename with its quant postfix and training-step suffix stripped. The first
 * number in that name rides on the corner as a version tab.
 */

// `SET_COLORS` in PixlStash's `setAppearance.js`, in order: the hash indexes it.
const PALETTE = [
    "#e53935", "#00acc1", "#f4511e", "#039be5", "#ff7043", "#546e7a",
    "#fb8c00", "#1e88e5", "#fdd835", "#3949ab", "#c0ca33", "#9c27b0",
    "#7cb342", "#8e24aa", "#43a047", "#d81b60", "#00897b", "#f06292",
    "#00bfa5", "#6d4c41", "#ff5252", "#00e5ff", "#ff6d00", "#2979ff",
    "#ffd740", "#651fff", "#64dd17", "#e040fb", "#1de9b6", "#f50057",
    "#b71c1c", "#006064", "#e65100", "#0d47a1", "#827717", "#4a148c",
    "#1b5e20", "#880e4f", "#004d40", "#37474f", "#ce93d8", "#ef9a9a",
    "#81d4fa", "#ff8a65", "#ffb300", "#80cbc4", "#a1887f", "#76ff03",
];

// The tile keeps the palette entry's hue and pins the rest, dark enough that
// white initials are legible on every hue.
const TINT_SATURATION = 55;
const TINT_LIGHTNESS  = 30;
const INK = "#ffffff";

const GGUF_HEAD_RE = /^(?:i?q|tq)\d+$/i;
const GGUF_LEVELS = new Set(["k", "s", "m", "l", "xs", "xl", "xxs", "nl", "0", "1"]);
const QUANT_TOKENS = new Set([
    "fp32", "f32", "fp16", "f16", "bf16", "fp8", "f8", "e4m3", "e4m3fn", "e5m2",
    "nvfp4", "mxfp4", "fp4", "nf4", "int8", "i8", "int4", "i4",
]);
const QUANT_MODIFIERS = new Set(["scaled", "awq", "gptq", "fast"]);
const TRAINING_SUFFIX_RE = /^(?:step\d+|epoch\d+|\d+ep|\d{5,})$/i;
const FILLER_WORDS = new Set([
    "of", "in", "on", "at", "by", "for", "from", "to", "with", "the", "and",
]);

/** FNV-1a: stable, and all that is asked of it. */
function hash32(text) {
    let hash = 0x811c9dc5;
    for (let i = 0; i < text.length; i += 1) {
        hash ^= text.charCodeAt(i);
        hash = Math.imul(hash, 0x01000193) >>> 0;
    }
    return hash;
}

function hueOf(hex) {
    const [r, g, b] = [1, 3, 5].map(i => parseInt(hex.slice(i, i + 2), 16) / 255);
    const max = Math.max(r, g, b);
    const span = max - Math.min(r, g, b);
    if (!span) return 0;
    let hue;
    if (max === r) hue = ((g - b) / span) % 6;
    else if (max === g) hue = (b - r) / span + 2;
    else hue = (r - g) / span + 4;
    return Math.round(hue * 60 + 360) % 360;
}

/** The name tokens of a filename, its quant postfix popped off the end. */
function withoutQuant(tokens) {
    for (const width of [3, 2, 1]) {
        if (tokens.length < width) continue;
        const tail = tokens.slice(tokens.length - width);
        if (GGUF_HEAD_RE.test(tail[0]) && tail.slice(1).every(t => GGUF_LEVELS.has(t.toLowerCase()))) {
            return tokens.slice(0, tokens.length - width);
        }
    }
    const kept = [...tokens];
    const popped = [];
    while (kept.length) {
        const last = kept[kept.length - 1].toLowerCase();
        if (!QUANT_TOKENS.has(last) && !QUANT_MODIFIERS.has(last)) break;
        popped.push(kept.pop());
    }
    // `scaled` and `fast` alone are part of somebody's name.
    return popped.some(t => QUANT_TOKENS.has(t.toLowerCase())) ? kept : [...tokens];
}

function derivedName(filename) {
    const base = String(filename || "").split(/[\\/]/).pop();
    const stem = base.replace(/\.[^.]+$/, "").replace(/[_-]/g, " ").trim();
    const tokens = withoutQuant(stem.split(/\s+/).filter(Boolean));
    while (tokens.length && TRAINING_SUFFIX_RE.test(tokens[tokens.length - 1])) tokens.pop();
    return tokens.join(" ");
}

/** The name the shelf shows for a row, which is what its initials come from. */
function shownName(row) {
    const filename = String(row?.filename || "").trim();
    return String(row?.display_name || "").trim()
        || String(row?.matched_name || "").trim()
        || derivedName(filename)
        || filename;
}

/**
 * Up to two letters: the first letters of the first two words that start with
 * a letter, skipping joining words so "World of Wordcraft" is WW. So
 * "sd3.5 large" is SL, and its 35 goes to the tab.
 */
export function markInitials(text) {
    const all = String(text || "").split(/[\s_\-.]+/).filter(Boolean);
    const significant = all.filter((word, i) => i === 0 || !FILLER_WORDS.has(word.toLowerCase()));
    const words = significant.length > 1 ? significant : all;
    const lettered = words.filter(word => /^\p{L}/u.test(word));
    if (lettered.length > 1) return (lettered[0][0] + lettered[1][0]).toUpperCase();
    if (lettered.length === 1) return lettered[0].replace(/\P{L}/gu, "").slice(0, 2).toUpperCase();
    return words.length ? words[0].slice(0, 2).toUpperCase() : "?";
}

/**
 * The tab's version: the name's first number, `x.y` kept with its dot and its
 * whole minor (`1.10`, never `1.1`), else two digits at most. A zero-padded number (`000002750`) is a counter, not a version.
 */
function versionOf(text) {
    const match = /(?<!\d)(?!0\d)(\d+)(?:\.(\d+))?/.exec(String(text || ""));
    if (!match) return "";
    const [, major, minor] = match;
    return major.length === 1 && minor ? `${major}.${minor}` : major.slice(0, 2);
}

/**
 * The mark for one shelf row (a `/adapters` or `/checkpoints` record).
 *
 * @returns {{color: string, ink: string, initials: string, version: string}}
 *   `color` is an `hsl()` tile colour, `ink` the colour of the initials on it,
 *   `version` the tab's digits (`""`: no tab).
 */
export function generatedMark(row) {
    const key = row?.base_model_canonical || row?.base_model_folded || row?.base_model || "";
    const hue = hueOf(PALETTE[hash32(key) % PALETTE.length]);
    const name = shownName(row);
    return {
        color: `hsl(${hue} ${TINT_SATURATION}% ${TINT_LIGHTNESS}%)`,
        ink: INK,
        initials: markInitials(name),
        version: versionOf(name),
    };
}

/**
 * Paint `box` as a row's mark: its initials, on its tile, in its ink, and the
 * version on a tab at the corner, the tile's own pair inverted as on the shelf.
 *
 * The letters are 55% of the tile, as on the shelf, so they fill it at any
 * size: `box` becomes a size container for them, which every caller's box can
 * be because each has a size of its own.
 *
 * `tab` is the tab's height as a CSS length, 15px at least: smaller digits do
 * not read. The tab hangs past the corner, unless `inside` keeps it within a
 * box that clips.
 */
export function paintMark(box, row, { tab = "15px", inside = false } = {}) {
    const mark = generatedMark(row);
    const initials = document.createElement("span");
    initials.textContent = mark.initials;
    initials.style.cssText = "font-size:55cqmin; letter-spacing:-0.02em; line-height:1;";
    box.style.containerType = "size";
    box.replaceChildren(initials);
    box.style.background = mark.color;
    box.style.color = mark.ink;
    if (!mark.version) return;
    if (!box.style.position) box.style.position = "relative";
    const edge = inside ? "4px" : `calc(${tab} * -0.4)`;
    const version = document.createElement("span");
    version.textContent = mark.version;
    version.style.cssText = `position:absolute; right:${edge}; bottom:${edge}; z-index:1; `
        + `box-sizing:border-box; min-width:${tab}; height:${tab}; padding:0 2px; `
        + "display:inline-flex; align-items:center; justify-content:center; "
        + `border:1px solid ${mark.color}; border-radius:999px; background:${mark.ink}; color:${mark.color}; `
        + `font-size:max(11px, calc(${tab} * 0.72)); font-weight:700; line-height:1;`;
    box.appendChild(version);
}

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
    // By code point, not UTF-16 unit, so a letter outside the BMP is taken whole.
    if (lettered.length > 1) return ([...lettered[0]][0] + [...lettered[1]][0]).toUpperCase();
    if (lettered.length === 1) return [...lettered[0].replace(/\P{L}/gu, "")].slice(0, 2).join("").toUpperCase();
    return words.length ? [...words[0]].slice(0, 2).join("").toUpperCase() : "?";
}

/**
 * The tab's version: the name's first number, `x.y` kept with its dot and its
 * whole minor of up to two digits (`1.10`, never `1.1`), else two digits at
 * most. A zero-padded number (`000002750`) is a counter, not a version.
 */
function versionOf(text) {
    const match = /(?<!\d)(?!0\d)(\d+)(?:\.(\d+))?/.exec(String(text || ""));
    if (!match) return "";
    const [, major, minor] = match;
    // A longer "minor" is a date or a counter, not a version: the major alone.
    return major.length === 1 && minor?.length <= 2 ? `${major}.${minor}` : major.slice(0, 2);
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

/*
 * The named tile's icon (PixlStash #1951): a PORT of `namedTile` in
 * PixlStash's `frontend/src/utils/modelTile.js` and of the face `ModelMark.vue`
 * draws for it. The server works the tile out and sends it on `/checkpoints`
 * and `/adapters` rows as `mark`, `mark_colors`, `mark_icon`, `mark_letters`
 * and `mark_band` (and `mark_tab`); this only lays it out. A release
 * (`mark_band`) is its maker's glyph centred on its maker's colour, or the
 * letters in its place; Chroma's eye fills the tile. A re-branded fine-tune
 * (Juggernaut's J) is its own name. The pack draws the icon alone: none of
 * the version and role pills the app hangs off its corners.
 */

// The glyphs the pack ships, by the `mark_icon` the server sends: LobeHub's
// mono icons drawn white (`img/lobehub/LICENSE`), and the owner's family
// pictures. Chroma's eye fills the tile; the horse and star sit where a
// maker's glyph would.
const GLYPHS = {
    chroma: ["model-families/chroma.webp", "full"],
    pony: ["model-families/pony-glyph.webp", "picture"],
    illustrious: ["model-families/illustrious-glyph.webp", "picture"],
    ...Object.fromEntries([
        "bytedance", "fal", "flux", "hunyuan", "ideogram", "kolors", "krea",
        "lightricks", "minimax", "nvidia", "qwen", "stability",
    ].map(name => [name, [`lobehub/${name}.svg`, "glyph"]])),
};

function glyphOf(icon) {
    if (!Object.hasOwn(GLYPHS, icon)) return null;
    const [path, kind] = GLYPHS[icon];
    return { url: new URL(`./img/${path}`, import.meta.url).href, kind };
}

// PixlStash's `--font-ui`: a system stack, so nothing loads late and a width
// once measured stays true.
const FONT_UI = 'system-ui, -apple-system, "Segoe UI", Roboto, Helvetica, Arial, sans-serif';
// A name's size as a share of the tile's line, for one line and for two.
const ONE_LINE = 15.4 / 24;
const TWO_LINES = 11.4 / 24;
const TRACKING = -0.02;
// What a character measures where nothing can be measured (Node), in em.
const FALLBACK_EM = 0.62;

let context;
const widths = new Map();

/** The width of `text` at 1px, semibold, tracked, in `FONT_UI`. */
function widthEm(text) {
    if (!widths.has(text)) {
        if (context === undefined) {
            context = typeof document === "undefined" ? null : document.createElement("canvas").getContext("2d");
            if (context) context.font = `600 100px ${FONT_UI}`;
        }
        const chars = [...text].length;
        widths.set(text, context ? context.measureText(text).width / 100 + TRACKING * chars : FALLBACK_EM * chars);
    }
    return widths.get(text);
}

/**
 * How a row's named tile draws, or null for a row the server named nothing
 * for (no known base model, or a PixlStash too old to send one), which keeps
 * the generated mark. `fallbackColor` is the generated mark's hue, for a
 * family with no maker colour.
 *
 * @returns {{background: string, release?: {glyph: ?{url: string, kind: string},
 *   letters: ?string}, lines?: string[], scale?: number} | null}
 */
export function namedTile(row, fallbackColor) {
    if (!Array.isArray(row?.mark) || !row.mark.length) return null;
    const stops = row.mark_colors?.length ? row.mark_colors : [fallbackColor];
    const background = stops.length > 1 ? `linear-gradient(135deg, ${stops.join(", ")})` : stops[0];
    if (!row.mark_band) {
        const cap = row.mark.length > 1 ? TWO_LINES : ONE_LINE;
        return { background, lines: row.mark, scale: Math.min(cap, 1 / Math.max(...row.mark.map(widthEm))) };
    }
    const glyph = glyphOf(row.mark_icon);
    return { background, release: { glyph, letters: glyph ? null : row.mark_letters || null } };
}

function part(tag, css, text) {
    const el = document.createElement(tag);
    el.style.cssText = css;
    if (text != null) el.textContent = text;
    return el;
}

/**
 * The named tile's face, in shares of the tile so it holds at any size, as
 * `ModelMark` lays it out in 32nds: a glyph or picture 24 of them wide,
 * centred, a maker's glyph with a hard shadow; letters 16 high. A re-branded
 * name is centred too, where the app packs it into the top-start corner to
 * clear the role pill the pack does not draw. Null where a release has no glyph or letters
 * (an icon this pack does not ship), which keeps the generated mark.
 */
function iconParts(tile) {
    const release = tile.release;
    const shadow = "calc(100cqmin / 36) calc(100cqmin / 36) 0 #000";
    if (!release) {
        const name = part("span", "display:flex; flex-direction:column; align-items:center; "
            + `font:600 calc(100cqmin * 28 / 32 * ${tile.scale})/0.9 ${FONT_UI}; letter-spacing:-0.02em; white-space:nowrap;`);
        name.append(...tile.lines.map(line => part("span", "", line)));
        return [name];
    }
    const { glyph, letters } = release;
    if (glyph?.kind === "full") {
        return [Object.assign(part("img", "width:100%; height:100%; object-fit:cover; border-radius:inherit;"),
            { src: glyph.url, alt: "" })];
    }
    if (glyph) {
        const css = "width:75%; height:75%;" + (glyph.kind === "glyph" ? ` filter:drop-shadow(${shadow});` : "");
        return [Object.assign(part("img", css), { src: glyph.url, alt: "" })];
    }
    return letters
        ? [part("span", `font:700 50cqmin/1 ${FONT_UI}; letter-spacing:-0.04em; text-shadow:${shadow};`, letters)]
        : null;
}

/**
 * Paint `box` as a row's mark: its initials, on its tile, in its ink, and the
 * version on a tab at the corner, the tile's own pair inverted as on the shelf.
 * With `named`, a row the server named (PixlStash #1951) draws its named
 * tile's icon instead, on its maker's colour, with no tab.
 *
 * The letters are 55% of the tile, as on the shelf, so they fill it at any
 * size: `box` becomes a size container for them, which every caller's box can
 * be because each has a size of its own. A caller's box centres what it holds.
 *
 * `tab` is the tab's height as a CSS length, 15px at least: smaller digits do
 * not read. The tab hangs past the corner, unless `inside` keeps it within a
 * box that clips.
 */
export function paintMark(box, row, { tab = "15px", inside = false, named = false } = {}) {
    const mark = generatedMark(row);
    box.style.containerType = "size";
    box.style.color = mark.ink;
    const tile = named ? namedTile(row, mark.color) : null;
    const icon = tile && iconParts(tile);
    if (icon) {
        box.replaceChildren(...icon);
        box.style.background = tile.background;
        return;
    }
    const initials = document.createElement("span");
    initials.textContent = mark.initials;
    initials.style.cssText = "font-size:55cqmin; letter-spacing:-0.02em; line-height:1;";
    box.replaceChildren(initials);
    box.style.background = mark.color;
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

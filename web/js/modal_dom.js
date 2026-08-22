/**
 * The bits of modal chrome the PixlStash pickers share.
 *
 * Both `picker.js` (pictures) and `adapter_picker.js` (adapters) build a dark
 * overlay with the same buttons, rows and selects. The modals themselves are
 * different enough not to merge — one pages the network and multi-selects,
 * the other filters a fetched array and single-selects — but these four are
 * pure DOM and there is no reason to carry two copies.
 *
 * `el` assigns properties rather than parsing markup, so server-supplied
 * strings passed as `textContent` are never interpreted as HTML.
 *
 * `fitLabel` is the odd one out — it measures canvas text for the node widgets
 * rather than building DOM — and lives here because it is the other thing in
 * this package that needs a document and nothing else.
 */

export function el(tag, props = {}) {
    const node = document.createElement(tag);
    for (const [k, v] of Object.entries(props)) {
        if (k === "style") node.style.cssText = v;
        else               node[k] = v;
    }
    return node;
}

export function mkBtn(label, bg = "#3a3a3a") {
    return el("button", {
        textContent: label,
        style: `
            padding:5px 14px; cursor:pointer; background:${bg};
            border:1px solid #555; border-radius:4px;
            color:#ddd; font-size:.85em; flex-shrink:0;
        `,
    });
}

export function mkRow(...children) {
    const d = el("div", { style: "display:flex; align-items:center; gap:10px; flex-shrink:0;" });
    d.append(...children);
    return d;
}

export function selStyle() {
    return "background:#2d2d2d; color:#ddd; border:1px solid #555; border-radius:4px; padding:4px 8px; font-size:.85em;";
}

/**
 * The longest prefix of `text` that fits `widthPx`, ellipsised if it had to cut.
 *
 * LiteGraph draws a button widget's text centred and does not clip it, so a
 * long file name on a narrow node spills over both edges of the button and
 * over its neighbours. Measured rather than estimated at so-many-pixels-per-
 * character: these are file names, and one full of `W`s would still overflow
 * while one full of `l`s would be cut with room to spare.
 */
// undefined = not tried yet, null = no context to be had. `getContext` is
// allowed to return null and does in constrained environments; resolved on
// first use rather than at import so a missing document cannot break loading
// the module either. With no context nothing is measurable and every label is
// drawn whole, which is what this looked like before it cut anything.
let _measureCtx;

function measureCtx() {
    if (_measureCtx === undefined) {
        _measureCtx =
            globalThis.document?.createElement("canvas")?.getContext?.("2d") ?? null;
    }
    return _measureCtx;
}

export function fitLabel(text, widthPx) {
    const ctx = measureCtx();
    if (!ctx || widthPx <= 0) return text;
    ctx.font = `${globalThis.LiteGraph?.NODE_TEXT_SIZE ?? 14}px Arial`;
    if (ctx.measureText(text).width <= widthPx) return text;
    let cut = text;
    while (cut.length && ctx.measureText(`${cut}…`).width > widthPx) {
        cut = cut.slice(0, -1);
    }
    return `${cut.trimEnd()}…`;
}


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

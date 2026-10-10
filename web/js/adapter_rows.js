/**
 * How the Multi Adapter Loader's rows are stored.
 *
 * The rows are flat, numbered widgets — `adapter_sha256` / `strength_model` /
 * `strength_clip` for the first, then `adapter_sha256_2` and so on — because
 * PixlStash finds LoRA slots by field name (see nodes/multi_adapter_loader.py,
 * which declares the same names). Everything here is arithmetic over those
 * widgets and nothing else, so it imports nothing and runs without a canvas.
 *
 * A row is `{ adapter_sha256, strength_model, strength_clip }`. An empty row is
 * an empty digest, never a missing widget, and the filled rows are kept packed
 * at the top: removing one moves the rows below it up before anything is
 * written.
 */

/** Must match ROWS in nodes/multi_adapter_loader.py. */
export const ROWS = 8;

const EMPTY = { adapter_sha256: "", strength_model: 1, strength_clip: 1 };

/** The widget name of `field` on row `row` (1-based): bare, then `_2` on. */
export const rowField = (field, row) => (row === 1 ? field : `${field}_${row}`);

const digestOf = (value) => String(value ?? "").trim().toLowerCase();

const strengthOf = (value) => {
    const n = Number(value);
    return Number.isFinite(n) ? n : 1;
};

/** The filled rows, top to bottom, out of `widgets` (a Map of name → widget). */
export function readRows(widgets) {
    const rows = [];
    for (let row = 1; row <= ROWS; row++) {
        const adapter_sha256 = digestOf(widgets.get(rowField("adapter_sha256", row))?.value);
        if (!adapter_sha256) continue;
        rows.push({
            adapter_sha256,
            strength_model: strengthOf(widgets.get(rowField("strength_model", row))?.value),
            strength_clip:  strengthOf(widgets.get(rowField("strength_clip", row))?.value),
        });
    }
    return rows;
}

/**
 * Write `rows` to the top of the node and empty every row below them.
 *
 * Returns whether anything changed. A widget is only assigned when its value
 * differs: an assignment that changes nothing would still have ComfyUI mark a
 * freshly opened workflow as modified.
 */
export function writeRows(widgets, rows) {
    let changed = false;
    for (let row = 1; row <= ROWS; row++) {
        const values = rows[row - 1] ?? EMPTY;
        for (const field of Object.keys(EMPTY)) {
            const widget = widgets.get(rowField(field, row));
            if (!widget) continue;
            // A digest saved padded or in capitals is the same digest, and is
            // left as it was written.
            const held = field === "adapter_sha256" ? digestOf(widget.value) : widget.value;
            if (held === values[field]) continue;
            widget.value = values[field];
            changed = true;
        }
    }
    return changed;
}

/**
 * The rows after the grid was confirmed with `digests` ticked, in that order.
 *
 * A digest that was already on the node keeps the strengths it had; a new one
 * starts at 1 / 1. `replaced` maps a digest to the one it was swapped in for
 * (another of the same person's adapters), which hands it that row's strengths
 * — but only if no tick still claims that row as itself, so what never left
 * the node never loses its strengths to what replaced it. Anything past the
 * last row the node has is dropped.
 */
export function withPicks(rows, digests, replaced = new Map()) {
    const left = [...rows];
    // The row in the tick's own place if it holds `digest`, else the first that
    // does: the same adapter may sit on two rows, each with its own strengths.
    const take = (digest, i) => {
        const at = left[i]?.adapter_sha256 === digest
            ? i
            : left.findIndex(r => r?.adapter_sha256 === digest);
        if (at < 0) return null;
        const row = left[at];
        left[at] = null;
        return row;
    };
    const picks = digests.slice(0, ROWS).map(digestOf);
    // Every tick that is a row first, then the swaps over what is left.
    const kept = picks.map(take);
    return picks.map((adapter_sha256, i) => {
        const from = kept[i] ?? take(digestOf(replaced.get(adapter_sha256) ?? ""), i);
        return { ...(from ?? EMPTY), adapter_sha256 };
    });
}

/**
 * Tick `digest` in the grid, or untick it: `ticked` is the digests in the
 * order they will fill the rows, changed in place.
 *
 * Unticking takes every occurrence — the same adapter may sit on two rows, and
 * a card that still read as ticked after being clicked would be lying — and
 * forgets what the digest was swapped in for. Ticking past `limit` does nothing.
 */
export function toggleTick(ticked, replaced, digest, limit = ROWS) {
    if (!ticked.includes(digest)) {
        if (ticked.length < limit) ticked.push(digest);
        return;
    }
    for (let at = ticked.indexOf(digest); at >= 0; at = ticked.indexOf(digest)) {
        ticked.splice(at, 1);
    }
    replaced.delete(digest);
}

/**
 * A people card's choice went from `was` to `now`: put `now` where `was` is
 * ticked, so the row keeps its place, and remember what it replaced.
 *
 * Nothing happens unless `was` is ticked and `now` is not. With both of a
 * person's adapters ticked the choice only changes which one the card shows.
 */
export function swapTick(ticked, replaced, was, now) {
    const at = ticked.indexOf(was);
    if (at < 0 || ticked.includes(now)) return;
    ticked[at] = now;
    replaced.set(now, replaced.get(was) ?? was);
    replaced.delete(was);
}

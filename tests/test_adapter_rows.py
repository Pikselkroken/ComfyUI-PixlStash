"""The Multi Adapter Loader's rows, as the browser stores and picks them.

``web/js/adapter_rows.js`` is the arithmetic over the node's flat, numbered
widgets, and ``adapter_picker.js`` folds the shelf into people for the grid's
people view.  What is pinned: the widget names are the ones the Python node
declares, a removed row leaves no gap, confirming the grid keeps the strengths
of the rows that stay, and a person is whoever an adapter is attached to first.

Run in Node, so this is skipped where there is no ``node``.
"""

import json
import pathlib
import shutil
import subprocess
import tempfile
import unittest

import _bootstrap as boot

multi = boot.load("nodes.multi_adapter_loader")

ROOT = pathlib.Path(__file__).resolve().parents[1]

DRIVER = """
import { ROWS, readRows, rowField, swapTick, toggleTick, withPicks, writeRows } from "./adapter_rows.js";
import { foldPeople, personOf, triggerText } from "./adapter_picker.js";

const A = "a".repeat(64), B = "b".repeat(64), C = "c".repeat(64), D = "d".repeat(64);

/** A node's row widgets, as a saved graph would restore them. */
function node(rows) {
    const widgets = new Map();
    for (let row = 1; row <= ROWS; row++) {
        const [sha = "", model = 1, clip = 1] = rows[row - 1] ?? [];
        widgets.set(rowField("adapter_sha256", row), { value: sha });
        widgets.set(rowField("strength_model", row), { value: model });
        widgets.set(rowField("strength_clip", row), { value: clip });
    }
    return widgets;
}
const dump = (widgets) => Object.fromEntries([...widgets].map(([name, w]) => [name, w.value]));
const filled = (widgets) => readRows(widgets).map(r => [r.adapter_sha256, r.strength_model, r.strength_clip]);

const results = { names: [...node([]).keys()] };

// Three rows, the middle one removed: two rows and no gap.
{
    const widgets = node([[A, 0.9, 0.8], [B, 0.5, 0.4], [C, 0.3, 0.2]]);
    const rows = readRows(widgets);
    rows.splice(1, 1);
    results.removed = { changed: writeRows(widgets, rows), widgets: dump(widgets) };
}

// A gap somebody else wrote closes; padding and capitals are still a digest.
{
    const widgets = node([[A, 0.9, 0.8], ["", 1, 1], [` ${C.toUpperCase()} `, 0.3, 0.2], ["", 1, 1], [B, 0.5, 0.4]]);
    results.gap = { read: filled(widgets), changed: writeRows(widgets, readRows(widgets)), widgets: dump(widgets) };
}

// Writing what is already there changes nothing, and says so.
{
    const widgets = node([[` ${A.toUpperCase()}`, 0.9, 0.8]]);
    results.unchanged = { changed: writeRows(widgets, readRows(widgets)), first: widgets.get("adapter_sha256").value };
}

// The grid confirmed: B unticked, D new, C swapped for another of that person's.
{
    const rows = readRows(node([[A, 0.9, 0.8], [B, 0.5, 0.4], [C, 0.3, 0.2]]));
    const E = "e".repeat(64);
    results.picks = withPicks(rows, [A, E, D], new Map([[E, C]])).map(r => [r.adapter_sha256, r.strength_model, r.strength_clip]);
    results.tooMany = withPicks([], Array.from({ length: 12 }, (_, i) => i.toString(16).repeat(64))).map(r => r.adapter_sha256[0]).join("");
    results.twice = withPicks(readRows(node([[A, 0.9, 0.8], [A, 0.1, 0.2]])), [A, A]).map(r => r.strength_model);
    // …and with the first of them swapped for B, each row still keeps its own.
    results.twiceSwapped = withPicks(readRows(node([[A, 0.9, 0.8], [A, 0.1, 0.2]])), [B, A], new Map([[B, A]])).map(r => r.strength_model);
    // A row that is still ticked as itself keeps its strengths, whatever was
    // swapped in for it and whichever of the two was ticked first.
    const two = readRows(node([[A, 0.5, 0.5], [B, 0.8, 0.8]]));
    const strengths = (picked, replaced) => withPicks(two, picked, new Map(replaced)).map(r => [r.adapter_sha256[0], r.strength_model]);
    results.swapButStillThere = [strengths([B, A], [[B, A]]), strengths([A, B], [[B, A]]), strengths([C, A], [[C, A]])];
}

// The grid's ticks: a people card's choice, and a card clicked.
{
    const run = (ticked, steps) => {
        const replaced = new Map();
        for (const [what, ...args] of steps) (what === "swap" ? swapTick : toggleTick)(ticked, replaced, ...args);
        return { ticked: ticked.map(d => d[0]).join(""), replaced: [...replaced].map(([k, v]) => k[0] + v[0]).join(",") };
    };
    results.ticks = {
        swapInPlace:     run([A, B], [["swap", B, C]]),
        swapOntoTicked:  run([A, B], [["swap", A, B]]),
        swapOfUnticked:  run([A], [["swap", B, C]]),
        swapTwice:       run([A], [["swap", A, B], ["swap", B, C]]),
        swapThenUntick:  run([A], [["swap", A, B], ["toggle", B]]),
        untickBothRows:  run([A, B, A], [["toggle", A]]),
        tickAtTheLimit:  run([A, B], [["toggle", C, 2]]),
        tickAppends:     run([A], [["toggle", B]]),
    };
}

// People: by first CHARACTER attachment, one entry each, sorted by name.
{
    const shelf = [
        { sha256: A, attachments: [{ entity_type: "set", entity_id: 9 }, { entity_type: "character", entity_id: 2 }] },
        { sha256: B, attachments: [{ entity_type: "character", entity_id: 1 }, { entity_type: "character", entity_id: 2 }] },
        { sha256: C, attachments: [{ entity_type: "character", entity_id: 2 }] },
        { sha256: D, attachments: [{ entity_type: "set", entity_id: 9 }] },
        { sha256: "f".repeat(64) },
    ];
    results.people = foldPeople(shelf, new Map([["2", "A Test Person"]]))
        .map(p => [p.id, p.name, p.adapters.map(a => a.sha256[0]).join("")]);
    results.persons = shelf.map(personOf);
}

results.triggers = [
    triggerText({ trigger_words: ["one", " two ", ""] }),
    triggerText({ trigger_words: '["TestPerson"]' }),
    triggerText({ trigger_words: "a knight, plate armour" }),
    triggerText({ trigger_words: "[not json" }),
    triggerText({ trigger_words: null }),
    triggerText({}),
];

console.log(JSON.stringify(results));
"""

A, B, C, D, E = ("a" * 64, "b" * 64, "c" * 64, "d" * 64, "e" * 64)


@unittest.skipUnless(shutil.which("node"), "needs node")
class AdapterRowsTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        with tempfile.TemporaryDirectory() as tmp:
            pathlib.Path(tmp, "package.json").write_text('{"type": "module"}')
            for name in ("adapter_rows.js", "adapter_picker.js", "modal_dom.js"):
                shutil.copy(ROOT / "web" / "js" / name, tmp)
            pathlib.Path(tmp, "driver.js").write_text(DRIVER)
            done = subprocess.run(
                ["node", str(pathlib.Path(tmp, "driver.js"))],
                capture_output=True,
                text=True,
                timeout=60,
            )
        if done.returncode:
            raise AssertionError(done.stderr)
        cls.results = json.loads(done.stdout.strip().splitlines()[-1])

    def _rows(self, widgets):
        """``widgets`` as ``[digest, model, clip]`` per row, top to bottom."""
        return [
            [widgets[multi.row_field(field, row)] for field in multi.ROW_FIELDS]
            for row in range(1, multi.ROWS + 1)
        ]

    def test_the_widget_names_are_the_node_s_inputs(self):
        declared = [
            name for name in multi.PixlStashMultiAdapterLoader.INPUT_TYPES()["optional"]
        ]
        self.assertEqual(self.results["names"], declared[1:])  # all but `clip`

    def test_a_removed_middle_row_leaves_two_rows_and_no_gap(self):
        removed = self.results["removed"]
        self.assertTrue(removed["changed"])
        self.assertEqual(
            self._rows(removed["widgets"]),
            [[A, 0.9, 0.8], [C, 0.3, 0.2]] + [["", 1, 1]] * 6,
        )

    def test_a_gap_closes_and_each_row_keeps_its_own_strengths(self):
        gap = self.results["gap"]
        self.assertEqual(gap["read"], [[A, 0.9, 0.8], [C, 0.3, 0.2], [B, 0.5, 0.4]])
        self.assertTrue(gap["changed"])
        self.assertEqual(self._rows(gap["widgets"])[:4], gap["read"] + [["", 1, 1]])

    def test_packed_rows_are_not_rewritten(self):
        # An assignment that changes nothing still marks the workflow modified.
        unchanged = self.results["unchanged"]
        self.assertFalse(unchanged["changed"])
        self.assertEqual(unchanged["first"], f" {A.upper()}")

    def test_confirming_the_grid_keeps_what_stays_and_appends_what_is_new(self):
        self.assertEqual(
            self.results["picks"],
            # A kept its strengths; E took C's place and C's strengths; D is new.
            [[A, 0.9, 0.8], [E, 0.3, 0.2], [D, 1, 1]],
        )

    def test_the_grid_cannot_fill_more_rows_than_the_node_has(self):
        # The first eight picked, not any eight.
        self.assertEqual(self.results["tooMany"], "01234567")

    def test_a_row_still_ticked_never_loses_its_strengths_to_a_swap(self):
        in_one_order, in_the_other, really_swapped = self.results["swapButStillThere"]
        self.assertEqual(in_one_order, [["b", 0.8], ["a", 0.5]])
        self.assertEqual(in_the_other, [["a", 0.5], ["b", 0.8]])
        # A is ticked as itself, so C cannot take its row: C is new.
        self.assertEqual(really_swapped, [["c", 1], ["a", 0.5]])

    def test_a_card_s_choice_swaps_the_tick_where_it_stands(self):
        ticks = self.results["ticks"]
        self.assertEqual(ticks["swapInPlace"], {"ticked": "ac", "replaced": "cb"})
        # C stands for the row A had, not for B's short stay in it.
        self.assertEqual(ticks["swapTwice"], {"ticked": "c", "replaced": "ca"})

    def test_a_choice_never_ticks_one_adapter_twice_or_ticks_a_person(self):
        ticks = self.results["ticks"]
        # Both of a person's adapters ticked: the choice changes neither.
        self.assertEqual(ticks["swapOntoTicked"], {"ticked": "ab", "replaced": ""})
        self.assertEqual(ticks["swapOfUnticked"], {"ticked": "a", "replaced": ""})

    def test_a_click_unticks_every_row_of_an_adapter_and_forgets_its_swap(self):
        ticks = self.results["ticks"]
        self.assertEqual(ticks["untickBothRows"], {"ticked": "b", "replaced": ""})
        self.assertEqual(ticks["swapThenUntick"], {"ticked": "", "replaced": ""})
        self.assertEqual(ticks["tickAppends"]["ticked"], "ab")
        self.assertEqual(ticks["tickAtTheLimit"]["ticked"], "ab")

    def test_the_same_adapter_on_two_rows_keeps_both_strengths(self):
        self.assertEqual(self.results["twice"], [0.9, 0.1])
        self.assertEqual(self.results["twiceSwapped"], [0.9, 0.1])

    def test_a_person_is_the_first_character_an_adapter_is_attached_to(self):
        self.assertEqual(self.results["persons"], ["2", "1", "2", None, None])
        self.assertEqual(
            self.results["people"],
            # Named from the list, or by number; each adapter under one person.
            [["2", "A Test Person", "ac"], ["1", "Character #1", "b"]],
        )

    def test_trigger_words_read_as_the_node_s_output_does(self):
        adapter_loader = boot.load("nodes.adapter_loader")
        records = [
            {"trigger_words": ["one", " two ", ""]},
            {"trigger_words": '["TestPerson"]'},
            {"trigger_words": "a knight, plate armour"},
            {"trigger_words": "[not json"},
            {"trigger_words": None},
            {},
        ]
        self.assertEqual(
            self.results["triggers"],
            [adapter_loader._trigger_words(r) for r in records],
        )
        self.assertEqual(self.results["triggers"][:2], ["one, two", "TestPerson"])


if __name__ == "__main__":
    unittest.main()

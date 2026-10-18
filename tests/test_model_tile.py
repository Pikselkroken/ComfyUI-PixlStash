"""The named tile the loader nodes and picker draw a known model with.

``namedTile`` in ``web/js/model_mark.js`` lays out what PixlStash's server
sends on a shelf row (``mark``, ``mark_tab``, ``mark_colors``, ``mark_icon``,
``mark_letters``, ``mark_band``). The rows here are shaped as the server sends
them. Run in Node, so skipped where there is none.
"""

import json
import pathlib
import shutil
import subprocess
import tempfile
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]

DRIVER = """
import { namedTile } from "./model_mark.js";

const band = (extra) => ({ mark_band: true, mark_letters: null, ...extra });
console.log(JSON.stringify({
    old_server: namedTile({ filename: "sd_xl_base_1.0.safetensors" }, "hsl(1 2% 3%)"),
    sdxl_base: namedTile(band({
        mark: ["SDXL", "1.0"], mark_tab: "BASE", mark_colors: ["#9d39ff", "#e80000"], mark_icon: "stability",
    }), "hsl(1 2% 3%)"),
    zit: namedTile(band({ mark: ["ZIT"], mark_tab: "⚡4", mark_colors: ["#dc5100"], mark_icon: null, mark_letters: "Z" }), "x"),
    no_colour: namedTile(band({ mark: ["HIDR"], mark_tab: null, mark_colors: null, mark_icon: null, mark_letters: "HI" }), "hsl(1 2% 3%)"),
    qwen: namedTile(band({ mark: ["Qwen", "2511"], mark_tab: "EDIT", mark_colors: ["#615ced"], mark_icon: "qwen" }), "x"),
    pony: namedTile(band({ mark: ["PDXL"], mark_colors: ["#4b2373"], mark_icon: "pony" }), "x"),
    chroma: namedTile(band({ mark: ["Chroma"], mark_tab: "LORA", mark_colors: ["#7a3b12"], mark_icon: "chroma" }), "x"),
    unknown_icon: namedTile(band({ mark: ["New"], mark_icon: "constructor", mark_letters: null }), "x"),
    glyphs: Object.fromEntries([
        "bytedance", "fal", "flux", "hunyuan", "ideogram", "kolors", "krea", "lightricks",
        "minimax", "nvidia", "qwen", "stability", "chroma", "pony", "illustrious",
    ].map(icon => [icon, namedTile(band({ mark: ["X"], mark_icon: icon }), "x").release.glyph?.url ?? null])),
    juggernaut: namedTile({ mark: ["J"], mark_tab: null, mark_colors: ["#6b46c1"], mark_icon: null, mark_band: false }, "x"),
}));
"""


@unittest.skipUnless(shutil.which("node"), "needs node")
class ModelTileTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        with tempfile.TemporaryDirectory() as tmp:
            pathlib.Path(tmp, "package.json").write_text('{"type": "module"}')
            shutil.copy(ROOT / "web" / "js" / "model_mark.js", tmp)
            pathlib.Path(tmp, "driver.js").write_text(DRIVER)
            done = subprocess.run(
                ["node", str(pathlib.Path(tmp, "driver.js"))],
                capture_output=True,
                text=True,
                timeout=60,
            )
        if done.returncode:
            raise AssertionError(done.stderr)
        cls.tiles = json.loads(done.stdout.strip().splitlines()[-1])

    def test_a_row_from_a_server_that_names_nothing_keeps_the_generated_mark(self):
        self.assertIsNone(self.tiles["old_server"])

    def test_a_release_wears_its_maker_s_glyph_on_its_colours(self):
        tile = self.tiles["sdxl_base"]
        self.assertEqual(tile["background"], "linear-gradient(135deg, #9d39ff, #e80000)")
        release = tile["release"]
        self.assertEqual(release["glyph"]["kind"], "glyph")
        self.assertTrue(release["glyph"]["url"].endswith("/img/lobehub/stability.svg"))
        self.assertIsNone(release["letters"])

    def test_every_shipped_glyph_is_on_disk(self):
        shipped = {
            path.name
            for path in (ROOT / "web" / "js" / "img").rglob("*")
            if path.suffix in (".svg", ".webp")
        }
        urls = self.tiles["glyphs"]
        for icon, url in urls.items():
            self.assertIsNotNone(url, icon)
            self.assertTrue((ROOT / "web" / "js" / "img" / url.split("/img/", 1)[1]).is_file(), url)
        # And none on disk that no icon name reaches.
        self.assertEqual({url.rsplit("/", 1)[1] for url in urls.values()}, shipped)

    def test_a_maker_with_no_glyph_wears_its_letters(self):
        release = self.tiles["zit"]["release"]
        self.assertIsNone(release["glyph"])
        self.assertEqual(release["letters"], "Z")

    def test_a_family_with_no_maker_colour_takes_the_hashed_hue(self):
        self.assertEqual(self.tiles["no_colour"]["background"], "hsl(1 2% 3%)")

    def test_the_owner_s_pictures_are_not_maker_glyphs(self):
        self.assertEqual(self.tiles["pony"]["release"]["glyph"]["kind"], "picture")

    def test_chroma_fills_the_tile(self):
        self.assertEqual(self.tiles["chroma"]["release"]["glyph"]["kind"], "full")

    def test_an_icon_the_pack_does_not_ship_draws_no_glyph(self):
        self.assertIsNone(self.tiles["unknown_icon"]["release"]["glyph"])

    def test_a_rebranded_fine_tune_is_its_name_alone(self):
        tile = self.tiles["juggernaut"]
        self.assertNotIn("release", tile)
        self.assertEqual(tile["lines"], ["J"])
        self.assertAlmostEqual(tile["scale"], 15.4 / 24)


if __name__ == "__main__":
    unittest.main()

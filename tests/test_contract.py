"""Kontrak UI <-> backend: payload gaya yang dibuat JavaScript harus diterima validator Python apa adanya."""
import json, os, re, shutil, subprocess, unittest
from pathlib import Path

from backend.layouts import LayoutSpec, build_filtergraph
from backend.pipeline import options_from_style

ROOT = Path(__file__).resolve().parent.parent


@unittest.skipUnless(shutil.which("node"), "butuh node")
class ContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        r = subprocess.run(["node", "tests/frontend.test.mjs"], cwd=ROOT, capture_output=True, text=True,
                           env={**os.environ, "EMIT_STYLES": "1"})
        cls.node_out = r.stdout + r.stderr
        cls.node_rc = r.returncode
        m = re.search(r"@@STYLES@@(.*?)@@END@@", r.stdout, re.S)
        cls.styles = json.loads(m.group(1)) if m else []

    def test_node_suite_passes(self):
        self.assertEqual(self.node_rc, 0, self.node_out[-2000:])

    def test_js_styles_are_accepted_by_python(self):
        self.assertGreaterEqual(len(self.styles), 7)
        modes = set()
        for st in self.styles:
            opts = options_from_style(st, "Judul")
            modes.add(opts.layout.mode)
            g = build_filtergraph(opts.layout, 1920, 1080, ass_file="c.ass", fonts_dir="/f")
            self.assertTrue(g.endswith("[vout]"))
        self.assertEqual(modes, {"fill_face", "fill_center", "letterbox", "split", "pip"})

    def test_js_preset_and_option_values_exist_in_python(self):
        from backend.captions import FONTS, PRESETS
        src = (ROOT / "frontend/js/studio.js").read_text()
        self.assertIn("'viral_pop'", src)
        default_preset = re.search(r"preset: '(\w+)'", src).group(1)
        self.assertIn(default_preset, PRESETS)
        default_font = re.search(r"font: '([\w ]+)'", src).group(1)
        self.assertIn(default_font, FONTS)


if __name__ == "__main__":
    unittest.main()

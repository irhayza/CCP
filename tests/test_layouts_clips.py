import unittest
from backend.clips import (Moment, fuse, heat_mean, heat_peaks, heuristic_candidates, iou, nms, normalize_heatmap,
                           snap, window_for)
from backend.layouts import LayoutSpec, build_filtergraph, cover_crop, ff_path
from backend.transcript import Segment


class LayoutTests(unittest.TestCase):
    def test_cover_crop_even_and_inside(self):
        for sw, sh in ((1920, 1080), (1280, 720), (1080, 1920), (1000, 1000), (3840, 2160), (1919, 1079)):
            cw, ch = cover_crop(sw, sh, 9 / 16)
            self.assertTrue(cw % 2 == 0 and ch % 2 == 0)
            self.assertTrue(cw <= sw and ch <= sh)
            self.assertAlmostEqual(cw / ch, 9 / 16, delta=0.01)

    def test_every_mode_ends_with_vout(self):
        for mode in ("fill_face", "fill_center", "letterbox", "split", "pip", "duo"):
            g = build_filtergraph(LayoutSpec(mode=mode), 1920, 1080, ass_file="captions.ass", fonts_dir="/f")
            self.assertTrue(g.endswith("[vout]"), mode)
            self.assertIn("ass=filename='captions.ass'", g)
            self.assertIn("format=yuv420p", g)

    def test_face_keys_used_only_in_fill_face(self):
        keys = [(0, 0.3), (5, 0.7)]
        self.assertIn("clip(", build_filtergraph(LayoutSpec(mode="fill_face", face_keys=keys), 1920, 1080))
        self.assertNotIn("clip(", build_filtergraph(LayoutSpec(mode="fill_center", face_keys=keys), 1920, 1080))

    def test_already_vertical_source_skips_pan(self):
        g = build_filtergraph(LayoutSpec(mode="fill_face", face_keys=[(0, .5), (3, .6)]), 1080, 1920)
        self.assertNotIn("clip(", g)

    def test_fps_cap_only_when_needed(self):
        self.assertIn("fps=30", build_filtergraph(LayoutSpec(mode="fill_center"), 1920, 1080, src_fps=60))
        self.assertNotIn("fps=", build_filtergraph(LayoutSpec(mode="fill_center"), 1920, 1080, src_fps=30))

    def test_validation(self):
        for bad in (dict(mode="nope"), dict(aspect="21:9"), dict(backdrop="red"), dict(pip_corner="center"),
                    dict(cam=(0.5, 0.5, 0.01, 0.2)), dict(game_fit="stretch")):
            with self.assertRaises(ValueError, msg=str(bad)):
                LayoutSpec(**bad).validate()

    def test_windows_path_escaping(self):
        self.assertEqual(ff_path("C:\\a b\\x.ass"), "'C\\:/a b/x.ass'")
        self.assertIn("'\\''", ff_path("it's.ass"))


def seg(s, e, t="x"):
    return Segment(s, e, t)


class HeatTests(unittest.TestCase):
    HM = [{"start_time": i * 10, "end_time": i * 10 + 10, "value": v} for i, v in enumerate([0.2, 0.4, 1.0, 0.9, 0.1, 0.3])]

    def test_normalise_and_mean(self):
        n = normalize_heatmap([{"start_time": 0, "end_time": 10, "value": 0.5}, {"start_time": 10, "end_time": 20, "value": 0.25},
                               {"bad": 1}, {"start_time": 5, "end_time": 5, "value": 1}])
        self.assertEqual([r["value"] for r in n], [1.0, 0.5])
        self.assertAlmostEqual(heat_mean(self.HM, 20, 40), 0.95)
        self.assertEqual(heat_mean(self.HM, 500, 600), 0.0)

    def test_peaks_respect_separation(self):
        p = heat_peaks(self.HM, 3, 25)
        self.assertEqual(p[0], 25)
        self.assertTrue(all(abs(a - b) >= 25 for i, a in enumerate(p) for b in p[:i]))


class SnapFuseTests(unittest.TestCase):
    SEGS = [seg(0, 8), seg(8, 20), seg(20, 33), seg(33, 50), seg(50, 70), seg(70, 90)]

    def test_snap_to_sentence_boundaries(self):
        self.assertEqual(snap(8.9, 32.1, self.SEGS, 15, 45, 100), (8.0, 33.0))

    def test_snap_enforces_duration(self):
        s, e = snap(8, 90, self.SEGS, 15, 45, 100)
        self.assertLessEqual(e - s, 45)
        self.assertIn(e, [x.end for x in self.SEGS])               # dipotong di batas kalimat
        s, e = snap(20, 22, self.SEGS, 15, 45, 100)
        self.assertGreaterEqual(e - s, 15)
        s, e = snap(95, 99, self.SEGS, 15, 45, 100)                # dekat akhir video: mundur, jangan lewat batas
        self.assertLessEqual(e, 100)
        self.assertGreaterEqual(e - s, 15)

    def test_nms_drops_overlap_keeps_best(self):
        a, b, c = Moment(0, 30, 90), Moment(5, 32, 80), Moment(60, 90, 70)
        kept = nms([b, c, a])
        self.assertEqual([m.score for m in kept], [90, 70])
        self.assertGreater(iou(a, b), 0.3)

    def test_fuse_blends_ai_and_heat_and_cleans(self):
        cands = [{"start": 20, "end": 50, "score": 80, "title": "T", "hashtags": ["#viral!", "viral", "ngakak", ""]},
                 {"start": "x", "end": 5}, {"start": 0, "end": 0.5, "score": 99}, {"start": 500, "end": 540, "score": 99},
                 {"start": float("nan"), "end": 5}]
        out = fuse(cands, TestHM, self.SEGS, 100, window_for("standard"))
        self.assertEqual(len(out), 1)
        m = out[0]
        self.assertEqual(m.ai_score, 80)
        self.assertGreater(m.heat, 0.5)
        self.assertAlmostEqual(m.score, 80 * 0.65 + m.heat * 100 * 0.35, places=1)
        self.assertEqual(m.hashtags, ["viral", "ngakak"])

    def test_fuse_without_heatmap_uses_ai_only(self):
        out = fuse([{"start": 8, "end": 33, "score": 77}], [], self.SEGS, 100, window_for("standard"))
        self.assertEqual(out[0].score, 77)
        self.assertIsNone(out[0].heat)

    def test_heuristic_fallback(self):
        c = heuristic_candidates(HeatTests.HM, self.SEGS, 100, window_for("standard"), n=2)
        self.assertTrue(c and all(x["source"] == "heuristic" for x in c))
        out = fuse(c, HeatTests.HM, self.SEGS, 100, window_for("standard"))
        self.assertTrue(out)
        evenly = heuristic_candidates([], [], 600, window_for("short"), n=3)
        self.assertEqual(len(evenly), 3)
        self.assertEqual(heuristic_candidates([], [], 20, window_for("standard")), [])

    def test_window_custom_and_invalid(self):
        self.assertEqual(window_for("standard", 10, 40), (10.0, 40.0, 25.0))
        self.assertEqual(window_for("standard", 40, 10), window_for("standard"))
        self.assertEqual(window_for("???"), window_for("standard"))


TestHM = HeatTests.HM

if __name__ == "__main__":
    unittest.main()

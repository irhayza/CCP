"""Layout dua pembicara (duo): filtergraph, pembagian dua jalur, analisis gambar, dan sambungan ke renderer."""
import re, shutil, subprocess, tempfile, unittest
from pathlib import Path
from unittest import mock

from backend import facetrack
from backend.captions import CaptionStyle
from backend.facetrack import (FaceSample, _owner_is_a, _top_faces, analyze_image_faces, faces_from_scan,
                               scan_faces, smooth_path, split_two)
from backend.layouts import DUO_DEFAULT_CENTERS, MODES, LayoutSpec, build_filtergraph
from backend.render import RenderOptions, render_clip

HAVE_FFMPEG = shutil.which("ffmpeg") is not None
try:
    import cv2
    import numpy as np
    HAS_CV2 = True
except ImportError:                                  # pragma: no cover
    HAS_CV2 = False


def det(cx, size=0.12, conf=0.9):
    return (cx, size, conf)


class DuoGraphTests(unittest.TestCase):
    def test_duo_is_a_known_mode_and_validates(self):
        self.assertIn("duo", MODES)
        LayoutSpec(mode="duo").validate()

    def test_two_panes_are_stacked_and_fill_the_canvas(self):
        g = build_filtergraph(LayoutSpec(mode="duo"), 1920, 1080, 1080, 1920)
        self.assertIn("split=2[a][b]", g)
        self.assertIn("vstack=inputs=2", g)
        self.assertTrue(g.endswith("[vout]"))
        sizes = re.findall(r"scale=(\d+):(\d+):flags=lanczos", g)
        self.assertEqual(sizes, [("1080", "960"), ("1080", "960")])         # dua panel 9:8 = 1080x1920 utuh

    def test_odd_canvas_height_still_adds_up(self):
        g = build_filtergraph(LayoutSpec(mode="duo"), 1920, 1080, 720, 1280)
        sizes = [int(h) for _, h in re.findall(r"scale=(\d+):(\d+):flags=lanczos", g)]
        self.assertEqual(sum(sizes), 1280)
        self.assertTrue(all(h % 2 == 0 for h in sizes))

    def test_each_pane_uses_its_own_track(self):
        # panel 9:8 dari sumber 16:9 selebar ~63%: pusat hanya bebas bergerak di 0.32..0.68 (di luarnya dijepit ke tepi)
        a = [(0.0, 0.35), (5.0, 0.45)]
        b = [(0.0, 0.55), (5.0, 0.65)]
        g = build_filtergraph(LayoutSpec(mode="duo", face_keys=a, face_keys_b=b), 1920, 1080)
        top, bot = [c for c in g.split(";") if c.startswith(("[a]crop", "[b]crop"))]
        self.assertIn("clip(", top)
        self.assertIn("clip(", bot)
        self.assertNotEqual(top.split("x='")[1], bot.split("x='")[1])

    def test_missing_tracks_fall_back_to_default_centers(self):
        g = build_filtergraph(LayoutSpec(mode="duo"), 1920, 1080)
        self.assertNotIn("clip(", g)                                         # posisi statis, bukan ekspresi gerak
        xs = [float(x) for x in re.findall(r"x='([\d.]+)'", g)]
        self.assertEqual(len(xs), 2)
        self.assertLess(xs[0], xs[1])                                        # panel atas = kiri, bawah = kanan
        self.assertEqual(len(DUO_DEFAULT_CENTERS), 2)

    def test_source_as_narrow_as_the_pane_skips_pan(self):
        g = build_filtergraph(LayoutSpec(mode="duo", face_keys=[(0, .3), (3, .6)], face_keys_b=[(0, .6), (3, .8)]),
                              1080, 960, 1080, 1920)
        self.assertNotIn("clip(", g)
        self.assertEqual(re.findall(r"x='([^']*)'", g), ["0", "0"])

    def test_face_keys_b_is_ignored_by_other_modes(self):
        keys = [(0, 0.3), (5, 0.7)]
        g = build_filtergraph(LayoutSpec(mode="fill_face", face_keys=keys, face_keys_b=keys), 1920, 1080)
        self.assertNotIn("vstack", g)
        self.assertEqual(g.count("clip("), 1)


class TopFacesTests(unittest.TestCase):
    def test_keeps_two_largest_sorted_left_to_right(self):
        faces = _top_faces([det(0.8, 0.2), det(0.2, 0.25), det(0.5, 0.05)])
        self.assertEqual([f[0] for f in faces], [0.2, 0.8])

    def test_tiny_background_faces_are_ignored(self):
        self.assertEqual(_top_faces([det(0.3, facetrack.MIN_FACE_FRAC / 2)]), [])

    def test_overlapping_boxes_count_as_one_person(self):
        faces = _top_faces([det(0.50, 0.20), det(0.55, 0.15)])
        self.assertEqual(len(faces), 1)
        self.assertEqual(faces[0][0], 0.50)                                  # yang lebih besar menang

    def test_owner_follows_nearest_track(self):
        self.assertTrue(_owner_is_a(0.3, 0.25, 0.75))
        self.assertFalse(_owner_is_a(0.7, 0.25, 0.75))
        self.assertTrue(_owner_is_a(0.2, None, None))
        self.assertFalse(_owner_is_a(0.8, None, None))
        self.assertFalse(_owner_is_a(0.8, None, 0.75))                       # hanya B yang dikenal: wajah dekat B = B
        self.assertTrue(_owner_is_a(0.3, 0.25, None))


class SplitTwoTests(unittest.TestCase):
    @staticmethod
    def scan(rows):
        return [(i * 0.4, list(r)) for i, r in enumerate(rows)]

    def test_two_people_get_separate_tracks(self):
        a, b, both = split_two(self.scan([[det(0.25), det(0.75)]] * 10))
        self.assertEqual(both, 10)
        self.assertTrue(all(s and abs(s.cx - 0.25) < 1e-9 for s in a))
        self.assertTrue(all(s and abs(s.cx - 0.75) < 1e-9 for s in b))

    def test_one_entry_per_sample_in_each_track(self):
        a, b, _ = split_two(self.scan([[det(0.25), det(0.75)], [], [det(0.25)]]))
        self.assertEqual((len(a), len(b)), (3, 3))
        self.assertIsNone(a[1])
        self.assertIsNone(b[1])

    def test_lone_visible_face_goes_to_the_nearest_track(self):
        rows = [[det(0.25), det(0.75)]] * 3 + [[det(0.74)]] * 3 + [[det(0.26)]] * 3
        a, b, both = split_two(self.scan(rows))
        self.assertEqual(both, 3)
        self.assertTrue(all(s is None for s in a[3:6]) and all(s for s in b[3:6]))     # B bicara sendirian
        self.assertTrue(all(s is None for s in b[6:9]) and all(s for s in a[6:9]))     # lalu A

    def test_tracks_do_not_swap_when_one_person_leaves(self):
        rows = [[det(0.25), det(0.75)]] * 3 + [[det(0.70)]] * 4
        a, b, _ = split_two(self.scan(rows))
        self.assertTrue(all(s is None for s in a[3:]))
        self.assertTrue(all(abs(s.cx - 0.70) < 1e-9 for s in b[3:]))

    def test_single_person_clip_never_counts_as_both(self):
        _, _, both = split_two(self.scan([[det(0.5)]] * 12))
        self.assertEqual(both, 0)

    def test_empty_scan(self):
        self.assertEqual(split_two([]), ([], [], 0))

    def test_tracks_feed_smooth_path_independently(self):
        a, b, _ = split_two(self.scan([[det(0.25), det(0.75)]] * 12))
        ka = smooth_path(a, 4.8, 0.4, 0.5)
        kb = smooth_path(b, 4.8, 0.4, 0.5)
        self.assertLess(ka[0][1], kb[0][1])


class FacesFromScanTests(unittest.TestCase):
    def test_matches_single_face_picker(self):
        scan = [(0.0, [det(0.3, 0.2)]), (0.4, []), (0.8, [det(0.31, 0.2), det(0.9, 0.05)])]
        out = faces_from_scan(scan)
        self.assertEqual(len(out), 3)
        self.assertIsNone(out[1])
        self.assertAlmostEqual(out[0].cx, 0.3)
        self.assertAlmostEqual(out[2].cx, 0.31)


@unittest.skipUnless(HAS_CV2, "butuh opencv")
class ImageAnalysisTests(unittest.TestCase):
    class Fake:
        def __init__(self, dets):
            self.dets = dets

        def detect(self, frame):
            return list(self.dets)

    def setUp(self):
        self.d = tempfile.TemporaryDirectory()
        self.img = str(Path(self.d.name) / "f.png")
        cv2.imwrite(self.img, np.zeros((90, 160, 3), dtype=np.uint8))

    def tearDown(self):
        self.d.cleanup()

    def test_two_faces_left_to_right_and_largest(self):
        cx, centers = analyze_image_faces(self.img, self.Fake([det(0.8, 0.2), det(0.2, 0.3)]))
        self.assertAlmostEqual(cx, 0.2)
        self.assertEqual(centers, [0.2, 0.8])

    def test_one_face(self):
        cx, centers = analyze_image_faces(self.img, self.Fake([det(0.4)]))
        self.assertAlmostEqual(cx, 0.4)
        self.assertEqual(centers, [0.4])

    def test_no_face(self):
        self.assertEqual(analyze_image_faces(self.img, self.Fake([])), (None, []))

    def test_unreadable_image(self):
        self.assertEqual(analyze_image_faces(str(Path(self.d.name) / "tidak-ada.png"), self.Fake([det(0.5)])), (None, []))


@unittest.skipUnless(HAS_CV2 and HAVE_FFMPEG, "butuh opencv + ffmpeg")
class ScanFacesTests(unittest.TestCase):
    def test_scan_returns_all_detections_per_sample(self):
        class Two:
            def detect(self, frame):
                return [det(0.25), det(0.75)]

        with tempfile.TemporaryDirectory() as d:
            src = Path(d) / "v.mp4"
            subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-f", "lavfi", "-i",
                            "testsrc2=s=320x180:r=10:d=3", "-c:v", "libx264", "-pix_fmt", "yuv420p", str(src)], check=True)
            scan = scan_faces(str(src), sample_fps=2, detector=Two())
            self.assertGreaterEqual(len(scan), 5)
            self.assertTrue(all(len(dets) == 2 for _, dets in scan))
            self.assertEqual([t for t, _ in scan], sorted(t for t, _ in scan))
            _, _, both = split_two(scan)
            self.assertEqual(both, len(scan))

    def test_stop_flag_halts_the_scan(self):
        class One:
            def detect(self, frame):
                return []

        with tempfile.TemporaryDirectory() as d:
            src = Path(d) / "v.mp4"
            subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-f", "lavfi", "-i",
                            "testsrc2=s=320x180:r=10:d=2", "-c:v", "libx264", "-pix_fmt", "yuv420p", str(src)], check=True)
            self.assertEqual(scan_faces(str(src), detector=One(), should_stop=lambda: True), [])


def make_wide(path: Path, w=1280, h=720, seconds=4) -> Path:
    subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-f", "lavfi", "-i", f"testsrc2=s={w}x{h}:r=10:d={seconds}",
                    "-c:v", "libx264", "-pix_fmt", "yuv420p", str(path)], check=True)
    return path


@unittest.skipUnless(HAVE_FFMPEG, "butuh ffmpeg")
class RenderDuoTests(unittest.TestCase):
    """render_clip dengan ffmpeg sungguhan; hanya hasil pindai wajah yang diganti."""

    def opts(self, **layout):
        return RenderOptions(layout=LayoutSpec(mode="duo", **layout), captions=None, resolution=720,
                             encoder="cpu", quality="fast")

    @staticmethod
    def two_people(n=10):
        return [(i * 0.4, [det(0.25), det(0.75)]) for i in range(n)]

    def run_render(self, d, scan, opts=None, w=1280, h=720, words=None):
        src = make_wide(d / "src.mp4", w, h)
        opts = opts or self.opts()
        with mock.patch.object(facetrack, "scan_faces", return_value=scan), \
                mock.patch.object(facetrack, "load_detector", return_value=(object(), "")):
            res = render_clip(src, d / "out.mp4", opts, words or [], d / "work", d)
        return res, opts

    def probe_size(self, path):
        out = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries", "stream=width,height",
                              "-of", "csv=p=0", str(path)], capture_output=True, text=True, check=True).stdout.strip()
        return tuple(int(v) for v in out.split(","))

    def test_two_people_render_with_two_tracks(self):
        with tempfile.TemporaryDirectory() as d:
            d = Path(d)
            res, opts = self.run_render(d, self.two_people())
            self.assertTrue(res["face_tracked"])
            self.assertEqual(self.probe_size(d / "out.mp4"), (720, 1280))
            self.assertTrue(opts.layout.face_keys and opts.layout.face_keys_b)
            self.assertLess(opts.layout.face_keys[0][1], opts.layout.face_keys_b[0][1])
            self.assertFalse(any("Ikuti wajah" in n for n in res["notes"]), res["notes"])

    def test_alone_on_screen_falls_back_to_single_face_layout(self):
        scan = [(i * 0.4, [det(0.4)]) for i in range(10)]
        with tempfile.TemporaryDirectory() as d:
            d = Path(d)
            res, opts = self.run_render(d, scan)
            self.assertTrue((d / "out.mp4").is_file())
            self.assertEqual(self.probe_size(d / "out.mp4"), (720, 1280))
            self.assertTrue(any("Ikuti wajah" in n for n in res["notes"]), res["notes"])
            self.assertTrue(res["face_tracked"])                              # satu wajah tetap diikuti
            self.assertEqual(opts.layout.mode, "duo")                         # opsi pemanggil tidak diubah diam-diam

    def test_two_people_only_briefly_falls_back(self):
        scan = [(i * 0.4, [det(0.25), det(0.75)] if i == 0 else [det(0.25)]) for i in range(10)]
        with tempfile.TemporaryDirectory() as d:
            d = Path(d)
            res, _ = self.run_render(d, scan)
            self.assertTrue(any("Ikuti wajah" in n for n in res["notes"]), res["notes"])

    def test_no_detections_at_all_still_renders(self):
        with tempfile.TemporaryDirectory() as d:
            d = Path(d)
            res, _ = self.run_render(d, [(i * 0.4, []) for i in range(10)])
            self.assertTrue((d / "out.mp4").is_file())
            self.assertFalse(res["face_tracked"])
            self.assertTrue(any("Ikuti wajah" in n for n in res["notes"]), res["notes"])

    def test_source_too_narrow_for_two_panels(self):
        with tempfile.TemporaryDirectory() as d:
            d = Path(d)
            res, _ = self.run_render(d, [], w=720, h=640)                    # rasio 9:8 -> panel selebar sumber
            self.assertTrue((d / "out.mp4").is_file())
            self.assertTrue(any("terlalu sempit" in n for n in res["notes"]), res["notes"])

    def captured_caption_y(self, d, layout_kwargs):
        seen = {}
        real = __import__("backend.render", fromlist=["build_ass"]).build_ass

        def spy(words, duration, style, title):
            seen["y"] = style.y_frac
            return real(words, duration, style, title)

        opts = RenderOptions(layout=LayoutSpec(mode="duo", **layout_kwargs), captions=CaptionStyle(y_frac=0.7),
                             resolution=720, encoder="cpu", quality="fast")
        with mock.patch("backend.render.build_ass", side_effect=spy):
            self.run_render(d, self.two_people(), opts)
        return seen["y"]

    def test_caption_sits_on_the_seam_by_default(self):
        with tempfile.TemporaryDirectory() as d:
            self.assertEqual(self.captured_caption_y(Path(d), {}), 0.5)

    def test_caption_position_is_kept_when_seam_option_is_off(self):
        with tempfile.TemporaryDirectory() as d:
            self.assertEqual(self.captured_caption_y(Path(d), {"seam_caption": False}), 0.7)

    def test_caption_stays_put_when_duo_fell_back(self):
        """Layout kembali ke Ikuti wajah: caption tidak boleh nyasar ke 'sambungan' yang tak ada."""
        seen = {}
        real = __import__("backend.render", fromlist=["build_ass"]).build_ass

        def spy(words, duration, style, title):
            seen["y"] = style.y_frac
            return real(words, duration, style, title)

        opts = RenderOptions(layout=LayoutSpec(mode="duo"), captions=CaptionStyle(y_frac=0.7), resolution=720,
                             encoder="cpu", quality="fast")
        with tempfile.TemporaryDirectory() as d, mock.patch("backend.render.build_ass", side_effect=spy):
            self.run_render(Path(d), [(i * 0.4, [det(0.4)]) for i in range(10)], opts)
        self.assertEqual(seen["y"], 0.7)


if __name__ == "__main__":
    unittest.main()

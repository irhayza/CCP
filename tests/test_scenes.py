"""Deteksi cut (scenes.py) dan penyambungannya ke renderer, dengan ffmpeg sungguhan."""
import shutil, subprocess, tempfile, unittest
from pathlib import Path
from unittest import mock

from backend import facetrack, scenes
from backend.facetrack import FaceSample
from backend.layouts import LayoutSpec
from backend.procutil import Cancelled
from backend.render import RenderOptions, render_clip

HAVE_FFMPEG = shutil.which("ffmpeg") is not None
try:
    import cv2
    HAS_HAAR = hasattr(cv2, "CascadeClassifier")     # OpenCV 5.x tidak punya; requirements.txt mematok <5.0
except ImportError:                                  # pragma: no cover
    HAS_HAAR = False


def make_cut_video(path: Path, w=640, h=360) -> Path:
    """2 dtk testsrc + 2 dtk merah + 1,5 dtk testsrc2 -> cut di 2,0 dan 4,0."""
    subprocess.run(["ffmpeg", "-y", "-loglevel", "error",
                    "-f", "lavfi", "-i", f"testsrc=duration=2:size={w}x{h}:rate=15",
                    "-f", "lavfi", "-i", f"color=c=red:duration=2:size={w}x{h}:rate=15",
                    "-f", "lavfi", "-i", f"testsrc2=duration=1.5:size={w}x{h}:rate=15",
                    "-filter_complex", "[0:v][1:v][2:v]concat=n=3:v=1:a=0[v]", "-map", "[v]",
                    "-c:v", "libx264", "-pix_fmt", "yuv420p", str(path)], check=True)
    return path


class ParseTests(unittest.TestCase):
    def test_parse_cut_times(self):
        lines = ["frame:0    pts:30      pts_time:2",
                 "lavfi.scene_score=0.512345",
                 "frame:1    pts:60      pts_time:4.0333",
                 "baris lain"]
        self.assertEqual(scenes.parse_cut_times(lines), [2.0, 4.0333])

    def test_parse_ignores_noise(self):
        self.assertEqual(scenes.parse_cut_times(["", "pts_time:", "lavfi.scene_score=0.9"]), [])

    def test_merge_close_sorts_and_drops_flashes(self):
        # jarak dihitung dari cut terakhir yang DIPERTAHANKAN: 2.2 dan 2.5 jatuh di dalam 0.6 dtk dari 2.0
        self.assertEqual(scenes.merge_close([4.0, 2.0, 2.2, 2.5, 7.0], 0.6), [2.0, 4.0, 7.0])


@unittest.skipUnless(HAVE_FFMPEG, "ffmpeg tidak ada")
class DetectCutsTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._tmp = tempfile.TemporaryDirectory()
        cls.dir = Path(cls._tmp.name)
        cls.video = make_cut_video(cls.dir / "cuts.mp4")

    @classmethod
    def tearDownClass(cls):
        cls._tmp.cleanup()

    def test_finds_both_cuts(self):
        cuts = scenes.detect_cuts(self.video)
        self.assertEqual(len(cuts), 2, cuts)
        self.assertAlmostEqual(cuts[0], 2.0, delta=0.15)
        self.assertAlmostEqual(cuts[1], 4.0, delta=0.15)

    def test_static_video_has_no_cuts(self):
        still = self.dir / "still.mp4"
        subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-f", "lavfi", "-i", "color=c=blue:duration=2:size=320x180:rate=15",
                        "-c:v", "libx264", "-pix_fmt", "yuv420p", str(still)], check=True)
        self.assertEqual(scenes.detect_cuts(still), [])

    def test_high_threshold_finds_nothing(self):
        self.assertEqual(scenes.detect_cuts(self.video, threshold=0.999), [])

    def test_failure_returns_empty_instead_of_raising(self):
        self.assertEqual(scenes.detect_cuts(self.dir / "tidak-ada.mp4"), [])
        self.assertEqual(scenes.detect_cuts(self.video, ffmpeg="ffmpeg-tidak-ada"), [])

    def test_cancellation_propagates(self):
        with self.assertRaises(Cancelled):
            scenes.detect_cuts(self.video, is_cancelled=lambda: True)


@unittest.skipUnless(HAVE_FFMPEG, "ffmpeg tidak ada")
class RenderWiringTests(unittest.TestCase):
    """render_clip harus memberi smooth_path daftar cut yang terdeteksi."""

    def test_cuts_reach_smooth_path_and_render_succeeds(self):
        with tempfile.TemporaryDirectory() as d:
            d = Path(d)
            src = make_cut_video(d / "src.mp4")
            # wajah palsu: di kiri sebelum cut 2,0 dan di kanan sesudahnya
            fake = [FaceSample(t * 0.4, 0.25 if t * 0.4 < 2 else 0.75, 0.1) for t in range(12)]
            seen = {}
            real = facetrack.smooth_path

            def spy(*a, **kw):
                seen["cuts"] = kw.get("cuts")
                return real(*a, **kw)

            opts = RenderOptions(layout=LayoutSpec(mode="fill_face"), captions=None, resolution=720,
                                 encoder="cpu", quality="fast")
            with mock.patch.object(facetrack, "detect_faces", return_value=fake), \
                    mock.patch.object(facetrack, "smooth_path", side_effect=spy):
                res = render_clip(src, d / "out.mp4", opts, [], d / "work", d)
            self.assertTrue(res["face_tracked"])
            self.assertTrue((d / "out.mp4").is_file())
            self.assertEqual(len(seen["cuts"]), 2, seen)
            self.assertAlmostEqual(seen["cuts"][0], 2.0, delta=0.15)

    @unittest.skipUnless(HAS_HAAR, "jatuh ke Haar hanya bisa diuji di OpenCV 4.x (dengan CascadeClassifier)")
    def test_detector_fallback_note_is_reported(self):
        with tempfile.TemporaryDirectory() as d:
            d = Path(d)
            src = make_cut_video(d / "src.mp4")
            opts = RenderOptions(layout=LayoutSpec(mode="fill_face"), captions=None, resolution=720,
                                 encoder="cpu", quality="fast")
            res = render_clip(src, d / "out.mp4", opts, [], d / "work", d,
                              face_detector="yunet", models_dir=d / "tidak-ada")
            self.assertTrue(any("YuNet" in n for n in res["notes"]), res["notes"])

    def test_no_detector_at_all_explains_how_to_fix(self):
        """OpenCV 5 tanpa model YuNet: render tetap jadi (crop tengah) dan catatannya menunjuk solusi."""
        with tempfile.TemporaryDirectory() as d:
            d = Path(d)
            src = make_cut_video(d / "src.mp4")
            opts = RenderOptions(layout=LayoutSpec(mode="fill_face"), captions=None, resolution=720,
                                 encoder="cpu", quality="fast")
            boom = RuntimeError("OpenCV ini tidak menyertakan CascadeClassifier")
            with mock.patch.object(facetrack, "load_detector", side_effect=boom), \
                    mock.patch.object(facetrack, "detect_faces", return_value=[]):
                res = render_clip(src, d / "out.mp4", opts, [], d / "work", d)
            self.assertTrue((d / "out.mp4").is_file())
            self.assertFalse(res["face_tracked"])
            self.assertTrue(any("python -m backend.models" in n and "CascadeClassifier" in n for n in res["notes"]),
                            res["notes"])


if __name__ == "__main__":
    unittest.main()

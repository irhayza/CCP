import math, tempfile, unittest
from pathlib import Path
from unittest import mock

from backend.config import load_settings
from backend.facetrack import (MIN_FACE_FRAC, FaceSample, HaarDetector, YuNetDetector, _rdp, find_yunet_model,
                               load_detector, pick_face, smooth_path, static_hold, x_expression)

try:
    import cv2
    import numpy as np
    HAS_CV2 = True
except ImportError:                                    # pragma: no cover
    HAS_CV2 = False
HAS_HAAR = HAS_CV2 and hasattr(cv2, "CascadeClassifier")   # OpenCV 5.x tidak punya; requirements.txt mematok <5.0

CROP = 0.316          # 9:16 dari 16:9


def samples(fn, dur=10.0, dt=0.4, size=0.1):
    out, t = [], 0.0
    while t < dur:
        v = fn(t)
        out.append(None if v is None else FaceSample(t, v, size))
        t += dt
    return out


def pos_at(keys, t):
    for (t0, x0), (t1, x1) in zip(keys, keys[1:]):
        if t0 <= t <= t1:
            return x0 if t1 == t0 else x0 + (x1 - x0) * (t - t0) / (t1 - t0)
    return keys[-1][1]


class SmoothTests(unittest.TestCase):
    def test_no_faces_centers(self):
        keys = smooth_path([None] * 10, 5.0, 0.5, CROP)
        self.assertEqual(keys[0][1], 0.5)
        self.assertEqual(keys[-1][0], 5.0)

    def test_jitter_inside_deadzone_is_ignored(self):
        keys = smooth_path(samples(lambda t: 0.5 + 0.02 * math.sin(t * 9)), 10, 0.4, CROP)
        self.assertLessEqual(len(keys), 3)
        self.assertLess(max(x for _, x in keys) - min(x for _, x in keys), 0.01)

    def test_outlier_rejected(self):
        def f(t):
            return 0.9 if abs(t - 4.0) < 0.1 else 0.4
        keys = smooth_path(samples(f), 10, 0.4, CROP)
        self.assertLess(max(x for _, x in keys), 0.45)

    def test_gradual_move_is_speed_limited(self):
        keys = smooth_path(samples(lambda t: 0.3 if t < 2 else 0.5), 10, 0.4, CROP, snap_threshold=0.28)
        for (t0, x0), (t1, x1) in zip(keys, keys[1:]):
            if t1 > t0:
                self.assertLessEqual(abs(x1 - x0) / (t1 - t0), 0.31)
        self.assertAlmostEqual(pos_at(keys, 9.5), 0.5, delta=0.05)

    def test_scene_cut_jumps_instead_of_panning(self):
        keys = smooth_path(samples(lambda t: 0.25 if t < 4 else 0.75), 10, 0.4, CROP)
        self.assertGreater(pos_at(keys, 5.0), 0.7)             # jump-cut: sudah di sisi baru <1 dtk setelahnya
        self.assertLess(pos_at(keys, 3.5), 0.3)

    def test_crop_stays_inside_frame(self):
        keys = smooth_path(samples(lambda t: 0.02 if t < 5 else 0.98), 10, 0.4, CROP)
        half = CROP / 2
        for _, x in keys:
            self.assertGreaterEqual(x, half - 1e-6)
            self.assertLessEqual(x, 1 - half + 1e-6)

    def test_keyframe_cap(self):
        keys = smooth_path(samples(lambda t: 0.5 + 0.2 * math.sin(t), 60, 0.4), 60, 0.4, CROP, max_keyframes=20)
        self.assertLessEqual(len(keys), 20)

    def test_rdp_keeps_endpoints(self):
        pts = [(i, i * 0.1) for i in range(10)]
        self.assertEqual(_rdp(pts, 0.001), [pts[0], pts[-1]])


def span(keys):
    return max(x for _, x in keys) - min(x for _, x in keys)


class StaticHoldTests(unittest.TestCase):
    """Ide AutoFlip: wajah yang selalu muat di satu crop -> kamera diam total; selebihnya tetap diikuti."""

    @staticmethod
    def drift(t):                      # 0.45 -> 0.55 perlahan: melewati deadzone 0.06, tetapi muat di crop
        return 0.45 + 0.10 * t / 10.0

    def test_unit_decision(self):
        args = (0.158, 0.158, 0.842, 0.02)               # half, lo, hi, margin (crop 9:16 dari 16:9)
        self.assertAlmostEqual(static_hold([0.42, 0.58], 0.1, *args), 0.5)
        self.assertIsNone(static_hold([0.30, 0.70], 0.1, *args))      # rentang terlalu lebar
        self.assertIsNone(static_hold([], 0.1, *args))
        self.assertIsNone(static_hold([0.5], 0.35, *args))            # wajah lebih lebar dari crop

    def test_small_drift_is_held_perfectly_still(self):
        keys = smooth_path(samples(self.drift), 10, 0.4, CROP)
        self.assertEqual(len({x for _, x in keys}), 1, keys)
        self.assertAlmostEqual(keys[0][1], 0.5, delta=0.01)           # tengah rentang 0.45..0.55

    def test_control_same_drift_pans_when_hold_is_off(self):
        """Kontrol: tanpa fitur ini, drift yang sama membuat kamera panning. Tes lain jadi bermakna."""
        keys = smooth_path(samples(self.drift), 10, 0.4, CROP, hold_static=False)
        self.assertGreater(span(keys), 0.02)

    def test_large_motion_is_still_tracked(self):
        keys = smooth_path(samples(lambda t: 0.3 + 0.35 * t / 10.0), 10, 0.4, CROP)
        self.assertGreater(span(keys), 0.15)

    def test_close_up_face_is_followed_even_for_small_motion(self):
        keys = smooth_path(samples(self.drift, size=0.25), 10, 0.4, CROP)      # wajah hampir selebar crop
        self.assertGreater(span(keys), 0.02)

    def test_each_scene_is_decided_separately(self):
        def f(t):                                                   # adegan 1 diam-ish, adegan 2 bergerak jauh
            return 0.30 + 0.02 * math.sin(t) if t < 5 else 0.3 + 0.4 * (t - 5) / 5
        keys = smooth_path(samples(f), 10, 0.4, CROP, cuts=[5.0])
        scene1 = [pos_at(keys, t) for t in (0, 1, 2.5, 4.8)]
        self.assertLess(max(scene1) - min(scene1), 1e-6)            # adegan 1 diam penuh
        self.assertGreater(pos_at(keys, 9.8) - pos_at(keys, 5.2), 0.1)         # adegan 2 diikuti

    def test_two_static_scenes_jump_once_at_the_cut(self):
        keys = smooth_path(samples(lambda t: 0.30 if t < 5 else 0.70), 10, 0.4, CROP, cuts=[5.0])
        for t in (0, 2, 4.8):
            self.assertAlmostEqual(pos_at(keys, t), 0.30, delta=0.005)
        for t in (5.2, 7, 10):
            self.assertAlmostEqual(pos_at(keys, t), 0.70, delta=0.005)

    def test_near_frame_edge_the_face_stays_inside_the_clamped_crop(self):
        keys = smooth_path(samples(lambda t: 0.90 + 0.01 * math.sin(t)), 10, 0.4, CROP)
        pos = keys[0][1]
        self.assertLessEqual(pos, 1 - CROP / 2 + 1e-9)              # crop tidak keluar frame
        for v in (0.89, 0.91):
            self.assertLessEqual(abs(v - pos) + 0.05, CROP / 2 + 1e-9)       # wajah (selebar 0.1) tetap di dalam

    def test_face_always_stays_inside_the_crop(self):
        """Sifat inti, untuk gerakan kecil (diam) maupun besar (diikuti): tepi wajah tidak pernah melewati tepi crop."""
        face_half = 0.05                                            # wajah selebar 0.1
        for amp in (0.02, 0.05, 0.08, 0.12, 0.2):
            def fn(t, amp=amp):
                return 0.5 + amp * math.sin(t * 0.7)
            keys = smooth_path(samples(fn, dur=20), 20, 0.4, CROP)
            for k in range(0, 200, 3):
                t = k * 0.1
                self.assertLessEqual(abs(fn(t) - pos_at(keys, t)) + face_half, CROP / 2, (amp, round(t, 1)))


class ExprTests(unittest.TestCase):
    def test_static(self):
        self.assertEqual(x_expression([(0, 0.5), (5, 0.5)], 1920, 608), "656.00")

    def test_clamped_and_piecewise(self):
        e = x_expression([(0, 0.0), (1, 0.0), (2, 1.0), (5, 1.0)], 1920, 608)
        self.assertTrue(e.startswith("0.00"))
        self.assertIn("clip((t-1.000)/1.000,0,1)", e)
        self.assertIn("+1312.00*", e)                           # 1920-608 = rentang maksimum, tidak melebihi


class CutTests(unittest.TestCase):
    """smooth_path dengan daftar cut sungguhan (mis. dari scenes.detect_cuts)."""

    @staticmethod
    def jump(t):
        return 0.25 if t < 4 else 0.75

    def test_known_cut_snaps_immediately(self):
        without = smooth_path(samples(self.jump), 10, 0.4, CROP)
        with_cut = smooth_path(samples(self.jump), 10, 0.4, CROP, cuts=[4.0])
        # tanpa info cut kamera masih membingkai posisi lama sesaat setelah cut (menunggu lompatan terkonfirmasi)
        self.assertLess(pos_at(without, 4.2), 0.4)
        # dengan info cut ia sudah di posisi adegan baru, dan sebelum cut tidak bergeser lebih awal
        self.assertGreater(pos_at(with_cut, 4.2), 0.7)
        self.assertLess(pos_at(with_cut, 3.8), 0.3)

    def test_small_jump_at_cut_does_not_pan(self):
        # lompatan 0.2 < snap_threshold: tanpa info cut kamera meluncur ~0.7 dtk; dengan cut langsung pindah
        f = lambda t: 0.4 if t < 4 else 0.6
        without = smooth_path(samples(f), 10, 0.4, CROP)
        with_cut = smooth_path(samples(f), 10, 0.4, CROP, cuts=[4.0])
        self.assertLess(pos_at(without, 4.3), 0.55)
        self.assertGreater(pos_at(with_cut, 4.3), 0.59)

    def test_cuts_outside_clip_are_ignored(self):
        base = smooth_path(samples(self.jump), 10, 0.4, CROP)
        self.assertEqual(smooth_path(samples(self.jump), 10, 0.4, CROP, cuts=[-1.0, 0.0, 10.0, 99.0]), base)

    def test_same_position_across_cut_stays_still(self):
        keys = smooth_path(samples(lambda t: 0.5), 10, 0.4, CROP, cuts=[3.0, 6.0])
        self.assertLessEqual(len(keys), 3)
        self.assertLess(max(x for _, x in keys) - min(x for _, x in keys), 0.01)

    def test_scene_without_detection_holds_position(self):
        keys = smooth_path(samples(lambda t: 0.3 if t < 4 else None), 10, 0.4, CROP, cuts=[4.0])
        self.assertAlmostEqual(pos_at(keys, 9.0), 0.3, delta=0.02)

    def test_no_faces_with_cuts_is_centered(self):
        keys = smooth_path([None] * 10, 5.0, 0.5, CROP, cuts=[2.0])
        self.assertEqual(keys[0][1], 0.5)

    def test_many_cuts_respect_keyframe_cap_and_bounds(self):
        f = lambda t: 0.1 if int(t) % 2 == 0 else 0.9
        keys = smooth_path(samples(f, 30), 30, 0.4, CROP, max_keyframes=25, cuts=[float(i) for i in range(1, 30)])
        self.assertLessEqual(len(keys), 25)
        for _, x in keys:
            self.assertGreaterEqual(x, CROP / 2 - 1e-6)
            self.assertLessEqual(x, 1 - CROP / 2 + 1e-6)


class PickFaceTests(unittest.TestCase):
    def test_empty(self):
        self.assertIsNone(pick_face([], None))

    def test_prefers_larger_face(self):
        self.assertEqual(pick_face([(0.2, 0.10, 1.0), (0.7, 0.20, 1.0)], None)[0], 0.7)

    def test_prefers_continuity_when_sizes_similar(self):
        self.assertEqual(pick_face([(0.2, 0.15, 1.0), (0.7, 0.18, 1.0)], prev_cx=0.2)[0], 0.2)

    def test_ignores_tiny_background_faces(self):
        self.assertIsNone(pick_face([(0.5, MIN_FACE_FRAC / 2, 1.0)], None))

    def test_confidence_matters(self):
        self.assertEqual(pick_face([(0.3, 0.10, 0.95), (0.6, 0.12, 0.30)], None)[0], 0.3)


@unittest.skipUnless(HAS_CV2, "OpenCV tidak terpasang")
class YuNetWrapperTests(unittest.TestCase):
    """Logika pembungkus diuji dengan FaceDetectorYN palsu: file model asli tidak diperlukan."""

    @staticmethod
    def fake(rows):
        calls = {}

        class Fake:
            @staticmethod
            def create(model, config, size, score, nms, top_k):
                calls["create"] = (model, size, score)
                return Fake()

            def setInputSize(self, size):
                calls["size"] = size

            def detect(self, frame):
                calls["frame"] = frame.shape
                return 1, (np.array(rows, dtype=np.float32) if rows else None)

        return Fake, calls

    @staticmethod
    def row(x, w, score=0.9):
        return [x, 50, w, w] + [0] * 10 + [score]

    def test_parses_rows_and_downscales(self):
        Fake, calls = self.fake([self.row(100, 80, 0.9)])
        with mock.patch.object(cv2, "FaceDetectorYN", Fake, create=True):
            det = YuNetDetector("model.onnx")
            out = det.detect(np.zeros((720, 1280, 3), np.uint8))
        self.assertEqual(calls["frame"][:2], (360, 640))           # diperkecil ke lebar 640
        self.assertEqual(calls["size"], (640, 360))
        (cx, fw, conf), = out
        self.assertAlmostEqual(cx, (100 + 40) / 640, places=4)
        self.assertAlmostEqual(fw, 80 / 640, places=4)
        self.assertAlmostEqual(conf, 0.9, places=3)

    def test_no_faces_returns_empty(self):
        Fake, _ = self.fake(None)
        with mock.patch.object(cv2, "FaceDetectorYN", Fake, create=True):
            self.assertEqual(YuNetDetector("model.onnx").detect(np.zeros((360, 640, 3), np.uint8)), [])

    def test_boxes_leaving_the_frame_are_clamped(self):
        Fake, _ = self.fake([self.row(600, 100)])
        with mock.patch.object(cv2, "FaceDetectorYN", Fake, create=True):
            (cx, fw, _), = YuNetDetector("model.onnx").detect(np.zeros((360, 640, 3), np.uint8))
        self.assertLessEqual(cx, 1.0)
        self.assertLessEqual(fw, 1.0)

    def test_missing_api_raises_clear_error(self):
        fake_cv2 = mock.Mock(spec=[])                               # objek tanpa atribut FaceDetectorYN
        with mock.patch.dict("sys.modules", {"cv2": fake_cv2}):
            with self.assertRaises(RuntimeError):
                YuNetDetector("model.onnx")


@unittest.skipUnless(HAS_HAAR, "OpenCV 4.x (dengan CascadeClassifier) tidak terpasang")
class LoadDetectorTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)

    def test_auto_without_model_uses_haar_silently(self):
        det, note = load_detector("auto", models_dir=self.dir)
        self.assertIsInstance(det, HaarDetector)
        self.assertIsNone(note)

    def test_yunet_requested_but_missing_explains(self):
        det, note = load_detector("yunet", models_dir=self.dir)
        self.assertIsInstance(det, HaarDetector)
        self.assertIn("YuNet", note)

    def test_broken_model_falls_back_with_note(self):
        (self.dir / "face_detection_yunet_2023mar.onnx").write_bytes(b"bukan model onnx")
        det, note = load_detector("auto", models_dir=self.dir)
        self.assertIsInstance(det, HaarDetector)
        self.assertTrue(note and note.startswith("YuNet tidak bisa dimuat"), note)

    def test_haar_mode_ignores_model_file(self):
        (self.dir / "face_detection_yunet_2023mar.onnx").write_bytes(b"bukan model onnx")
        det, note = load_detector("haar", models_dir=self.dir)
        self.assertIsInstance(det, HaarDetector)
        self.assertIsNone(note)

    def test_unknown_mode_behaves_like_auto(self):
        det, _ = load_detector("???", models_dir=self.dir)
        self.assertEqual(det.name, "haar")

    def test_find_model_prefers_newest_and_honors_explicit_path(self):
        a = self.dir / "face_detection_yunet_2022mar.onnx"
        b = self.dir / "face_detection_yunet_2023mar.onnx"
        a.write_bytes(b"x")
        b.write_bytes(b"x")
        self.assertEqual(find_yunet_model(None, self.dir), b)
        self.assertEqual(find_yunet_model(str(a), self.dir), a)
        self.assertIsNone(find_yunet_model(str(self.dir / "tidak-ada.onnx"), self.dir))
        self.assertIsNone(find_yunet_model(None, self.dir / "folder-tidak-ada"))

    def test_loaded_yunet_is_used_when_model_loads(self):
        (self.dir / "face_detection_yunet_2023mar.onnx").write_bytes(b"x")
        Fake, _ = YuNetWrapperTests.fake([])
        with mock.patch.object(cv2, "FaceDetectorYN", Fake, create=True):
            det, note = load_detector("auto", models_dir=self.dir)
        self.assertEqual(det.name, "yunet")
        self.assertIsNone(note)


class SettingsTests(unittest.TestCase):
    def test_defaults_and_validation(self):
        with tempfile.TemporaryDirectory() as d:
            s = load_settings({"DATA_DIR": d})
            self.assertEqual((s.face_detector, s.yunet_model), ("auto", ""))
            self.assertEqual(s.models_dir, Path(d).resolve() / "models")
            self.assertEqual(load_settings({"DATA_DIR": d, "FACE_DETECTOR": " YuNet "}).face_detector, "yunet")
            self.assertEqual(load_settings({"DATA_DIR": d, "FACE_DETECTOR": "ngawur"}).face_detector, "auto")
            self.assertEqual(load_settings({"DATA_DIR": d, "YUNET_MODEL": "/x/m.onnx"}).yunet_model, "/x/m.onnx")


if __name__ == "__main__":
    unittest.main()

import math, unittest
from backend.facetrack import FaceSample, smooth_path, x_expression, _rdp

CROP = 0.316          # 9:16 dari 16:9


def samples(fn, dur=10.0, dt=0.4):
    out, t = [], 0.0
    while t < dur:
        v = fn(t)
        out.append(None if v is None else FaceSample(t, v, 0.1))
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


class ExprTests(unittest.TestCase):
    def test_static(self):
        self.assertEqual(x_expression([(0, 0.5), (5, 0.5)], 1920, 608), "656.00")

    def test_clamped_and_piecewise(self):
        e = x_expression([(0, 0.0), (1, 0.0), (2, 1.0), (5, 1.0)], 1920, 608)
        self.assertTrue(e.startswith("0.00"))
        self.assertIn("clip((t-1.000)/1.000,0,1)", e)
        self.assertIn("+1312.00*", e)                           # 1920-608 = rentang maksimum, tidak melebihi


if __name__ == "__main__":
    unittest.main()

"""Face tracking untuk reframe 9:16.

Detektor (OpenCV Haar) hanya menghasilkan sampel mentah yang berisik. Kualitas gerak kamera
ditentukan oleh `smooth_path`: median filter, deadzone, batas kecepatan pan, dan jump-cut saat
pergantian adegan. Hasilnya disederhanakan jadi beberapa keyframe lalu dijadikan ekspresi ffmpeg."""
from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Callable


@dataclass
class FaceSample:
    t: float
    cx: float      # pusat wajah, 0..1 dari lebar frame
    size: float    # lebar wajah / lebar frame


# --------------------------------------------------------------------------- deteksi
def detect_faces(video_path: str, sample_fps: float = 2.5, max_samples: int = 600,
                 should_stop: Callable[[], bool] | None = None) -> list[FaceSample | None]:
    """Satu entri per titik sampel waktu; None bila tidak ada wajah. Butuh opencv-python."""
    import cv2  # impor lambat: modul lain tetap bisa dipakai tanpa OpenCV

    cap = cv2.VideoCapture(video_path)
    if not cap.isOpened():
        return []
    fps = cap.get(cv2.CAP_PROP_FPS) or 25.0
    total = int(cap.get(cv2.CAP_PROP_FRAME_COUNT) or 0)
    step = max(1, int(round(fps / max(0.5, sample_fps))))
    if total and total / step > max_samples:
        step = int(math.ceil(total / max_samples))

    cascades = []
    for name in ("haarcascade_frontalface_alt2.xml", "haarcascade_frontalface_default.xml"):
        c = cv2.CascadeClassifier(cv2.data.haarcascades + name)
        if not c.empty():
            cascades.append(c)
    if not cascades:
        cap.release()
        return []

    out: list[FaceSample | None] = []
    prev_cx: float | None = None
    idx = 0
    while True:
        if should_stop and should_stop():
            break
        if not cap.grab():
            break
        if idx % step == 0:
            ok, frame = cap.retrieve()
            if ok:
                h, w = frame.shape[:2]
                scale = 480.0 / w if w > 480 else 1.0
                small = cv2.resize(frame, (int(w * scale), int(h * scale))) if scale != 1.0 else frame
                gray = cv2.equalizeHist(cv2.cvtColor(small, cv2.COLOR_BGR2GRAY))
                sw = gray.shape[1]
                found = []
                for c in cascades:
                    found = c.detectMultiScale(gray, scaleFactor=1.15, minNeighbors=5,
                                               minSize=(max(24, sw // 16), max(24, sw // 16)))
                    if len(found):
                        break
                t = idx / fps
                if len(found):
                    best, best_score = None, -1.0
                    for (x, y, fw, fh) in found:
                        cx = (x + fw / 2) / sw
                        area = (fw / sw) ** 2
                        # utamakan wajah besar, dan wajah yang dekat dengan posisi sebelumnya (kontinuitas)
                        cont = 1.0 if prev_cx is None else 1.0 - 0.7 * min(1.0, abs(cx - prev_cx) / 0.5)
                        score = area * cont
                        if score > best_score:
                            best, best_score = FaceSample(t, cx, fw / sw), score
                    prev_cx = best.cx  # type: ignore[union-attr]
                    out.append(best)
                else:
                    out.append(FaceSample(t, float("nan"), 0.0))
        idx += 1
    cap.release()
    return [None if (s is None or math.isnan(s.cx)) else s for s in out]


# --------------------------------------------------------------------------- smoothing
def _median(vals: list[float]) -> float:
    s = sorted(vals)
    return s[len(s) // 2]


def _rdp(points: list[tuple[float, float]], eps: float) -> list[tuple[float, float]]:
    """Ramer–Douglas–Peucker pada (t, x) dengan jarak vertikal terhadap interpolasi linear."""
    if len(points) < 3:
        return points
    (t0, x0), (t1, x1) = points[0], points[-1]
    worst, wi = -1.0, 0
    for i in range(1, len(points) - 1):
        t, x = points[i]
        pred = x0 if t1 == t0 else x0 + (x1 - x0) * (t - t0) / (t1 - t0)
        d = abs(x - pred)
        if d > worst:
            worst, wi = d, i
    if worst <= eps:
        return [points[0], points[-1]]
    left = _rdp(points[: wi + 1], eps)
    right = _rdp(points[wi:], eps)
    return left[:-1] + right


def smooth_path(samples: list[FaceSample | None], duration: float, sample_dt: float, crop_w_frac: float,
                *, deadzone: float = 0.06, max_speed: float = 0.30, snap_threshold: float = 0.28,
                grid_dt: float = 0.1, max_keyframes: int = 40) -> list[tuple[float, float]]:
    """Ubah sampel wajah mentah menjadi keyframe (t, pusat_crop 0..1) yang halus.

    deadzone      : abaikan perbedaan target < nilai ini (fraksi lebar frame) agar kamera tidak bergetar
    max_speed     : kecepatan pan maksimum (fraksi lebar frame per detik)
    snap_threshold: bila target melompat lebih jauh dari ini dan bertahan >=0.3 dtk -> jump-cut
    """
    duration = max(duration, grid_dt)
    half = crop_w_frac / 2.0
    lo, hi = half, 1.0 - half
    center = min(max(0.5, lo), hi)
    valid = [s for s in samples if s is not None]
    if not valid:
        return [(0.0, center), (duration, center)]

    # 0) median 3-sampel pada sampel mentah: satu deteksi salah (outlier) tidak boleh menggerakkan kamera,
    #    sedangkan pindah posisi yang bertahan >= 2 sampel (pergantian adegan) tetap lolos.
    ordered = sorted(valid, key=lambda s: s.t)
    cxs = [s.cx for s in ordered]
    valid = [FaceSample(s.t, _median(cxs[max(0, i - 1): i + 2]), s.size) for i, s in enumerate(ordered)]

    n = int(math.ceil(duration / grid_dt)) + 1
    times = [i * grid_dt for i in range(n)]
    # 1) target per titik grid: sampel valid terdekat dalam 1.2 dtk, lalu isi lubang dengan nilai terakhir
    raw: list[float | None] = []
    for t in times:
        near = min(valid, key=lambda s: abs(s.t - t))
        raw.append(near.cx if abs(near.t - t) <= max(1.2, sample_dt * 1.5) else None)
    first = next(v for v in raw if v is not None)
    filled: list[float] = []
    last = first
    for v in raw:
        last = v if v is not None else last
        filled.append(last)
    # 2) median filter (jendela 5) membuang deteksi liar
    med = [_median(filled[max(0, i - 2): i + 3]) for i in range(len(filled))]
    # 3) kontroler: deadzone + histeresis + batas kecepatan + jump-cut
    pos = min(max(med[0], lo), hi)
    moving = False
    far_count = 0
    need = max(1, int(round(0.3 / grid_dt)))
    path: list[float] = []
    for tgt in med:
        tgt = min(max(tgt, lo), hi)
        err = tgt - pos
        if abs(err) > snap_threshold:
            far_count += 1
            if far_count >= need:
                pos, moving, far_count = tgt, False, 0
                path.append(pos)
                continue
        else:
            far_count = 0
        if not moving and abs(err) > deadzone:
            moving = True
        if moving:
            step = max(-max_speed * grid_dt, min(max_speed * grid_dt, err * 0.35))
            pos += step
            if abs(tgt - pos) < deadzone / 3:
                moving = False
        path.append(pos)
    # 4) sederhanakan jadi keyframe
    pts = list(zip(times, path))
    eps = 0.004
    keys = _rdp(pts, eps)
    while len(keys) > max_keyframes:
        eps *= 1.6
        keys = _rdp(pts, eps)
    if keys[-1][0] < duration:
        keys.append((duration, keys[-1][1]))
    return [(round(t, 3), round(x, 5)) for t, x in keys]


def x_expression(keys: list[tuple[float, float]], src_w: int, crop_w: int) -> str:
    """Ekspresi ffmpeg untuk crop `x` (piksel, linear antar-keyframe, bebas nested-if).

    x(t) = x0 + sum_i dx_i * clip((t - t_i) / dt_i, 0, 1)
    """
    def px(c: float) -> float:
        return min(max(c * src_w - crop_w / 2.0, 0.0), float(src_w - crop_w))

    xs = [px(c) for _, c in keys]
    terms = [f"{xs[0]:.2f}"]
    for i in range(len(keys) - 1):
        dt = keys[i + 1][0] - keys[i][0]
        dx = xs[i + 1] - xs[i]
        if abs(dx) < 0.5 or dt <= 0:
            continue
        terms.append(f"{dx:+.2f}*clip((t-{keys[i][0]:.3f})/{dt:.3f},0,1)")
    return "".join(terms)


def sample_interval(samples: list[FaceSample | None], duration: float) -> float:
    return max(0.1, duration / max(1, len(samples)))


_cascade_cache: list | None = None


def _get_cascades():
    """Muat CascadeClassifier sekali (lazy singleton) agar tidak re-load XML dari disk setiap panggilan."""
    global _cascade_cache
    if _cascade_cache is not None:
        return _cascade_cache
    import cv2
    cascades = []
    for name in ("haarcascade_frontalface_alt2.xml", "haarcascade_frontalface_default.xml"):
        c = cv2.CascadeClassifier(cv2.data.haarcascades + name)
        if not c.empty():
            cascades.append(c)
    _cascade_cache = cascades
    return cascades


def detect_face_in_image(path: str) -> float | None:
    """Pusat horizontal (0..1) wajah terbesar pada satu gambar, atau None. Dipakai untuk preview studio."""
    try:
        import cv2
    except ImportError:
        return None
    img = cv2.imread(path)
    if img is None:
        return None
    h, w = img.shape[:2]
    gray = cv2.equalizeHist(cv2.cvtColor(img, cv2.COLOR_BGR2GRAY))
    for c in _get_cascades():
        found = c.detectMultiScale(gray, 1.15, 5, minSize=(max(24, w // 16), max(24, w // 16)))
        if len(found):
            x, _, fw, _ = max(found, key=lambda f: f[2] * f[3])
            return float((x + fw / 2) / w)
    return None

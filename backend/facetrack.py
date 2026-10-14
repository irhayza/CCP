"""Face tracking untuk reframe 9:16.

Detektor (YuNet bila modelnya tersedia, selain itu Haar bawaan OpenCV) hanya menghasilkan sampel mentah
yang berisik. Kualitas gerak kamera ditentukan oleh `smooth_path`: median filter, deadzone, batas
kecepatan pan, dan jump-cut saat pergantian adegan (dari daftar cut yang diberikan, bukan ditebak dari
lompatan posisi wajah). Hasilnya disederhanakan jadi beberapa keyframe lalu dijadikan ekspresi ffmpeg."""
from __future__ import annotations

import bisect
import math
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Sequence


@dataclass
class FaceSample:
    t: float
    cx: float      # pusat wajah, 0..1 dari lebar frame
    size: float    # lebar wajah / lebar frame


# (pusat x 0..1, lebar wajah / lebar frame, keyakinan 0..1)
Detection = tuple[float, float, float]

MIN_FACE_FRAC = 0.05   # wajah lebih sempit dari ini (mis. penonton di latar) diabaikan


# --------------------------------------------------------------------------- detektor
class HaarDetector:
    """Haar cascade bawaan OpenCV: tanpa unduhan model, tetapi lemah pada wajah menyamping/terhalang."""
    name = "haar"

    def __init__(self) -> None:
        import cv2  # impor lambat: modul lain tetap bisa dipakai tanpa OpenCV
        if not hasattr(cv2, "CascadeClassifier"):     # mis. OpenCV 5.x; requirements.txt mematok <5.0
            raise RuntimeError("OpenCV ini tidak menyertakan CascadeClassifier (pakai opencv-python-headless 4.x).")
        self._cv2 = cv2
        # Satu instans per detektor: CascadeClassifier tidak aman dipakai bersama antar-thread render.
        self._cascades = []
        for name in ("haarcascade_frontalface_alt2.xml", "haarcascade_frontalface_default.xml"):
            c = cv2.CascadeClassifier(cv2.data.haarcascades + name)
            if not c.empty():
                self._cascades.append(c)
        if not self._cascades:
            raise RuntimeError("Cascade Haar bawaan OpenCV tidak ditemukan.")

    def detect(self, frame) -> list[Detection]:
        cv2 = self._cv2
        h, w = frame.shape[:2]
        scale = 480.0 / w if w > 480 else 1.0
        small = cv2.resize(frame, (int(w * scale), int(h * scale))) if scale != 1.0 else frame
        gray = cv2.equalizeHist(cv2.cvtColor(small, cv2.COLOR_BGR2GRAY))
        sw = gray.shape[1]
        for c in self._cascades:
            found = c.detectMultiScale(gray, scaleFactor=1.15, minNeighbors=5,
                                       minSize=(max(24, sw // 16), max(24, sw // 16)))
            if len(found):
                return [((x + fw / 2) / sw, fw / sw, 1.0) for (x, _y, fw, _fh) in found]
        return []


class YuNetDetector:
    """YuNet (OpenCV `FaceDetectorYN`): model ONNX kecil berbasis CNN, jauh lebih tahan terhadap wajah
    menyamping/terhalang dan lebih cepat daripada Haar. Butuh file model terpisah (lihat README)."""
    name = "yunet"

    def __init__(self, model_path: str | Path, score_threshold: float = 0.7, max_width: int = 640) -> None:
        import cv2
        if not hasattr(cv2, "FaceDetectorYN"):
            raise RuntimeError("OpenCV ini tidak punya FaceDetectorYN (butuh OpenCV >= 4.5.4).")
        self._cv2 = cv2
        self._max_width = max_width
        self._size = (320, 320)
        self._net = cv2.FaceDetectorYN.create(str(model_path), "", self._size, score_threshold, 0.3, 5000)

    def detect(self, frame) -> list[Detection]:
        cv2 = self._cv2
        h, w = frame.shape[:2]
        if w > self._max_width:
            frame = cv2.resize(frame, (self._max_width, max(1, int(round(h * self._max_width / w)))))
        sh, sw = frame.shape[:2]
        if (sw, sh) != self._size:
            self._net.setInputSize((sw, sh))
            self._size = (sw, sh)
        _, faces = self._net.detect(frame)
        if faces is None:
            return []
        out: list[Detection] = []
        for row in faces:
            x, fw = float(row[0]), float(row[2])
            conf = float(row[14]) if len(row) > 14 else 1.0
            # kotak YuNet boleh keluar dari frame (wajah terpotong); jaga agar tetap di 0..1
            out.append((min(1.0, max(0.0, (x + fw / 2) / sw)), min(1.0, max(0.0, fw / sw)), min(1.0, max(0.0, conf))))
        return out


def find_yunet_model(explicit: str | None = None, models_dir: str | Path | None = None) -> Path | None:
    """Lokasi file model YuNet: path eksplisit, atau `face_detection_yunet*.onnx` di `models_dir`."""
    if explicit:
        p = Path(explicit).expanduser()
        return p if p.is_file() else None
    if models_dir and Path(models_dir).is_dir():
        for p in sorted(Path(models_dir).glob("face_detection_yunet*.onnx"), reverse=True):  # nama bertanggal: terbaru dulu
            if p.is_file():
                return p
    return None


def load_detector(mode: str = "auto", *, model_path: str | None = None,
                  models_dir: str | Path | None = None):
    """Pilih detektor wajah. Return (detektor, catatan).

    auto  : YuNet bila file modelnya ada dan bisa dimuat, selain itu Haar.
    yunet : sama, tetapi memberi catatan bila model tidak ada.
    haar  : selalu Haar.
    Catatan terisi bila YuNet diminta tetapi tidak bisa dipakai (model tidak ada / rusak / OpenCV lama).
    Gagal memuat YuNet tidak pernah menggagalkan render: selalu jatuh ke Haar."""
    mode = (mode or "auto").strip().lower()
    note: str | None = None
    if mode in ("auto", "yunet"):
        path = find_yunet_model(model_path, models_dir)
        if path is None:
            if mode == "yunet":
                note = "Model YuNet tidak ditemukan; memakai detektor Haar."
        else:
            try:
                return YuNetDetector(path), None
            except Exception as e:  # noqa: BLE001 - cv2.error, RuntimeError, dll.: apa pun alasannya, jatuh ke Haar
                note = f"YuNet tidak bisa dimuat ({str(e).strip().splitlines()[0][:100] if str(e).strip() else type(e).__name__}); memakai detektor Haar."
    return HaarDetector(), note


def pick_face(dets: Sequence[Detection], prev_cx: float | None) -> Detection | None:
    """Pilih satu wajah: utamakan yang besar, yakin, dan dekat dengan posisi sebelumnya (kontinuitas)."""
    best: Detection | None = None
    best_score = -1.0
    for cx, fw, conf in dets:
        if fw < MIN_FACE_FRAC:
            continue
        cont = 1.0 if prev_cx is None else 1.0 - 0.7 * min(1.0, abs(cx - prev_cx) / 0.5)
        score = fw * fw * cont * max(conf, 0.05)
        if score > best_score:
            best, best_score = (cx, fw, conf), score
    return best


Scan = list[tuple[float, list[Detection]]]      # [(waktu detik, semua deteksi pada frame itu)]


def scan_faces(video_path: str, sample_fps: float = 2.5, max_samples: int = 600,
               should_stop: Callable[[], bool] | None = None, *, detector=None) -> Scan:
    """Deteksi mentah (belum memilih wajah mana pun) pada tiap titik sampel waktu. Butuh opencv-python.

    Satu pindai diolah murni di memori: `faces_from_scan` (satu wajah, layout Ikuti wajah) atau `split_two`
    (dua pembicara). Daftar kosong bila detektor tidak ada atau video tidak bisa dibuka."""
    import cv2

    if detector is None:
        try:
            detector = HaarDetector()
        except RuntimeError:
            return []
    cap = cv2.VideoCapture(video_path)
    if not cap.isOpened():
        return []
    fps = cap.get(cv2.CAP_PROP_FPS) or 25.0
    total = int(cap.get(cv2.CAP_PROP_FRAME_COUNT) or 0)
    step = max(1, int(round(fps / max(0.5, sample_fps))))
    if total and total / step > max_samples:
        step = int(math.ceil(total / max_samples))

    out: Scan = []
    idx = 0
    while True:
        if should_stop and should_stop():
            break
        if not cap.grab():
            break
        if idx % step == 0:
            ok, frame = cap.retrieve()
            if ok:
                out.append((idx / fps, list(detector.detect(frame))))
        idx += 1
    cap.release()
    return out


def faces_from_scan(scan: Sequence[tuple[float, Sequence[Detection]]]) -> list[FaceSample | None]:
    """Satu wajah per sampel (None bila tidak ada), dipilih dengan kontinuitas posisi: `pick_face`."""
    out: list[FaceSample | None] = []
    prev_cx: float | None = None
    for t, dets in scan:
        best = pick_face(dets, prev_cx)
        if best is None:
            out.append(None)
        else:
            prev_cx = best[0]
            out.append(FaceSample(t, best[0], best[1]))
    return out


def detect_faces(video_path: str, sample_fps: float = 2.5, max_samples: int = 600,
                 should_stop: Callable[[], bool] | None = None, *, detector=None) -> list[FaceSample | None]:
    """Satu entri per titik sampel waktu; None bila tidak ada wajah. Butuh opencv-python."""
    return faces_from_scan(scan_faces(video_path, sample_fps, max_samples, should_stop, detector=detector))


def _top_faces(dets: Sequence[Detection]) -> list[Detection]:
    """Hingga dua wajah terbesar, urut kiri -> kanan. Dua kotak yang saling menimpa dianggap satu wajah
    (deteksi ganda), jadi yang dikembalikan hanya yang lebih besar."""
    faces = sorted((d for d in dets if d[1] >= MIN_FACE_FRAC),
                   key=lambda d: d[1] * d[1] * max(d[2], 0.05), reverse=True)[:2]
    if len(faces) == 2:
        left, right = sorted(faces, key=lambda d: d[0])
        return [left, right] if right[0] - left[0] >= (left[1] + right[1]) / 2.0 else faces[:1]
    return faces


def _owner_is_a(cx: float, prev_a: float | None, prev_b: float | None) -> bool:
    """Satu wajah terlihat: milik jalur A (kiri) atau B (kanan)? Ikuti jalur yang posisinya terdekat."""
    if prev_a is None and prev_b is None:
        return cx < 0.5
    if prev_a is None:
        return abs(cx - prev_b) > 0.2          # type: ignore[operator]
    if prev_b is None:
        return abs(cx - prev_a) <= 0.2
    return abs(cx - prev_a) <= abs(cx - prev_b)


def split_two(scan: Sequence[tuple[float, Sequence[Detection]]]
              ) -> tuple[list[FaceSample | None], list[FaceSample | None], int]:
    """Bagi deteksi mentah jadi dua jalur untuk layout dua pembicara: A = pembicara kiri, B = kanan.

    Satu entri per sampel di tiap jalur (None bila jalur itu tak terlihat; `smooth_path` menahan posisi terakhir).
    Bila hanya satu wajah terlihat, ia diberikan ke jalur dengan posisi terdekat sehingga jalur tidak
    saling bertukar saat salah satu orang keluar frame sebentar.
    Return (jalur_a, jalur_b, jumlah sampel yang memperlihatkan dua orang sekaligus)."""
    out_a: list[FaceSample | None] = []
    out_b: list[FaceSample | None] = []
    prev_a: float | None = None
    prev_b: float | None = None
    both = 0
    for t, dets in scan:
        faces = _top_faces(dets)
        a: Detection | None = None
        b: Detection | None = None
        if len(faces) == 2:
            a, b = faces
            both += 1
        elif len(faces) == 1:
            if _owner_is_a(faces[0][0], prev_a, prev_b):
                a = faces[0]
            else:
                b = faces[0]
        if a is not None:
            prev_a = a[0]
        if b is not None:
            prev_b = b[0]
        out_a.append(FaceSample(t, a[0], a[1]) if a else None)
        out_b.append(FaceSample(t, b[0], b[1]) if b else None)
    return out_a, out_b, both


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


def static_hold(values: Sequence[float], face_size: float, half: float, lo: float, hi: float,
                margin: float) -> float | None:
    """Posisi crop tetap untuk satu adegan, atau None bila kamera harus bergerak.

    Ide "stationary vs tracking" dari AutoFlip (Google MediaPipe): bila seluruh wajah, dari posisi
    paling kiri sampai paling kanan selama adegan, tetap muat di dalam SATU crop, kamera diam di tengah
    rentang itu. Gerakan kecil tidak perlu diikuti dengan panning. Syarat dicek terhadap posisi akhir
    (setelah dijepit ke dalam frame), jadi hasilnya tidak pernah memotong wajah yang sebelumnya terlihat.

    values   : pusat wajah (0..1) sepanjang adegan
    face_size: lebar wajah terbesar (fraksi lebar frame)
    half     : setengah lebar crop (fraksi lebar frame)
    margin   : ruang aman antara tepi wajah dan tepi crop (fraksi lebar frame)
    """
    if not values:
        return None
    limit = half - face_size / 2.0 - margin
    if limit <= 0:
        return None                                   # wajah (hampir) selebar crop: harus diikuti
    pos = min(max((min(values) + max(values)) / 2.0, lo), hi)
    return pos if max(abs(v - pos) for v in values) <= limit else None


def smooth_path(samples: list[FaceSample | None], duration: float, sample_dt: float, crop_w_frac: float,
                *, deadzone: float = 0.06, max_speed: float = 0.30, snap_threshold: float = 0.28,
                grid_dt: float = 0.1, max_keyframes: int = 40,
                cuts: Sequence[float] = (), hold_static: bool = True,
                hold_margin: float = 0.02) -> list[tuple[float, float]]:
    """Ubah sampel wajah mentah menjadi keyframe (t, pusat_crop 0..1) yang halus.

    deadzone      : abaikan perbedaan target < nilai ini (fraksi lebar frame) agar kamera tidak bergetar
    max_speed     : kecepatan pan maksimum (fraksi lebar frame per detik)
    snap_threshold: bila target melompat lebih jauh dari ini dan bertahan >=0.3 dtk -> jump-cut
    cuts          : waktu (detik, relatif awal klip) pergantian adegan yang sebenarnya, mis. dari
                    `scenes.detect_cuts`. Di tiap cut kamera pindah seketika ke posisi adegan baru (tanpa
                    meluncur), dan sampel dari adegan lama tidak ikut memengaruhi target adegan baru.
    hold_static   : adegan yang seluruh wajahnya selalu muat di satu crop dibuat diam total (lihat `static_hold`)
    hold_margin   : ruang aman wajah-ke-tepi-crop untuk keputusan di atas (fraksi lebar frame)
    """
    duration = max(duration, grid_dt)
    half = crop_w_frac / 2.0
    lo, hi = half, 1.0 - half
    center = min(max(0.5, lo), hi)
    valid = [s for s in samples if s is not None]
    if not valid:
        return [(0.0, center), (duration, center)]

    cut_times = sorted(c for c in cuts if 0.0 < c < duration)

    def seg(t: float) -> int:
        """Indeks adegan untuk waktu t (0 bila tidak ada cut)."""
        return bisect.bisect_right(cut_times, t)

    # 0) median 3-sampel pada sampel mentah: satu deteksi salah (outlier) tidak boleh menggerakkan kamera,
    #    sedangkan pindah posisi yang bertahan >= 2 sampel (pergantian adegan) tetap lolos.
    #    Tetangga dari adegan lain tidak dihitung: sampel pertama setelah cut sah dipercaya.
    ordered = sorted(valid, key=lambda s: s.t)
    cxs = [s.cx for s in ordered]
    sample_seg = [seg(s.t) for s in ordered]
    valid = [FaceSample(s.t, _median([cxs[j] for j in range(max(0, i - 1), min(len(ordered), i + 2))
                                      if sample_seg[j] == sample_seg[i]]), s.size)
             for i, s in enumerate(ordered)]

    n = int(math.ceil(duration / grid_dt)) + 1
    times = [i * grid_dt for i in range(n)]
    grid_seg = [seg(t) for t in times]
    by_seg: dict[int, list[FaceSample]] = {}
    for s in valid:
        by_seg.setdefault(seg(s.t), []).append(s)
    # 1) target per titik grid: sampel valid terdekat (dalam adegan yang sama bila ada) dalam 1.2 dtk,
    #    lalu isi lubang dengan nilai terakhir
    raw: list[float | None] = []
    for t, g in zip(times, grid_seg):
        pool = by_seg.get(g) or valid          # adegan tanpa deteksi sama sekali: perilaku lama (sampel terdekat)
        near = min(pool, key=lambda s: abs(s.t - t))
        raw.append(near.cx if abs(near.t - t) <= max(1.2, sample_dt * 1.5) else None)
    first = next((v for v in raw if v is not None), center)
    filled: list[float] = []
    last = first
    for v in raw:
        last = v if v is not None else last
        filled.append(last)
    # 2) median filter (jendela 5, tidak melewati batas adegan) membuang deteksi liar
    med = [_median([filled[j] for j in range(max(0, i - 2), min(len(filled), i + 3)) if grid_seg[j] == grid_seg[i]])
           for i in range(len(filled))]
    # 2b) adegan "diam": wajah tak pernah keluar dari satu crop -> kamera tidak bergerak sama sekali
    if hold_static:
        idx_by_seg: dict[int, list[int]] = {}
        for i, g in enumerate(grid_seg):
            idx_by_seg.setdefault(g, []).append(i)
        for g, idxs in idx_by_seg.items():
            seg_samples = by_seg.get(g)
            if not seg_samples:
                continue                                  # tanpa deteksi di adegan ini: biarkan perilaku lama
            held = static_hold([med[i] for i in idxs], max(s.size for s in seg_samples), half, lo, hi, hold_margin)
            if held is not None:
                for i in idxs:
                    med[i] = held
    # 3) kontroler: deadzone + histeresis + batas kecepatan + jump-cut
    pos = min(max(med[0], lo), hi)
    moving = False
    far_count = 0
    prev_seg = grid_seg[0]
    need = max(1, int(round(0.3 / grid_dt)))
    path: list[float] = []
    for i, tgt in enumerate(med):
        tgt = min(max(tgt, lo), hi)
        if grid_seg[i] != prev_seg:             # cut sungguhan: pindah seketika, tidak meluncur
            prev_seg = grid_seg[i]
            pos, moving, far_count = tgt, False, 0
            path.append(pos)
            continue
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


def detect_face_in_image(path: str, detector=None) -> float | None:
    """Pusat horizontal (0..1) wajah terbesar pada satu gambar, atau None. Dipakai untuk preview studio."""
    try:
        import cv2
    except ImportError:
        return None
    img = cv2.imread(path)
    if img is None:
        return None
    if detector is None:
        try:
            detector = HaarDetector()
        except RuntimeError:
            return None
    best = pick_face(detector.detect(img), None)
    return float(best[0]) if best else None


def analyze_image_faces(path: str, detector=None) -> tuple[float | None, list[float]]:
    """Satu kali deteksi untuk preview studio: (pusat wajah terbesar, pusat hingga dua wajah urut kiri->kanan).
    Dua nilai itu dipakai preview layout Ikuti wajah dan layout dua pembicara. (None, []) bila tak ada wajah."""
    try:
        import cv2
    except ImportError:
        return None, []
    img = cv2.imread(path)
    if img is None:
        return None, []
    if detector is None:
        try:
            detector = HaarDetector()
        except RuntimeError:
            return None, []
    dets = detector.detect(img)
    best = pick_face(dets, None)
    return (float(best[0]) if best else None), [float(f[0]) for f in _top_faces(dets)]

"""Pemilihan klip: fusi skor AI + heatmap retensi, snapping ke batas kalimat, dan penghapusan tumpang-tindih.
Semua fungsi murni (tanpa jaringan) sehingga bisa diuji penuh."""
from __future__ import annotations

import math
import re
from dataclasses import asdict, dataclass, field

from .transcript import Segment

DURATION_PRESETS = {           # (min, max, target) detik
    "short": (8.0, 24.0, 15.0),
    "standard": (20.0, 45.0, 30.0),
    "extended": (40.0, 95.0, 60.0),
}


@dataclass
class Moment:
    start: float
    end: float
    score: float = 50.0          # skor akhir 0..100
    ai_score: float | None = None
    heat: float | None = None    # rata-rata retensi relatif 0..1
    title: str = ""
    hook: str = ""
    reason: str = ""
    hashtags: list[str] = field(default_factory=list)
    source: str = "ai"           # ai | heuristic

    @property
    def length(self) -> float:
        return self.end - self.start

    def to_dict(self) -> dict:
        d = asdict(self)
        d["duration"] = round(self.length, 2)
        for k in ("start", "end", "score"):
            d[k] = round(d[k], 2)
        if d["heat"] is not None:
            d["heat"] = round(d["heat"], 3)
        return d


def window_for(preset: str, min_len: float | None = None, max_len: float | None = None) -> tuple[float, float, float]:
    lo, hi, tgt = DURATION_PRESETS.get(preset, DURATION_PRESETS["standard"])
    if min_len and max_len and 3 <= min_len < max_len <= 600:
        lo, hi, tgt = float(min_len), float(max_len), (float(min_len) + float(max_len)) / 2
    return lo, hi, tgt


# --------------------------------------------------------------------------- heatmap
def normalize_heatmap(hm: list[dict] | None) -> list[dict]:
    rows = []
    for h in hm or []:
        try:
            s, e, v = float(h["start_time"]), float(h["end_time"]), float(h["value"])
        except (KeyError, TypeError, ValueError):
            continue
        if e > s:
            rows.append({"start_time": s, "end_time": e, "value": v})
    peak = max((r["value"] for r in rows), default=0.0)
    if peak > 0:
        for r in rows:
            r["value"] = round(r["value"] / peak, 4)
    return rows


def heat_mean(hm: list[dict], s: float, e: float) -> float:
    tot = w = 0.0
    for h in hm:
        a, b = max(s, h["start_time"]), min(e, h["end_time"])
        if b > a:
            tot += h["value"] * (b - a)
            w += b - a
    return tot / w if w else 0.0


def heat_peaks(hm: list[dict], k: int, min_sep: float) -> list[float]:
    """Waktu pusat k puncak tertinggi dengan jarak minimal `min_sep` detik."""
    picked: list[float] = []
    for h in sorted(hm, key=lambda r: -r["value"]):
        c = (h["start_time"] + h["end_time"]) / 2
        if all(abs(c - p) >= min_sep for p in picked):
            picked.append(c)
        if len(picked) >= k:
            break
    return picked


# --------------------------------------------------------------------------- snapping
def _nearest(values: list[float], target: float, max_shift: float) -> float | None:
    best = min(values, key=lambda v: abs(v - target), default=None)
    return best if best is not None and abs(best - target) <= max_shift else None


def snap(start: float, end: float, segments: list[Segment], lo: float, hi: float, duration: float,
         max_shift: float = 3.0) -> tuple[float, float]:
    """Geser start/end ke batas kalimat terdekat tanpa melanggar batas durasi."""
    start, end = max(0.0, start), min(duration, end)
    if segments:
        s2 = _nearest([s.start for s in segments], start, max_shift)
        e2 = _nearest([s.end for s in segments], end, max_shift)
        if s2 is not None:
            start = s2
        if e2 is not None and e2 > start:
            end = e2
    length = end - start
    if length > hi:
        end = start + hi
        cut = [s.end for s in segments if start + lo <= s.end <= start + hi]
        if cut:
            end = max(cut)
    elif length < lo:
        end = min(duration, start + lo)
        if end - start < lo:
            start = max(0.0, end - lo)
    return round(start, 2), round(min(end, duration), 2)


def iou(a: Moment, b: Moment) -> float:
    inter = max(0.0, min(a.end, b.end) - max(a.start, b.start))
    union = a.length + b.length - inter
    return inter / union if union > 0 else 0.0


def nms(moments: list[Moment], max_overlap: float = 0.3) -> list[Moment]:
    kept: list[Moment] = []
    for m in sorted(moments, key=lambda x: -x.score):
        if all(iou(m, k) <= max_overlap and abs(m.start - k.start) > 2.0 for k in kept):
            kept.append(m)
    return kept


# --------------------------------------------------------------------------- fusi
def _clean_tags(tags) -> list[str]:
    out = []
    for t in tags or []:
        t = re.sub(r"[^\w]", "", str(t), flags=re.UNICODE)
        if t and t.lower() not in {x.lower() for x in out}:
            out.append(t[:30])
    return out[:6]


def fuse(candidates: list[dict], heatmap: list[dict], segments: list[Segment], duration: float,
         window: tuple[float, float, float], max_clips: int = 8, ai_weight: float = 0.65) -> list[Moment]:
    """Gabungkan kandidat AI dengan retensi penonton menjadi daftar klip akhir yang rapi."""
    lo, hi, _ = window
    hm = normalize_heatmap(heatmap)
    moments: list[Moment] = []
    for c in candidates:
        try:
            s, e = float(c["start"]), float(c["end"])
        except (KeyError, TypeError, ValueError):
            continue
        if not (math.isfinite(s) and math.isfinite(e)):
            continue
        if e < s:
            s, e = e, s
        if s >= duration or e - s < 1:
            continue
        s, e = snap(s, e, segments, lo, hi, duration)
        if e - s < min(lo, 3.0):
            continue
        ai = max(0.0, min(100.0, float(c.get("score", 50) or 50)))
        heat = heat_mean(hm, s, e) if hm else None
        score = ai if heat is None else ai * ai_weight + heat * 100 * (1 - ai_weight)
        moments.append(Moment(s, e, round(score, 1), ai, heat, str(c.get("title", ""))[:90],
                              str(c.get("hook", ""))[:120], str(c.get("reason", ""))[:240],
                              _clean_tags(c.get("hashtags")), c.get("source", "ai")))
    return nms(moments)[:max_clips]


def heuristic_candidates(heatmap: list[dict], segments: list[Segment], duration: float,
                         window: tuple[float, float, float], n: int = 8) -> list[dict]:
    """Cadangan tanpa AI: ambil puncak retensi, lalu bangun jendela di sekitarnya."""
    hm = normalize_heatmap(heatmap)
    lo, hi, tgt = window
    cands: list[dict] = []
    if hm:
        for p in heat_peaks(hm, n * 2, min_sep=max(hi, 20.0)):
            s = p - tgt * 0.4
            e = s + tgt
            text = next((sg.text for sg in segments if sg.start >= max(0, s) - 1), "")
            cands.append({"start": s, "end": e, "score": 55 + 40 * heat_mean(hm, s, e),
                          "title": " ".join(text.split()[:8]) or "Momen paling banyak ditonton ulang",
                          "hook": " ".join(text.split()[:10]),
                          "reason": "Puncak retensi penonton (heatmap) — dipilih tanpa AI.",
                          "hashtags": [], "source": "heuristic"})
    elif duration > tgt * 2:
        step = duration / (n + 1)
        for i in range(1, n + 1):
            s = i * step - tgt / 2
            cands.append({"start": s, "end": s + tgt, "score": 40, "title": f"Potongan {i}",
                          "hook": "", "reason": "Pembagian merata (tidak ada heatmap/transkrip).",
                          "hashtags": [], "source": "heuristic"})
    return cands

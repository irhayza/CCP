"""Deteksi pergantian adegan (cut keras) lewat filter `scene` milik ffmpeg. Tanpa dependensi tambahan.

Dipakai `facetrack.smooth_path` agar kamera pindah seketika di cut sungguhan, bukan meluncur ke posisi
wajah baru. Untuk klip pendek (<= beberapa menit) satu pass ffmpeg pada resolusi kecil cukup cepat."""
from __future__ import annotations

import re
import subprocess
from pathlib import Path
from typing import Callable, Iterable

from .procutil import run_streaming

_PTS_TIME = re.compile(r"pts_time:(\d+(?:\.\d+)?)")


def parse_cut_times(lines: Iterable[str]) -> list[float]:
    """Ambil waktu (detik) dari keluaran `metadata=print`: baris `frame:N pts:P pts_time:T`."""
    out: list[float] = []
    for line in lines:
        m = _PTS_TIME.search(line)
        if m:
            out.append(float(m.group(1)))
    return out


def merge_close(times: Iterable[float], min_gap: float) -> list[float]:
    """Urutkan, lalu buang cut yang berjarak < min_gap dari cut sebelumnya (kilatan/transisi = satu cut)."""
    out: list[float] = []
    for t in sorted(times):
        if not out or t - out[-1] >= min_gap:
            out.append(t)
    return out


def detect_cuts(source: str | Path, *, ffmpeg: str = "ffmpeg", threshold: float = 0.30, min_gap: float = 0.6,
                max_cuts: int = 200, width: int = 320,
                register: Callable[[subprocess.Popen], None] | None = None,
                is_cancelled: Callable[[], bool] = lambda: False, timeout: float = 180.0) -> list[float]:
    """Waktu cut (detik, relatif awal file), terurut. Daftar kosong bila tidak ada cut atau ffmpeg gagal:
    deteksi cut hanyalah peningkatan kualitas, kegagalannya tidak boleh menggagalkan render.
    Melempar `procutil.Cancelled` bila dibatalkan."""
    graph = f"scale={int(width)}:-2,select='gt(scene,{float(threshold):.3f})',metadata=print:file=-"
    cmd = [ffmpeg, "-hide_banner", "-nostats", "-loglevel", "error", "-i", str(source),
           "-an", "-sn", "-vf", graph, "-f", "null", "-"]
    lines: list[str] = []
    try:
        rc, _, _ = run_streaming(cmd, on_line=lines.append, register=register, is_cancelled=is_cancelled,
                                 timeout=timeout)
    except OSError:
        return []
    if rc != 0:
        return []
    return merge_close(parse_cut_times(lines), min_gap)[:max_cuts]

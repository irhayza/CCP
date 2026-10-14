"""Transcript: parser json3 YouTube (timing per kata), segmentasi kalimat, Whisper sebagai cadangan."""
from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path

from .captions import Word


@dataclass
class Segment:
    start: float
    end: float
    text: str


def fmt_ts(sec: float) -> str:
    sec = max(0, int(sec))
    h, rem = divmod(sec, 3600)
    m, s = divmod(rem, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m:02d}:{s:02d}"


# --------------------------------------------------------------------------- json3
def parse_json3(data: dict) -> list[Word]:
    """Ubah file subtitle json3 (auto-caption atau manual) menjadi daftar kata berwaktu."""
    raw: list[tuple[float, str]] = []
    for ev in data.get("events", []) or []:
        segs = ev.get("segs")
        if not segs:
            continue
        base = float(ev.get("tStartMs", 0)) / 1000.0
        dur = float(ev.get("dDurationMs", 0)) / 1000.0
        toks = [((s.get("utf8") or "").replace("\n", " "), s.get("tOffsetMs")) for s in segs]
        toks = [(u, off) for u, off in toks if u.strip()]
        if not toks:
            continue
        timed = any(off is not None for _, off in toks[1:])
        n = len(toks)
        starts = []
        for i, (_, off) in enumerate(toks):
            starts.append(base + (float(off or 0) / 1000.0 if timed else dur * i / n))
        for i, (u, _) in enumerate(toks):
            parts = u.split()
            nxt = starts[i + 1] if i + 1 < n else base + (dur if dur > 0 else 0.6 * len(parts))
            span = max(nxt - starts[i], 0.05 * len(parts))
            for k, p in enumerate(parts):
                raw.append((starts[i] + span * k / len(parts), p))
    raw.sort(key=lambda x: x[0])
    words: list[Word] = []
    for start, text in raw:
        if words and words[-1].text == text and abs(words[-1].start - start) < 0.1:
            continue  # duplikat dari caption bergulir
        words.append(Word(text, start, start))
    for i, w in enumerate(words):
        nxt = words[i + 1].start if i + 1 < len(words) else w.start + 0.8
        w.end = max(w.start + 0.05, min(nxt, w.start + 0.8))
    return words


def load_words(path: str | Path) -> list[Word]:
    return parse_json3(json.loads(Path(path).read_text(encoding="utf-8")))


def words_to_json(words: list[Word]) -> list[list]:
    return [[w.text, round(w.start, 3), round(w.end, 3)] for w in words]


def words_from_json(rows: list[list]) -> list[Word]:
    return [Word(str(r[0]), float(r[1]), float(r[2])) for r in rows]


def words_in_range(words: list[Word], start: float, end: float) -> list[Word]:
    """Kata dalam [start, end], waktu digeser agar klip mulai dari 0."""
    out = []
    for w in words:
        if w.end <= start or w.start >= end:
            continue
        out.append(Word(w.text, max(0.0, w.start - start), min(end, w.end) - start))
    return out


# --------------------------------------------------------------------------- segmentasi
def make_segments(words: list[Word], gap: float = 0.8, max_len: float = 12.0) -> list[Segment]:
    """Kelompokkan kata jadi kalimat: batas di tanda baca akhir, jeda >= gap, atau terlalu panjang."""
    segs: list[Segment] = []
    cur: list[Word] = []
    for w in words:
        if cur and (w.start - cur[-1].end >= gap or w.end - cur[0].start > max_len):
            segs.append(_seg(cur))
            cur = []
        cur.append(w)
        if re.search(r"[.?!]$", w.text):
            segs.append(_seg(cur))
            cur = []
    if cur:
        segs.append(_seg(cur))
    return segs


def _seg(ws: list[Word]) -> Segment:
    return Segment(ws[0].start, ws[-1].end, " ".join(w.text for w in ws))


def prompt_lines(segments: list[Segment], t0: float, t1: float) -> str:
    return "\n".join(f"[{s.start:.0f}s | {fmt_ts(s.start)}] {s.text}" for s in segments if t0 <= s.start < t1)


# --------------------------------------------------------------------------- whisper (opsional)
def whisper_available() -> bool:
    try:
        import faster_whisper  # noqa: F401
        return True
    except Exception:
        return False


_whisper_cache: dict = {}  # key: (model_size, device) -> WhisperModel


def whisper_words(audio_path: str, model_size: str = "small", device: str = "auto",
                  language: str | None = None) -> list[Word]:
    from faster_whisper import WhisperModel  # type: ignore

    cache_key = (model_size, device)
    if cache_key not in _whisper_cache:
        compute = "float16" if device == "cuda" else "int8"
        _whisper_cache[cache_key] = WhisperModel(model_size, device=device, compute_type=compute)
    model = _whisper_cache[cache_key]
    segments, _ = model.transcribe(audio_path, language=language or None, word_timestamps=True, vad_filter=True)
    words: list[Word] = []
    for seg in segments:
        for w in seg.words or []:
            t = (w.word or "").strip()
            if t:
                words.append(Word(t, float(w.start), float(w.end)))
    return words

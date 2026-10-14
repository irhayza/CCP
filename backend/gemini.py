"""Klien Gemini via REST (stdlib saja).

- API key dikirim lewat header `x-goog-api-key`, tidak pernah lewat query string / URL.
- Daftar model diambil dinamis dari akun si pengguna (model lama yang sudah dipensiunkan otomatis hilang);
  daftar statis hanya dipakai bila pemanggilan list gagal.
- Fallback antar-model untuk kuota (429), model sibuk (5xx), atau model tidak ditemukan (404).
- Transkrip panjang dipecah per jendela waktu agar podcast 2-3 jam tetap muat."""
from __future__ import annotations

import json
import math
import re
import time
import urllib.error
import urllib.request
from typing import Callable

from .config import DEFAULT_GEMINI_FALLBACK
from .security import redact
from .transcript import Segment, fmt_ts, prompt_lines

BASE = "https://generativelanguage.googleapis.com/v1beta"

SCHEMA = {
    "type": "ARRAY",
    "items": {
        "type": "OBJECT",
        "properties": {
            "start": {"type": "NUMBER"}, "end": {"type": "NUMBER"}, "score": {"type": "NUMBER"},
            "title": {"type": "STRING"}, "hook": {"type": "STRING"}, "reason": {"type": "STRING"},
            "hashtags": {"type": "ARRAY", "items": {"type": "STRING"}},
        },
        "required": ["start", "end", "score", "title", "hook", "reason"],
    },
}


class GeminiError(Exception):
    def __init__(self, kind: str, message: str, retry_after: float | None = None):
        super().__init__(message)
        self.kind = kind            # auth | quota | unavailable | notfound | bad | network
        self.retry_after = retry_after


HttpFn = Callable[[str, str, str, dict | None, float], dict]


def http_json(method: str, url: str, key: str, body: dict | None = None, timeout: float = 90) -> dict:
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method, headers={
        "x-goog-api-key": key, "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read().decode("utf-8", "replace") or "{}")
    except urllib.error.HTTPError as e:
        raw = e.read().decode("utf-8", "replace")
        msg = redact(raw[:300], key)
        retry = None
        m = re.search(r'"retryDelay"\s*:\s*"(\d+(?:\.\d+)?)s"', raw)
        if m:
            retry = float(m.group(1))
        if e.code in (400, 401, 403) and re.search(r"API key|API_KEY|PERMISSION|credential", raw, re.I):
            raise GeminiError("auth", "API key ditolak Google. Periksa kuncinya.") from None
        if e.code == 429:
            raise GeminiError("quota", "Kuota model habis.", retry) from None
        if e.code == 404:
            raise GeminiError("notfound", "Model tidak ditemukan.") from None
        if e.code >= 500:
            raise GeminiError("unavailable", f"Model sibuk ({e.code}).", retry) from None
        raise GeminiError("bad", f"Permintaan ditolak ({e.code}): {msg}") from None
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        raise GeminiError("network", f"Gagal terhubung ke Google: {redact(str(e), key)}") from None


# --------------------------------------------------------------------------- model
_BAD = re.compile(r"preview|exp|image|tts|live|audio|thinking|embed|8b|learnlm|gemma|robotics|computer", re.I)
_FLASH = re.compile(r"^gemini-(\d+(?:\.\d+)?)-flash(-lite)?(-\d+)?$")


def rank_models(names: list[str], override: tuple[str, ...] = ()) -> list[str]:
    if override:
        return list(override)
    found = []
    for n in names:
        n = n.removeprefix("models/")
        m = _FLASH.match(n)
        if m and not _BAD.search(n):
            found.append((-float(m.group(1)), 1 if m.group(2) else 0, 1 if m.group(3) else 0, n))
    found.sort()
    ranked = [f[-1] for f in found]
    if "gemini-flash-latest" in [x.removeprefix("models/") for x in names]:
        ranked.append("gemini-flash-latest")
    return ranked or list(DEFAULT_GEMINI_FALLBACK)


def list_models(key: str, override: tuple[str, ...] = (), http: HttpFn = http_json) -> list[str]:
    try:
        res = http("GET", f"{BASE}/models?pageSize=200", key, None, 30)
    except GeminiError as e:
        if e.kind == "auth":
            raise
        return rank_models([], override)
    names = [m["name"] for m in res.get("models", [])
             if "generateContent" in (m.get("supportedGenerationMethods") or [])]
    return rank_models(names, override)


# --------------------------------------------------------------------------- prompt
def build_prompt(lines: str, *, n: int, lo: float, hi: float, target: float, language: str, focus: str,
                 peaks: list[float], video_title: str, t0: float, t1: float) -> str:
    lang = {"id": "Indonesian", "en": "English"}.get(language, "the same language as the transcript")
    peak_txt = ", ".join(fmt_ts(p) for p in peaks if t0 <= p <= t1) or "none"
    focus_txt = f"\nEditor's focus request (follow it, but keep the rules): {focus.strip()[:300]}" if focus.strip() else ""
    return (
        "You are a senior short-form video editor for TikTok, Reels and YouTube Shorts.\n"
        f"Video title: {video_title[:150]}\n"
        f"Transcript window {fmt_ts(t0)}–{fmt_ts(t1)}; each line starts with [seconds | clock] timestamp.\n\n"
        f"Pick the {n} best self-contained moments. Rules:\n"
        f"- Each moment is {lo:.0f}–{hi:.0f} seconds long (aim for ~{target:.0f}s).\n"
        "- start/end are absolute seconds from the start of the video, aligned to sentence boundaries.\n"
        "- The first 3 seconds must work as a hook; the moment must have a complete arc or punchline and "
        "must not end mid-sentence.\n"
        "- Prefer surprising, emotional, funny, controversial or highly actionable content. "
        "Skip intros, greetings, sponsor reads, filler and housekeeping.\n"
        f"- Viewers replayed these timestamps most (audience retention peaks): {peak_txt}. Treat as a hint.\n"
        "- score: 0-100 predicted viral potential. Be critical and spread scores; do not give everything 90+.\n"
        f"- title <= 60 characters; hook <= 12 words (text overlay shown in the first seconds); reason = one sentence; "
        f"hashtags = 3-5 words without '#'.\n- Write title, hook, reason in {lang}."
        f"{focus_txt}\n\nTRANSCRIPT:\n{lines}\n\nReturn only a JSON array."
    )


def _parse_json(text: str):
    t = text.strip()
    t = re.sub(r"^```(?:json)?\s*|\s*```$", "", t, flags=re.I)
    try:
        return json.loads(t)
    except json.JSONDecodeError:
        m = re.search(r"\[.*\]", t, re.S)
        if not m:
            raise
        return json.loads(m.group(0))


def generate_json(key: str, models: list[str], prompt: str, *, http: HttpFn = http_json,
                  sleep: Callable[[float], None] = time.sleep, on_try: Callable[[str], None] | None = None):
    """Coba model satu per satu. Mengembalikan (data, model_dipakai, daftar_percobaan)."""
    body = {"contents": [{"parts": [{"text": prompt}]}],
            "generationConfig": {"temperature": 0.7, "responseMimeType": "application/json",
                                 "responseSchema": SCHEMA}}
    tried: list[str] = []
    last: GeminiError | None = None
    for model in models:
        if on_try:
            on_try(model)
        for attempt in range(2):
            try:
                res = http("POST", f"{BASE}/models/{model}:generateContent", key, body, 120)
                cands = res.get("candidates") or []
                parts = ((cands[0].get("content") or {}).get("parts") or []) if cands else []
                text = "".join(p.get("text", "") for p in parts)
                if not text:
                    raise GeminiError("bad", "Respons kosong / diblokir.")
                data = _parse_json(text)
                if not isinstance(data, list):
                    raise GeminiError("bad", "Respons bukan array.")
                return data, model, tried + [f"{model}: ok"]
            except json.JSONDecodeError:
                last = GeminiError("bad", "JSON dari model rusak.")
                tried.append(f"{model}: json rusak")
                break
            except GeminiError as e:
                if e.kind == "auth":
                    raise
                last = e
                if e.kind == "unavailable" and attempt == 0:
                    sleep(min(e.retry_after or 2.0, 30.0))
                    continue
                tried.append(f"{model}: {e.kind}")
                break
    raise last or GeminiError("bad", "Tidak ada model yang tersedia.")


# --------------------------------------------------------------------------- orkestrasi
def plan_windows(duration: float, max_windows: int = 8, base: float = 1200.0, overlap: float = 60.0):
    size = max(base, math.ceil(duration / max_windows)) if duration else base
    wins, t = [], 0.0
    while t < max(duration, 1.0):
        wins.append((t, min(duration, t + size + overlap)))
        t += size
    return wins


def find_candidates(key: str, segments: list[Segment], duration: float, video_title: str, *, window,
                    max_clips: int, language: str, focus: str, peaks: list[float], models: list[str],
                    http: HttpFn = http_json, sleep: Callable[[float], None] = time.sleep,
                    on_progress: Callable[[float, str], None] | None = None) -> tuple[list[dict], list[str], list[str]]:
    """Kembalikan (kandidat mentah, model_dipakai, log_percobaan)."""
    lo, hi, tgt = window
    wins = plan_windows(duration)
    per = min(12, max(3, math.ceil(max_clips * (2 if len(wins) == 1 else 1.4))))
    out: list[dict] = []
    used: list[str] = []
    log: list[str] = []
    order = list(models)
    for i, (t0, t1) in enumerate(wins):
        lines = prompt_lines(segments, t0, t1)
        if not lines.strip():
            continue
        if on_progress:
            on_progress(i / len(wins), f"Gemini menganalisis bagian {i + 1}/{len(wins)}")
        prompt = build_prompt(lines, n=per, lo=lo, hi=hi, target=tgt, language=language, focus=focus,
                              peaks=peaks, video_title=video_title, t0=t0, t1=t1)
        data, model, tried = generate_json(key, order, prompt, http=http, sleep=sleep)
        out.extend(c for c in data if isinstance(c, dict))
        log.extend(tried)
        used.append(model)
        order = [model] + [m for m in order if m != model]   # model yang berhasil dicoba lebih dulu
    return out, used, log

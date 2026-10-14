"""Alur kerja tingkat tinggi (analisis & render). Tidak bergantung pada FastAPI sehingga mudah diuji."""
from __future__ import annotations

import hashlib
import json
import re
import shutil
import threading
import time
from pathlib import Path
from typing import Any

from . import gemini
from .captions import CaptionStyle, TitleSpec, Word, PRESETS
from .clips import fuse, heat_peaks, heuristic_candidates, normalize_heatmap, window_for
from .config import Settings
from .db import Store
from .jobs import JobContext
from .layouts import LayoutSpec
from .procutil import CommandError
from .render import RenderOptions, render_clip
from .security import InputError
from .transcript import (make_segments, load_words, whisper_available, whisper_words, words_from_json,
                         words_in_range, words_to_json)
from .ytdlp import YtDlp

ANALYSIS_TTL = 24 * 3600
WHISPER_MAX_SECONDS = 45 * 60


class Services:
    def __init__(self, settings: Settings, store: Store, ytdlp: YtDlp):
        self.settings, self.store, self.ytdlp = settings, store, ytdlp
        self._tlocks: dict[str, threading.Lock] = {}
        self._guard = threading.Lock()

    def lock_for(self, vid: str) -> threading.Lock:
        with self._guard:
            return self._tlocks.setdefault(vid, threading.Lock())


# --------------------------------------------------------------------------- transcript
def get_transcript(svc: Services, url: str, vid: str, info: dict, language: str,
                   ctx: JobContext | None = None) -> tuple[list[Word], str]:
    """Kembalikan (kata, sumber). Sumber: youtube | whisper | none. Di-cache per video."""
    cache = svc.settings.cache_dir / "transcripts" / f"{vid}.json"
    with svc.lock_for(vid):
        if cache.is_file():
            try:
                d = json.loads(cache.read_text(encoding="utf-8"))
                return words_from_json(d["words"]), d["source"]
            except (OSError, ValueError, KeyError):
                cache.unlink(missing_ok=True)
        hooks = ctx.hooks if ctx else None
        langs = [l for l in (language if language in ("id", "en") else "", info.get("language") or "", "en", "id") if l]
        langs = list(dict.fromkeys(langs))
        words: list[Word] = []
        source = "none"
        sub = svc.ytdlp.fetch_subtitles(url, vid, langs, svc.settings.work_dir / f"subs_{vid}", hooks)
        if sub:
            words = load_words(sub)
            source = "youtube" if words else "none"
        if not words and whisper_available() and 0 < info.get("duration", 0) <= WHISPER_MAX_SECONDS:
            if ctx:
                ctx.progress(15, "whisper", "Tidak ada subtitle, mentranskrip dengan Whisper (bisa lama)")
            audio = svc.ytdlp.download_audio(url, vid, svc.settings.work_dir / f"audio_{vid}", hooks)
            words = whisper_words(str(audio), svc.settings.whisper_model, svc.settings.whisper_device,
                                  language if language in ("id", "en") else info.get("language") or None)
            source = "whisper" if words else "none"
            shutil.rmtree(audio.parent, ignore_errors=True)
        shutil.rmtree(svc.settings.work_dir / f"subs_{vid}", ignore_errors=True)
        if words:
            cache.write_text(json.dumps({"source": source, "words": words_to_json(words)}), encoding="utf-8")
        return words, source


# --------------------------------------------------------------------------- analisis
def analysis_key(p: dict, mode: str) -> str:
    raw = json.dumps([p["video_id"], p["preset"], p.get("min_len"), p.get("max_len"), p["max_clips"],
                      p.get("focus", ""), p.get("language", "auto"), mode], sort_keys=True)
    return "analysis:" + hashlib.sha1(raw.encode()).hexdigest()


def run_analysis(svc: Services, ctx: JobContext, p: dict, api_key: str) -> dict:
    mode = "demo" if api_key == "mock" else ("ai" if api_key else "noai")
    key = analysis_key(p, mode)
    if p.get("use_cache", True):
        hit = svc.store.cache_get(key, ANALYSIS_TTL)
        if hit:
            hit["from_cache"] = True
            return hit

    url, vid = p["url"], p["video_id"]
    ctx.progress(4, "info", "Mengambil info video")
    info = svc.ytdlp.fetch_info(url, vid, ctx.hooks)
    if info.get("is_live"):
        raise InputError("Livestream yang sedang berlangsung belum bisa dianalisis.")
    duration = float(info.get("duration") or 0)
    if duration < 20:
        raise InputError("Video terlalu pendek untuk dianalisis.")
    heatmap = normalize_heatmap(info.get("heatmap"))
    window = window_for(p["preset"], p.get("min_len"), p.get("max_len"))

    ctx.progress(12, "transcript", "Mengambil transkrip")
    words, tsource = get_transcript(svc, url, vid, info, p.get("language", "auto"), ctx)
    segments = make_segments(words)

    notes: list[str] = []
    candidates: list[dict] = []
    models_used: list[str] = []
    if not heatmap:
        notes.append("Video ini tidak punya heatmap retensi (biasanya karena penontonnya masih sedikit).")
    if tsource == "none":
        notes.append("Transkrip tidak tersedia, analisis AI dilewati.")

    if api_key and api_key != "mock" and segments:
        try:
            ctx.progress(30, "ai", "Mencari model Gemini")
            models = gemini.list_models(api_key, svc.settings.gemini_models)
            candidates, models_used, log = gemini.find_candidates(
                api_key, segments, duration, info["title"], window=window, max_clips=p["max_clips"],
                language=p.get("language", "auto"), focus=p.get("focus", ""),
                peaks=heat_peaks(heatmap, 12, 30), models=models,
                on_progress=lambda f, m: ctx.progress(30 + 55 * f, "ai", m))
            if any("quota" in x or "unavailable" in x for x in log):
                notes.append("Sebagian model Gemini penuh/sibuk, otomatis pakai model lain: " + "; ".join(log))
        except gemini.GeminiError as e:
            if e.kind == "auth":
                raise InputError(str(e)) from None
            notes.append(f"Gemini tidak bisa dipakai ({e}). Klip dipilih dari heatmap saja.")
    elif api_key == "mock":
        notes.append("Mode demo: tanpa AI, klip dipilih dari heatmap.")
    elif not api_key:
        notes.append("Tanpa API key Gemini: klip dipilih dari heatmap saja.")

    ctx.progress(88, "fuse", "Menggabungkan skor")
    moments = fuse(candidates, heatmap, segments, duration, window, p["max_clips"]) if candidates else []
    if not moments:
        moments = fuse(heuristic_candidates(heatmap, segments, duration, window, p["max_clips"]),
                       heatmap, segments, duration, window, p["max_clips"])
    for i, m in enumerate(moments, 1):
        if not m.title:
            m.title = f"Klip {i}"

    result = {
        "video": {"id": vid, "url": url, "title": info["title"], "channel": info.get("channel", ""),
                  "channel_url": info.get("channel_url", ""), "duration": duration, "thumbnail": info.get("thumbnail", "")},
        "heatmap": heatmap,
        "moments": [dict(m.to_dict(), id=f"c{i}") for i, m in enumerate(moments, 1)],
        "meta": {"mode": mode, "transcript": tsource, "models": models_used, "notes": notes,
                 "window": {"min": window[0], "max": window[1], "target": window[2]}},
        "from_cache": False,
    }
    svc.store.cache_set(key, result)
    return result


# --------------------------------------------------------------------------- style -> opsi render
def options_from_style(style: dict, overlay_title: str = "") -> RenderOptions:
    """Validasi + ubah dict gaya dari klien menjadi RenderOptions. Semua kesalahan input -> InputError (HTTP 400)."""
    try:
        return _options_from_style(style, overlay_title)
    except (ValueError, TypeError, KeyError, AttributeError) as e:
        if isinstance(e, InputError):
            raise
        raise InputError(f"Pengaturan gaya tidak valid: {e}") from None


def _options_from_style(style: dict, overlay_title: str) -> RenderOptions:
    lay = style.get("layout") or {}
    cam = lay.get("cam") or [0.0, 0.0, 0.30, 0.38]
    layout = LayoutSpec(mode=lay.get("mode", "fill_face"), aspect=lay.get("aspect", "16:9"),
                        backdrop=lay.get("backdrop", "blur"), cam=tuple(float(x) for x in cam),  # type: ignore[arg-type]
                        game_fit=lay.get("game_fit", "cover"), pip_corner=lay.get("pip_corner", "tr"),
                        pip_scale=float(lay.get("pip_scale", 0.36)),
                        seam_caption=bool(lay.get("seam_caption", True))).validate()
    cap = style.get("captions") or {}
    captions = None
    if cap.get("enabled", True):
        preset = cap.get("preset", "viral_pop")
        if preset not in PRESETS:
            raise InputError(f"Preset caption tidak dikenal: {preset}")
        captions = CaptionStyle(preset=preset, font=cap.get("font", "Montserrat"), size=int(cap.get("size", 84)),
                                y_frac=float(cap.get("y_frac", 0.70)),
                                words_per_chunk=int(cap.get("words_per_chunk", 3)), uppercase=cap.get("uppercase"))
    tt = style.get("title") or {}
    title = None
    text = (overlay_title or tt.get("text") or "").strip()
    if tt.get("enabled", False) and text:
        dur = tt.get("duration")
        title = TitleSpec(text=text, y_frac=float(tt.get("y_frac", 0.12)),
                          duration=None if dur in (None, 0, "full") else float(dur), size=int(tt.get("size", 66)))
    q = style.get("quality", "balanced")
    if q not in ("fast", "balanced", "high"):
        raise InputError("quality harus fast, balanced, atau high")
    enc = style.get("encoder", "auto")
    if enc not in ("auto", "gpu", "cpu"):
        raise InputError("encoder harus auto, gpu, atau cpu")
    return RenderOptions(layout=layout, captions=captions, title=title, quality=q,
                         resolution=720 if int(style.get("resolution", 1080)) == 720 else 1080, encoder=enc,
                         loudnorm=bool(style.get("loudnorm", True)), export_srt=bool(style.get("export_srt", False)))


def slugify(text: str, fallback: str = "klip") -> str:
    s = re.sub(r"[^A-Za-z0-9]+", "-", text or "").strip("-").lower()[:40]
    return s or fallback


# --------------------------------------------------------------------------- render
def run_render(svc: Services, ctx: JobContext, p: dict) -> dict:
    clip, style = p["clip"], p["style"]
    start, end = float(clip["start"]), float(clip["end"])
    if not (0 <= start < end) or end - start > svc.settings.max_clip_seconds or end - start < 3:
        raise InputError(f"Durasi klip harus 3–{svc.settings.max_clip_seconds} detik.")
    opts = options_from_style(style, clip.get("overlay_title", ""))
    url, vid = p["url"], p["video_id"]
    height = 720 if opts.resolution == 720 else 1080

    ctx.progress(1, "download", "Mengunduh potongan video")
    src = svc.ytdlp.download_section(url, vid, start, end, height, ctx.hooks,
                                     on_percent=lambda f: ctx.progress(1 + 34 * f, "download", f"Unduh {int(f * 100)}%"))
    if ctx.is_cancelled():
        return {}

    words: list[Word] = []
    if opts.captions is not None:
        ctx.progress(36, "transcript", "Menyiapkan caption")
        info = svc.ytdlp.fetch_info(url, vid, ctx.hooks)
        allw, tsource = get_transcript(svc, url, vid, info, p.get("language", "auto"), ctx)
        words = words_in_range(allw, start, end)
        if not words and tsource == "none" and whisper_available():
            words = whisper_words(str(src), svc.settings.whisper_model, svc.settings.whisper_device, None)
    else:
        info = _info_for_credit(svc, url, vid, ctx)

    name = f"{p['batch_id']}_{p['index']:02d}_{slugify(clip.get('title', ''), clip.get('id', 'klip'))}.mp4"
    out = svc.settings.exports_dir / name
    work = svc.settings.work_dir / f"render_{ctx.job_id}"
    try:
        res = render_clip(src, out, opts, words, work, svc.settings.fonts_dir, ffmpeg=svc.settings.ffmpeg_bin,
                          ffprobe=svc.settings.ffprobe_bin,
                          on_progress=lambda stage, pct, msg: ctx.progress(36 + 0.64 * pct, stage, msg),
                          is_cancelled=ctx.is_cancelled, register=ctx.register,
                          face_detector=svc.settings.face_detector,
                          yunet_model=svc.settings.yunet_model or None, models_dir=svc.settings.models_dir)
    finally:
        shutil.rmtree(work, ignore_errors=True)
    res.update({"title": clip.get("title", ""), "hook": clip.get("hook", ""), "hashtags": clip.get("hashtags", []),
                "reason": clip.get("reason", ""), "start": start, "end": end, "clip_id": clip.get("id"),
                "caption_words": len(words),
                # kredit sumber: kanal asal video, dari data yt-dlp (bukan dari klien)
                "source_channel": info.get("channel", ""), "source_channel_url": info.get("channel_url", ""),
                "source_url": url})
    return res


def _info_for_credit(svc: Services, url: str, vid: str, ctx: JobContext) -> dict:
    """Info video hanya untuk kredit sumber; gagal mengambilnya tidak boleh menggagalkan render."""
    try:
        return svc.ytdlp.fetch_info(url, vid, ctx.hooks)
    except (CommandError, OSError, ValueError):
        return {}


# --------------------------------------------------------------------------- penyimpanan
def dir_size(path: Path) -> tuple[int, int]:
    total = count = 0
    if path.exists():
        for f in path.rglob("*"):
            if f.is_file():
                try:
                    total += f.stat().st_size
                    count += 1
                except OSError:
                    pass
    return count, total


def human(n: float) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} GB"


def storage_info(settings: Settings) -> dict:
    parts = {"temp": settings.work_dir, "slices": settings.cache_dir / "slices",
             "frames": settings.cache_dir / "frames", "exports": settings.exports_dir}
    out = {}
    for k, d in parts.items():
        c, s = dir_size(d)
        out[k] = {"files": c, "bytes": s, "human": human(s)}
    cleanable = sum(out[k]["bytes"] for k in ("temp", "slices", "frames"))
    out["cleanable"] = {"bytes": cleanable, "human": human(cleanable)}
    return out


def clear_temp(settings: Settings, busy: bool) -> dict:
    """Hapus work/slices/frames. Hasil render (exports) tidak disentuh. Ditolak saat ada job berjalan."""
    if busy:
        raise InputError("Masih ada proses yang berjalan. Tunggu selesai atau batalkan dulu.")
    freed = 0
    for d in (settings.work_dir, settings.cache_dir / "slices", settings.cache_dir / "frames"):
        c, s = dir_size(d)
        freed += s
        shutil.rmtree(d, ignore_errors=True)
        d.mkdir(parents=True, exist_ok=True)
    return {"freed_bytes": freed, "freed": human(freed)}


def purge_old(settings: Settings, store: Store) -> None:
    """Dipanggil saat start: buang cache lebih tua dari TTL agar disk tidak membengkak."""
    cutoff = time.time() - settings.cache_ttl_hours * 3600
    for d in (settings.cache_dir / "slices", settings.cache_dir / "frames",
               settings.cache_dir / "transcripts", settings.work_dir):
        if d.exists():
            for f in d.iterdir():
                try:
                    if f.stat().st_mtime < cutoff:
                        shutil.rmtree(f, ignore_errors=True) if f.is_dir() else f.unlink(missing_ok=True)
                except OSError:
                    pass
    store.purge_jobs(time.time() - 14 * 86400)

"""Wrapper yt-dlp (CLI via subprocess, tanpa shell). Semua pemanggilan bisa dibatalkan."""
from __future__ import annotations

import importlib.util
import json
import re
import shutil
import sys
import time
from pathlib import Path
from typing import Callable

from .config import Settings
from .procutil import CommandError, run_streaming

Hooks = dict  # {"register": fn, "is_cancelled": fn}

_FRIENDLY = (
    (r"Sign in to confirm|not a bot", "YouTube meminta verifikasi bot. Tambahkan cookies di menu Cookies lalu coba lagi."),
    (r"Private video|members-only|Join this channel", "Video privat atau khusus member. Butuh cookies akun yang punya akses."),
    (r"Video unavailable|This video is not available|has been removed", "Video tidak tersedia (dihapus / dibatasi wilayah)."),
    (r"HTTP Error 429|Too Many Requests", "YouTube membatasi permintaan (429). Tunggu beberapa menit atau pakai cookies."),
    (r"age|confirm your age", "Video dibatasi usia. Tambahkan cookies akun yang sudah login."),
    (r"ffmpeg|ffprobe", "ffmpeg tidak ditemukan / bermasalah. Pastikan ffmpeg terpasang di PATH."),
    (r"Requested format is not available|n challenge|JS runtime|EJS", "yt-dlp belum bisa mengekstrak format video ini. Perbarui yt-dlp "
     "(pip install -U yt-dlp) dan pastikan runtime JavaScript (mis. Deno/Node) tersedia bila diminta yt-dlp."),
)


def friendly_error(stderr: str) -> str:
    for pat, msg in _FRIENDLY:
        if re.search(pat, stderr, re.I):
            return msg
    tail = [ln for ln in stderr.strip().splitlines() if ln.strip()][-2:]
    return "yt-dlp gagal: " + " | ".join(t[:200] for t in tail) if tail else "yt-dlp gagal."


class YtDlp:
    def __init__(self, settings: Settings):
        self.s = settings
        self._stream_cache: dict[str, tuple[float, str]] = {}

    # ------------------------------------------------------------------ dasar
    def command(self) -> list[str]:
        if importlib.util.find_spec("yt_dlp") is not None:
            return [sys.executable, "-m", "yt_dlp"]
        return [shutil.which("yt-dlp") or "yt-dlp"]

    def available(self) -> bool:
        return importlib.util.find_spec("yt_dlp") is not None or shutil.which("yt-dlp") is not None

    def _base(self) -> list[str]:
        cmd = self.command() + ["--no-playlist", "--no-warnings", "--no-colors", "--retries", "3"]
        if self.s.ytdlp_proxy:
            cmd += ["--proxy", self.s.ytdlp_proxy]
        if self.s.cookies_file.is_file():
            cmd += ["--cookies", str(self.s.cookies_file)]
        if self.s.ffmpeg_bin != "ffmpeg":
            cmd += ["--ffmpeg-location", self.s.ffmpeg_bin]
        return cmd

    def _run(self, args: list[str], hooks: Hooks | None = None, timeout: float = 600,
             on_line: Callable[[str], None] | None = None) -> str:
        hooks = hooks or {}
        rc, out, err = run_streaming(self._base() + args, on_line=on_line, timeout=timeout, **hooks)
        if rc != 0:
            raise CommandError(friendly_error(err), rc)
        return out

    def version(self) -> str | None:
        try:
            rc, out, _ = run_streaming(self.command() + ["--version"], timeout=20)
            return out.strip() if rc == 0 else None
        except (OSError, CommandError):
            return None

    # ------------------------------------------------------------------ info
    def fetch_info(self, url: str, vid: str, hooks: Hooks | None = None, ttl: int = 3600) -> dict:
        cache = self.s.cache_dir / "info" / f"{vid}.json"
        if cache.is_file() and time.time() - cache.stat().st_mtime < ttl:
            try:
                return json.loads(cache.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                pass
        raw = json.loads(self._run(["-J", "--skip-download", url], hooks, timeout=120))
        info = {
            "id": raw.get("id") or vid,
            "title": raw.get("title") or "",
            "duration": float(raw.get("duration") or 0),
            "channel": raw.get("channel") or raw.get("uploader") or "",
            "thumbnail": raw.get("thumbnail") or "",
            "language": (raw.get("language") or "").split("-")[0],
            "description": (raw.get("description") or "")[:1200],
            "heatmap": raw.get("heatmap") or [],
            "chapters": [{"start": c.get("start_time"), "end": c.get("end_time"), "title": c.get("title")}
                         for c in (raw.get("chapters") or [])],
            "is_live": bool(raw.get("is_live")),
            "view_count": raw.get("view_count"),
            "url": url,
        }
        cache.write_text(json.dumps(info), encoding="utf-8")
        return info

    # ------------------------------------------------------------------ subtitle
    def fetch_subtitles(self, url: str, vid: str, languages: list[str], out_dir: Path,
                        hooks: Hooks | None = None) -> Path | None:
        out_dir.mkdir(parents=True, exist_ok=True)
        for old in out_dir.glob(f"{vid}.*.json3"):
            old.unlink(missing_ok=True)
        langs = ",".join(f"{l}.*" for l in languages if l) or "en.*"
        try:
            self._run(["--skip-download", "--write-subs", "--write-auto-subs", "--sub-langs", langs,
                       "--no-abort-on-error",
                       "--sub-format", "json3", "-o", str(out_dir / "%(id)s.%(ext)s"), url], hooks, timeout=180)
        except CommandError:
            pass
        files = sorted(out_dir.glob(f"{vid}.*.json3"))
        if not files:
            return None

        def rank(p: Path) -> tuple[int, int]:
            lang = p.name.split(".")[-2]
            for i, l in enumerate(languages):
                if lang == f"{l}-orig":
                    return (i, 0)
                if lang == l:
                    return (i, 1)
                if lang.startswith(l):
                    return (i, 2)
            return (99, 0)
        return min(files, key=rank)

    # ------------------------------------------------------------------ potong section
    def download_section(self, url: str, vid: str, start: float, end: float, max_height: int,
                         hooks: Hooks | None = None, on_percent: Callable[[float], None] | None = None) -> Path:
        slices = self.s.cache_dir / "slices"
        slices.mkdir(parents=True, exist_ok=True)
        stem = f"{vid}_{int(start * 10)}_{int(end * 10)}_{max_height}"
        hit = slices / f"{stem}.mp4"
        if hit.is_file() and hit.stat().st_size > 10_000:
            return hit
        fmt = f"bv*[height<={max_height}]+ba/b[height<={max_height}]/b"

        def on_line(line: str) -> None:
            m = re.search(r"\[download\]\s+(\d+(?:\.\d+)?)%", line)
            if m and on_percent:
                on_percent(float(m.group(1)) / 100)

        self._run(["-f", fmt, "--download-sections", f"*{start:.2f}-{end:.2f}", "--force-keyframes-at-cuts",
                   "--merge-output-format", "mp4", "--newline", "--concurrent-fragments", "4",
                   "-o", str(slices / f"{stem}.%(ext)s"), url], hooks, timeout=1800, on_line=on_line)
        if hit.is_file():
            return hit
        alt = [p for p in slices.glob(f"{stem}.*") if p.suffix not in (".part", ".ytdl")]
        if not alt:
            raise CommandError("File potongan video tidak ditemukan setelah unduh.")
        return alt[0]

    # ------------------------------------------------------------------ preview
    def stream_url(self, url: str, vid: str) -> str:
        hit = self._stream_cache.get(vid)
        if hit and time.time() - hit[0] < 600:
            return hit[1]
        out = self._run(["-g", "-f", "best[height<=720][ext=mp4]/best[height<=720]/best", url], timeout=90)
        first = next((ln.strip() for ln in out.splitlines() if ln.strip().startswith("http")), "")
        if not first:
            raise CommandError("Tidak mendapat URL stream untuk preview.")
        self._stream_cache[vid] = (time.time(), first)
        return first

    # ------------------------------------------------------------------ unduh penuh
    def download_raw(self, url: str, vid: str, out_dir: Path, max_height: int, hooks: Hooks | None = None,
                     on_progress: Callable[[dict], None] | None = None) -> Path:
        out_dir.mkdir(parents=True, exist_ok=True)
        stem = f"raw_{vid}_{max_height}"

        def num(s: str) -> float | None:
            try:
                return float(s)
            except ValueError:
                return None

        def on_line(line: str) -> None:
            if not line.startswith("CCPM|") or not on_progress:
                return
            _, done, total, est, speed, eta = (line.split("|") + [""] * 6)[:6]
            d, t = num(done), num(total) or num(est)
            on_progress({"downloaded": d, "total": t, "speed": num(speed), "eta": num(eta),
                         "percent": (d / t) if d is not None and t else None})

        tmpl = "download:CCPM|%(progress.downloaded_bytes)s|%(progress.total_bytes)s|" \
               "%(progress.total_bytes_estimate)s|%(progress.speed)s|%(progress.eta)s"
        self._run(["-f", f"bv*[height<={max_height}]+ba/b[height<={max_height}]/b", "--merge-output-format", "mp4",
                   "--newline", "--progress-template", tmpl, "-o", str(out_dir / f"{stem}.%(ext)s"), url],
                  hooks, timeout=6 * 3600, on_line=on_line)
        final = out_dir / f"{stem}.mp4"
        if final.is_file():
            return final
        alt = [p for p in out_dir.glob(f"{stem}.*") if p.suffix not in (".part", ".ytdl")]
        if not alt:
            raise CommandError("File hasil unduhan tidak ditemukan.")
        return alt[0]

    def download_audio(self, url: str, vid: str, out_dir: Path, hooks: Hooks | None = None) -> Path:
        out_dir.mkdir(parents=True, exist_ok=True)
        stem = f"audio_{vid}"
        self._run(["-f", "bestaudio/best", "-x", "--audio-format", "m4a", "-o", str(out_dir / f"{stem}.%(ext)s"), url],
                  hooks, timeout=3600)
        files = [p for p in out_dir.glob(f"{stem}.*") if p.suffix not in (".part", ".ytdl")]
        if not files:
            raise CommandError("Audio tidak ditemukan setelah unduh.")
        return files[0]

    def self_update(self) -> str:
        if not self.s.allow_self_update:
            raise CommandError("Pembaruan otomatis dimatikan. Set ALLOW_SELF_UPDATE=1 untuk mengaktifkan.")
        rc, out, err = run_streaming([sys.executable, "-m", "pip", "install", "-U", "yt-dlp"], timeout=300)
        if rc != 0:
            raise CommandError("Gagal memperbarui yt-dlp: " + err.strip()[-200:])
        return self.version() or "ok"

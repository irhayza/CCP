"""Konfigurasi terpusat. Semua nilai bisa di-override lewat environment / file .env."""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

VERSION = "1.0.0"
ROOT = Path(__file__).resolve().parent.parent

DEFAULT_ALLOWED_HOSTS = (
    "youtube.com", "www.youtube.com", "m.youtube.com", "music.youtube.com", "youtu.be",
)
# Dipakai hanya jika daftar model dinamis dari Google gagal diambil.
DEFAULT_GEMINI_FALLBACK = ("gemini-2.5-flash", "gemini-2.5-flash-lite", "gemini-2.0-flash")


def _read_dotenv(path: Path) -> dict[str, str]:
    out: dict[str, str] = {}
    if not path.is_file():
        return out
    for raw in path.read_text(encoding="utf-8", errors="ignore").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, val = line.partition("=")
        out[key.strip()] = val.strip().strip('"').strip("'")
    return out


def _bool(v: str | None, default: bool = False) -> bool:
    if v is None or v == "":
        return default
    return v.strip().lower() in {"1", "true", "yes", "on"}


def _choice(v: str | None, allowed: tuple[str, ...], default: str) -> str:
    v = (v or "").strip().lower()
    return v if v in allowed else default


def _int(v: str | None, default: int, lo: int, hi: int) -> int:
    try:
        n = int(v) if v not in (None, "") else default
    except ValueError:
        n = default
    return max(lo, min(hi, n))


@dataclass(frozen=True)
class Settings:
    root: Path
    data_dir: Path
    fonts_dir: Path
    frontend_dir: Path
    host: str
    port: int
    app_token: str
    allowed_hosts: tuple[str, ...]
    render_workers: int
    max_clip_seconds: int
    cache_ttl_hours: int
    gemini_models: tuple[str, ...]
    ffmpeg_bin: str
    ffprobe_bin: str
    ytdlp_proxy: str
    whisper_model: str
    whisper_device: str
    allow_self_update: bool
    face_detector: str = "auto"      # auto | yunet | haar
    yunet_model: str = ""            # path file .onnx YuNet; kosong = cari di DATA_DIR/models

    @property
    def models_dir(self) -> Path:
        return self.data_dir / "models"

    @property
    def cache_dir(self) -> Path:
        return self.data_dir / "cache"

    @property
    def exports_dir(self) -> Path:
        return self.data_dir / "exports"

    @property
    def work_dir(self) -> Path:
        return self.data_dir / "work"

    @property
    def cookies_file(self) -> Path:
        return self.data_dir / "cookies.txt"

    @property
    def db_path(self) -> Path:
        return self.data_dir / "promax.db"

    def ensure_dirs(self) -> None:
        for d in (self.data_dir, self.cache_dir, self.exports_dir, self.work_dir, self.models_dir,
                  self.cache_dir / "slices", self.cache_dir / "transcripts",
                  self.cache_dir / "frames", self.cache_dir / "info", self.fonts_dir):
            d.mkdir(parents=True, exist_ok=True)


def load_settings(env: dict[str, str] | None = None) -> Settings:
    merged = {**_read_dotenv(ROOT / ".env"), **(env if env is not None else os.environ)}
    g = merged.get
    data_dir = Path(g("DATA_DIR") or ROOT / "data").expanduser().resolve()
    hosts = tuple(h.strip().lower() for h in (g("ALLOWED_HOSTS") or "").split(",") if h.strip())
    models = tuple(m.strip() for m in (g("GEMINI_MODELS") or "").split(",") if m.strip())
    return Settings(
        root=ROOT,
        data_dir=data_dir,
        fonts_dir=ROOT / "backend" / "fonts",
        frontend_dir=ROOT / "frontend",
        host=g("HOST") or "127.0.0.1",
        port=_int(g("PORT"), 8000, 1, 65535),
        app_token=g("APP_TOKEN") or "",
        allowed_hosts=hosts or DEFAULT_ALLOWED_HOSTS,
        render_workers=_int(g("RENDER_WORKERS"), 2, 1, 8),
        max_clip_seconds=_int(g("MAX_CLIP_SECONDS"), 180, 5, 900),
        cache_ttl_hours=_int(g("CACHE_TTL_HOURS"), 72, 1, 24 * 365),
        gemini_models=models,
        ffmpeg_bin=g("FFMPEG_BIN") or "ffmpeg",
        ffprobe_bin=g("FFPROBE_BIN") or "ffprobe",
        ytdlp_proxy=g("YTDLP_PROXY") or "",
        whisper_model=g("WHISPER_MODEL") or "small",
        whisper_device=g("WHISPER_DEVICE") or "auto",
        allow_self_update=_bool(g("ALLOW_SELF_UPDATE")),
        face_detector=_choice(g("FACE_DETECTOR"), ("auto", "yunet", "haar"), "auto"),
        yunet_model=g("YUNET_MODEL") or "",
    )

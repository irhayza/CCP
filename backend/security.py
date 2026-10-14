"""Validasi input & pengaman. Semua fungsi murni agar mudah diuji."""
from __future__ import annotations

import hashlib
import hmac
import re
from pathlib import Path
from urllib.parse import parse_qs, urlparse

_YT_ID = re.compile(r"^[A-Za-z0-9_-]{11}$")
_SAFE_ID = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
_SAFE_FILE = re.compile(r"^[A-Za-z0-9_.-]{1,160}$")
_YT_HOSTS = {"youtube.com", "www.youtube.com", "m.youtube.com", "music.youtube.com"}


class InputError(ValueError):
    """Input pengguna tidak valid (diterjemahkan ke HTTP 400)."""


def parse_video_url(url: str, allowed_hosts: tuple[str, ...]) -> tuple[str, str]:
    """Kembalikan (url_kanonik, video_id). Hanya host allowlist; mencegah SSRF / file://."""
    if not isinstance(url, str) or len(url) > 500:
        raise InputError("URL tidak valid.")
    parsed = urlparse(url.strip())
    if parsed.scheme not in ("http", "https"):
        raise InputError("URL harus diawali http:// atau https://")
    host = (parsed.hostname or "").lower()
    if host not in allowed_hosts:
        raise InputError(f"Host '{host}' tidak diizinkan. Atur ALLOWED_HOSTS jika memang perlu.")
    vid = ""
    if host == "youtu.be":
        vid = parsed.path.strip("/").split("/")[0]
    elif host in _YT_HOSTS:
        if parsed.path == "/watch":
            vid = (parse_qs(parsed.query).get("v") or [""])[0]
        else:
            parts = [p for p in parsed.path.split("/") if p]
            if len(parts) >= 2 and parts[0] in {"shorts", "live", "embed", "v"}:
                vid = parts[1]
    if vid:
        if not _YT_ID.match(vid):
            raise InputError("ID video YouTube tidak valid.")
        return f"https://www.youtube.com/watch?v={vid}", vid
    if host in _YT_HOSTS or host == "youtu.be":
        raise InputError("Tidak menemukan ID video di URL tersebut.")
    clean = f"https://{host}{parsed.path}" + (f"?{parsed.query}" if parsed.query else "")
    return clean, "x" + hashlib.sha1(clean.encode()).hexdigest()[:10]


def valid_id(value: str) -> str:
    if not isinstance(value, str) or not _SAFE_ID.match(value):
        raise InputError("ID tidak valid.")
    return value


def safe_child(base: Path, name: str) -> Path:
    """Gabungkan nama file ke base tanpa bisa keluar dari base (anti path-traversal)."""
    if not isinstance(name, str) or not _SAFE_FILE.match(name) or name.startswith("."):
        raise InputError("Nama file tidak valid.")
    base = base.resolve()
    target = (base / name).resolve()
    if target.parent != base:
        raise InputError("Nama file tidak valid.")
    return target


def token_ok(expected: str, provided: str | None) -> bool:
    if not expected:
        return True
    return bool(provided) and hmac.compare_digest(expected.encode(), (provided or "").encode())


def redact(text: str, *secrets: str) -> str:
    """Hapus rahasia (API key, token) dari teks sebelum dicatat / dikirim ke klien."""
    out = text or ""
    for s in secrets:
        if s and len(s) >= 6:
            out = out.replace(s, "***")
    return re.sub(r"AIza[0-9A-Za-z_-]{20,}", "AIza***", out)


def validate_netscape_cookies(text: str, max_bytes: int = 512_000) -> dict:
    """Validasi format cookies.txt Netscape. Mengembalikan ringkasan tanpa membocorkan nilai cookie."""
    if len(text.encode("utf-8", "ignore")) > max_bytes:
        raise InputError("File cookies terlalu besar.")
    domains: dict[str, int] = {}
    count = 0
    for raw in text.splitlines():
        line = raw.strip()
        if not line or (line.startswith("#") and not line.startswith("#HttpOnly_")):
            continue
        cols = raw.rstrip("\r\n").split("\t")
        if len(cols) != 7:
            raise InputError("Format bukan Netscape cookies.txt (harus 7 kolom dipisah TAB).")
        dom = cols[0].replace("#HttpOnly_", "").lstrip(".").lower()
        domains[dom] = domains.get(dom, 0) + 1
        count += 1
    if count == 0:
        raise InputError("Tidak ada cookie yang ditemukan.")
    return {"count": count, "domains": sorted(domains)[:12]}

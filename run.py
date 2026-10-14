#!/usr/bin/env python3
"""Launcher satu perintah:  python run.py   (lalu buka http://127.0.0.1:8000)"""
from __future__ import annotations

import argparse
import os
import shutil
import sys
import threading
import webbrowser

LOOPBACK = {"127.0.0.1", "localhost", "::1"}


def main() -> int:
    if sys.version_info < (3, 10):
        print(f"Dibutuhkan Python 3.10 atau lebih baru (kamu memakai {sys.version.split()[0]}).", file=sys.stderr)
        return 1
    ap = argparse.ArgumentParser(description="Cheat Clip Pro Max")
    ap.add_argument("--host", help="default 127.0.0.1 (atau env HOST)")
    ap.add_argument("--port", type=int, help="default 8000 (atau env PORT)")
    ap.add_argument("--no-browser", action="store_true", help="jangan buka browser otomatis")
    ap.add_argument("--skip-checks", action="store_true", help="lewati pengecekan ffmpeg/dependensi")
    args = ap.parse_args()

    from backend.config import load_settings
    s = load_settings({**os.environ, **({"HOST": args.host} if args.host else {}), **({"PORT": str(args.port)} if args.port else {})})

    if not args.skip_checks:
        problems = []
        if not shutil.which(s.ffmpeg_bin):
            problems.append("ffmpeg tidak ditemukan di PATH. Windows: winget install Gyan.FFmpeg | macOS: brew install ffmpeg | Linux: apt install ffmpeg")
        for mod, pkg in (("fastapi", "fastapi"), ("uvicorn", "uvicorn"), ("yt_dlp", "yt-dlp")):
            try:
                __import__(mod)
            except ImportError:
                problems.append(f"paket Python '{pkg}' belum terpasang. Jalankan: python -m pip install -r requirements.txt")
        if problems:
            print("Tidak bisa mulai:\n  - " + "\n  - ".join(problems), file=sys.stderr)
            return 1

    if s.host not in LOOPBACK and not s.app_token and os.environ.get("ALLOW_OPEN_SERVER") != "1":
        print(f"Menolak mengikat ke {s.host} tanpa APP_TOKEN: siapa pun di jaringan bisa memakai cookies & API key-mu.\n"
              "Isi APP_TOKEN di file .env (disarankan), atau set ALLOW_OPEN_SERVER=1 bila kamu benar-benar yakin.", file=sys.stderr)
        return 2

    from backend.facetrack import find_yunet_model
    if s.face_detector != "haar" and find_yunet_model(s.yunet_model or None, s.models_dir) is None:
        print("Tip: face tracking lebih akurat dengan model YuNet (sekali pasang, ~230 KB):  python -m backend.models")

    import uvicorn
    url =f"http://{'127.0.0.1' if s.host in ('0.0.0.0', '::') else s.host}:{s.port}"
    if not args.no_browser and s.host in LOOPBACK:
        threading.Timer(1.5, lambda: webbrowser.open(url)).start()
    print(f"Cheat Clip Pro Max berjalan di {url}  (Ctrl+C untuk berhenti)")
    uvicorn.run("backend.asgi:app", host=s.host, port=s.port, log_level="info")
    return 0


if __name__ == "__main__":
    sys.exit(main())

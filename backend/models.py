"""Pengunduh model opsional. Saat ini hanya YuNet (detektor wajah OpenCV).

Hanya stdlib. Unduhan ditulis ke file `.part`, diperiksa ukuran dan SHA-256-nya, baru dipindah ke nama akhir:
file yang rusak atau diganti pihak lain tidak pernah sampai ke folder model. Ukuran maksimum dibatasi agar
server yang salah tidak bisa memenuhi disk.

    python -m backend.models              # pasang semua model yang dikenal ke DATA_DIR/models
    python -m backend.models yunet --dir ./data/models
"""
from __future__ import annotations

import argparse
import hashlib
import os
import sys
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Sequence

CHUNK = 64 * 1024


class ModelError(Exception):
    """Unduhan gagal atau tidak lolos verifikasi. Pesannya aman ditampilkan ke pengguna."""


@dataclass(frozen=True)
class ModelSpec:
    key: str
    filename: str
    urls: tuple[str, ...]          # dicoba berurutan; semuanya harus https
    sha256: str
    size: int
    license: str
    note: str = ""


# YuNet 2023mar dari opencv_zoo (MIT, (c) Shiqi Yu). SHA-256 dan ukuran dicocokkan dengan pointer Git LFS
# di repo opencv_zoo sendiri (oid sha256) dan dengan file hasil unduhan.
YUNET = ModelSpec(
    key="yunet",
    filename="face_detection_yunet_2023mar.onnx",
    urls=(
        # dikunci ke commit supaya tidak ikut berubah bila `main` direstrukturisasi; `main` sebagai cadangan
        "https://media.githubusercontent.com/media/opencv/opencv_zoo/47534e27c9851bb1128ccc0102f1145e27f23f98/"
        "models/face_detection_yunet/face_detection_yunet_2023mar.onnx",
        "https://media.githubusercontent.com/media/opencv/opencv_zoo/main/"
        "models/face_detection_yunet/face_detection_yunet_2023mar.onnx",
    ),
    sha256="8f2383e4dd3cfbb4553ea8718107fc0423210dc964f9f4280604804ed2552fa4",
    size=232589,
    license="MIT (opencv_zoo)",
    note="Detektor wajah CNN kecil; lebih tahan wajah menyamping/terhalang daripada Haar.",
)

MODELS: dict[str, ModelSpec] = {YUNET.key: YUNET}

Opener = Callable[..., object]


def file_ok(path: Path, spec: ModelSpec) -> bool:
    """True bila file ada, ukurannya pas, dan SHA-256-nya cocok."""
    try:
        if not path.is_file() or path.stat().st_size != spec.size:
            return False
        h = hashlib.sha256()
        with open(path, "rb") as f:
            for chunk in iter(lambda: f.read(CHUNK), b""):
                h.update(chunk)
        return h.hexdigest() == spec.sha256
    except OSError:
        return False


def _download(url: str, tmp: Path, spec: ModelSpec, opener: Opener, timeout: float) -> None:
    if not url.lower().startswith("https://"):
        raise ModelError("Hanya URL https yang diizinkan.")
    req = urllib.request.Request(url, headers={"User-Agent": "cheat-clip-pro-max/models"})
    h = hashlib.sha256()
    total = 0
    limit = spec.size + 1024            # sedikit longgar; ukuran pasti diperiksa setelah selesai
    with opener(req, timeout=timeout) as resp, open(tmp, "wb") as out:      # type: ignore[operator]
        while True:
            chunk = resp.read(CHUNK)
            if not chunk:
                break
            total += len(chunk)
            if total > limit:
                raise ModelError(f"Ukuran unduhan melebihi {spec.size} byte; dibatalkan.")
            h.update(chunk)
            out.write(chunk)
    if total != spec.size:
        raise ModelError(f"Ukuran file {total} byte, seharusnya {spec.size}.")
    if h.hexdigest() != spec.sha256:
        raise ModelError("SHA-256 tidak cocok; file tidak dipasang.")


def fetch(spec: ModelSpec, dest_dir: str | Path, *, force: bool = False, opener: Opener = urllib.request.urlopen,
          timeout: float = 60.0, log: Callable[[str], None] = lambda _m: None) -> Path:
    """Pasang model ke `dest_dir` dan kembalikan path-nya. Idempoten: file yang sudah benar tidak diunduh ulang."""
    dest = Path(dest_dir)
    dest.mkdir(parents=True, exist_ok=True)
    final = dest / spec.filename
    if not force and file_ok(final, spec):
        log(f"{spec.key}: sudah terpasang ({final})")
        return final
    tmp = dest / (spec.filename + ".part")
    errors: list[str] = []
    for url in spec.urls:
        try:
            log(f"{spec.key}: mengunduh {url}")
            _download(url, tmp, spec, opener, timeout)
            os.replace(tmp, final)
            return final
        except (OSError, ModelError) as e:                 # urllib.error.* adalah subclass OSError
            errors.append(f"{url.split('/')[2]}: {e}")
        finally:
            try:
                tmp.unlink()
            except OSError:
                pass
    raise ModelError(f"Gagal memasang {spec.key}. " + " | ".join(errors))


def main(argv: Sequence[str] | None = None, *, opener: Opener = urllib.request.urlopen) -> int:
    ap = argparse.ArgumentParser(prog="python -m backend.models", description="Pasang model opsional Cheat Clip Pro Max")
    ap.add_argument("names", nargs="*", help=f"model yang dipasang (default semua): {', '.join(MODELS)}")
    ap.add_argument("--dir", help="folder tujuan (default: DATA_DIR/models)")
    ap.add_argument("--force", action="store_true", help="unduh ulang walau file sudah ada dan benar")
    args = ap.parse_args(argv)

    names = args.names or list(MODELS)
    unknown = [n for n in names if n not in MODELS]
    if unknown:
        print(f"Model tidak dikenal: {', '.join(unknown)}. Pilihan: {', '.join(MODELS)}", file=sys.stderr)
        return 2
    if args.dir:
        dest = Path(args.dir)
    else:
        from .config import load_settings
        dest = load_settings().models_dir
    rc = 0
    for n in names:
        spec = MODELS[n]
        try:
            path = fetch(spec, dest, force=args.force, opener=opener, log=print)
            print(f"OK  {spec.key} -> {path}  [{spec.license}]")
        except ModelError as e:
            print(f"GAGAL  {e}", file=sys.stderr)
            rc = 1
    return rc


if __name__ == "__main__":
    sys.exit(main())

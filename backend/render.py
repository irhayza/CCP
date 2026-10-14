"""Renderer klip: probe -> face tracking -> ASS -> ffmpeg (progress real-time, bisa dibatalkan,
fallback otomatis ke CPU bila encoder GPU gagal di tengah jalan)."""
from __future__ import annotations

import json
import os
import re
import subprocess
import threading
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Callable

from . import facetrack, scenes
from .captions import CaptionStyle, TitleSpec, Word, build_ass, build_srt
from .hwenc import CPU, Encoder, pick_encoder
from .layouts import LayoutSpec, build_filtergraph, cover_crop, even
from .procutil import Cancelled, spawn

ProgressCb = Callable[[str, float, str], None]   # (stage, persen 0..100, pesan)


class RenderCancelled(Exception):
    pass


class RenderError(RuntimeError):
    pass


@dataclass
class RenderOptions:
    layout: LayoutSpec = field(default_factory=LayoutSpec)
    captions: CaptionStyle | None = field(default_factory=CaptionStyle)   # None = tanpa caption
    title: TitleSpec | None = None
    quality: str = "balanced"          # fast | balanced | high
    resolution: int = 1080             # lebar output: 720 | 1080
    encoder: str = "auto"              # auto | gpu | cpu
    loudnorm: bool = True
    max_fps: int = 30
    export_srt: bool = False


@dataclass
class MediaInfo:
    width: int
    height: int
    duration: float
    fps: float
    has_audio: bool


def _fraction(s: str) -> float:
    try:
        if "/" in s:
            a, b = s.split("/")
            return float(a) / float(b) if float(b) else 0.0
        return float(s)
    except (ValueError, ZeroDivisionError):
        return 0.0


def probe(path: str | Path, ffprobe: str = "ffprobe") -> MediaInfo:
    def run(args: list[str]) -> dict:
        r = subprocess.run([ffprobe, "-v", "error", *args, "-of", "json", str(path)],
                           capture_output=True, text=True, timeout=60)
        if r.returncode != 0:
            raise RenderError(f"ffprobe gagal: {r.stderr.strip()[:300]}")
        return json.loads(r.stdout or "{}")

    v = run(["-select_streams", "v:0", "-show_entries", "stream=width,height,r_frame_rate:format=duration"])
    streams = v.get("streams") or []
    if not streams:
        raise RenderError("File tidak berisi stream video.")
    s = streams[0]
    a = run(["-select_streams", "a:0", "-show_entries", "stream=codec_type"])
    dur = float((v.get("format") or {}).get("duration") or 0.0)
    return MediaInfo(int(s["width"]), int(s["height"]), dur, _fraction(s.get("r_frame_rate", "30/1")) or 30.0,
                     bool(a.get("streams")))


def _run_ffmpeg(cmd: list[str], duration: float, on_progress: Callable[[float], None],
                register: Callable[[subprocess.Popen], None] | None, is_cancelled: Callable[[], bool],
                cwd: str | None) -> None:
    proc = spawn(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                 encoding="utf-8", errors="replace", cwd=cwd)
    if register:
        register(proc)
    err_lines: list[str] = []

    def drain() -> None:
        if proc.stderr is None:
            return
        for line in proc.stderr:
            err_lines.append(line)
            del err_lines[:-60]

    t = threading.Thread(target=drain, daemon=True)
    t.start()
    if proc.stdout is None:
        raise RenderError("stdout pipe tidak tersedia")
    for line in proc.stdout:
        m = re.match(r"out_time=(\d+):(\d+):(\d+(?:\.\d+)?)", line.strip())
        if m and duration > 0:
            sec = int(m.group(1)) * 3600 + int(m.group(2)) * 60 + float(m.group(3))
            on_progress(min(1.0, sec / duration))
    proc.wait()
    t.join(timeout=2)
    proc.stdout.close()
    if proc.stderr:
        proc.stderr.close()
    if is_cancelled():
        raise RenderCancelled()
    if proc.returncode != 0:
        raise RenderError("ffmpeg gagal: " + "".join(err_lines).strip()[-900:])


def build_command(ffmpeg: str, source: str, out: str, graph: str, enc: Encoder, opts: RenderOptions,
                  has_audio: bool) -> list[str]:
    cmd = [ffmpeg, "-y", "-hide_banner", "-loglevel", "error", "-nostats", "-progress", "pipe:1",
           "-i", source, "-filter_complex", graph, "-map", "[vout]"]
    if has_audio:
        cmd += ["-map", "0:a:0"]
    cmd += enc.video_args(opts.quality)
    if has_audio:
        cmd += ["-c:a", "aac", "-b:a", "160k", "-ar", "48000"]
        if opts.loudnorm:
            cmd += ["-af", "loudnorm=I=-14:TP=-1.5:LRA=11"]
    cmd += ["-pix_fmt", "yuv420p", "-movflags", "+faststart", "-f", "mp4", out]
    return cmd


def _scene_cuts(source: Path, ffmpeg: str, register, is_cancelled: Callable[[], bool]) -> list[float]:
    """Waktu cut dari `scenes.detect_cuts`; pembatalan dipetakan ke RenderCancelled."""
    try:
        cuts = scenes.detect_cuts(str(source), ffmpeg=ffmpeg, register=register, is_cancelled=is_cancelled)
    except Cancelled:
        raise RenderCancelled() from None
    if is_cancelled():
        raise RenderCancelled()
    return cuts


def render_clip(source: str | Path, out_path: str | Path, opts: RenderOptions, words: list[Word],
                workdir: str | Path, fonts_dir: str | Path, *, ffmpeg: str = "ffmpeg", ffprobe: str = "ffprobe",
                on_progress: ProgressCb | None = None, is_cancelled: Callable[[], bool] = lambda: False,
                register: Callable[[subprocess.Popen], None] | None = None,
                face_detector: str = "auto", yunet_model: str | None = None,
                models_dir: str | Path | None = None) -> dict:
    cb: ProgressCb = on_progress or (lambda *_: None)
    workdir, out_path = Path(workdir).resolve(), Path(out_path).resolve()
    source = Path(source).resolve()
    workdir.mkdir(parents=True, exist_ok=True)
    opts.layout.validate()

    info = probe(source, ffprobe)
    if info.duration <= 0:
        raise RenderError("Durasi klip tidak terbaca.")
    out_w = 720 if opts.resolution == 720 else 1080
    out_h = out_w * 16 // 9
    notes: list[str] = []

    # 1) face tracking (Ikuti wajah: satu jalur; Dua pembicara: dua jalur, satu per panel)
    layout = opts.layout
    face_keys = None
    if layout.mode in ("fill_face", "duo"):
        cb("tracking", 2, "Melacak wajah pembicara")
        duo = layout.mode == "duo"
        pane_ar = out_w / even(out_h / 2) if duo else out_w / out_h              # panel layout duo: 9:8
        cw, _ = cover_crop(info.width, info.height, pane_ar)
        if cw < info.width:
            try:
                detector, det_note = facetrack.load_detector(face_detector, model_path=yunet_model or None,
                                                             models_dir=models_dir)
            except RuntimeError as e:            # tidak ada detektor yang bisa dimuat -> crop tengah (lihat bawah)
                detector = None
                det_note = (f"Detektor wajah tidak tersedia ({str(e).strip() or 'OpenCV tidak lengkap'}). "
                            "Pasang model YuNet dengan: python -m backend.models")
            if det_note:
                notes.append(det_note)

            samples = None
            if duo:
                scan = facetrack.scan_faces(str(source), should_stop=is_cancelled, detector=detector)
                if is_cancelled():
                    raise RenderCancelled()
                track_a, track_b, both = facetrack.split_two(scan)
                if both >= max(2, len(scan) * 0.25):
                    cuts = _scene_cuts(source, ffmpeg, register, is_cancelled)
                    dt = facetrack.sample_interval(track_a, info.duration)
                    frac = cw / info.width
                    layout.face_keys = facetrack.smooth_path(track_a, info.duration, dt, frac, cuts=cuts)
                    layout.face_keys_b = facetrack.smooth_path(track_b, info.duration, dt, frac, cuts=cuts)
                    face_keys = layout.face_keys
                else:
                    # Kurang dari dua orang terlihat bersamaan: tumpukan dua panel akan menampilkan orang yang sama
                    # dua kali. Pakai layout Ikuti wajah dengan hasil pindai yang sama (tanpa memindai ulang).
                    notes.append("Dua wajah tidak terdeteksi stabil bersamaan; memakai layout Ikuti wajah.")
                    layout = replace(layout, mode="fill_face", face_keys=None, face_keys_b=None)
                    duo = False
                    cw, _ = cover_crop(info.width, info.height, out_w / out_h)
                    samples = facetrack.faces_from_scan(scan) if cw < info.width else None
            else:
                samples = facetrack.detect_faces(str(source), should_stop=is_cancelled, detector=detector)
                if is_cancelled():
                    raise RenderCancelled()

            if samples is not None:
                found = sum(1 for s in samples if s)
                if found >= max(2, len(samples) * 0.15):
                    cuts = _scene_cuts(source, ffmpeg, register, is_cancelled)
                    dt = facetrack.sample_interval(samples, info.duration)
                    face_keys = facetrack.smooth_path(samples, info.duration, dt, cw / info.width, cuts=cuts)
                else:
                    notes.append("Wajah tidak terdeteksi stabil; memakai crop tengah.")
        elif duo:       # sumber (hampir) selebar panel, mis. video 1:1 atau potret: dua panel akan identik
            notes.append("Video terlalu sempit untuk dua panel; memakai layout Ikuti wajah.")
            layout = replace(layout, mode="fill_face", face_keys=None, face_keys_b=None)
    if layout.mode != "duo":
        layout.face_keys = face_keys

    # 2) caption (layout duo: di sambungan kedua panel, kecuali dimatikan)
    cap_style = opts.captions
    if cap_style is not None and layout.mode == "duo" and layout.seam_caption:
        cap_style = replace(cap_style, y_frac=0.5)
    ass_name = None
    if cap_style is not None or opts.title is not None:
        cb("captions", 12, "Menyusun caption")
        ass_name = "captions.ass"
        (workdir / ass_name).write_text(
            build_ass(words if cap_style is not None else [], info.duration,
                      cap_style or CaptionStyle(), opts.title), encoding="utf-8")
        if cap_style is not None and not words:
            notes.append("Transkrip tidak tersedia: klip dirender tanpa caption.")
    if opts.export_srt and words:
        out_path.with_suffix(".srt").write_text(build_srt(words), encoding="utf-8")

    graph = build_filtergraph(layout, info.width, info.height, out_w, out_h, ass_file=ass_name,
                              fonts_dir=str(Path(fonts_dir).resolve()), max_fps=opts.max_fps, src_fps=info.fps)

    # 3) encode (GPU -> CPU fallback)
    part = out_path.with_name(out_path.stem + ".part.mp4")
    encoders = [pick_encoder(opts.encoder, ffmpeg)]
    if encoders[0].hw:
        encoders.append(CPU)
    used = encoders[0]
    last_err: Exception | None = None
    for i, enc in enumerate(encoders):
        used = enc
        cb("encoding", 15, f"Render dengan {enc.label}")
        cmd = build_command(ffmpeg, str(source), str(part), graph, enc, opts, info.has_audio)
        try:
            _run_ffmpeg(cmd, info.duration, lambda f: cb("encoding", 15 + 85 * f, f"Render {int(f * 100)}%"),
                        register, is_cancelled, cwd=str(workdir))
            last_err = None
            break
        except RenderCancelled:
            part.unlink(missing_ok=True)
            raise
        except RenderError as e:
            last_err = e
            part.unlink(missing_ok=True)
            if i + 1 < len(encoders):
                notes.append(f"{enc.label} gagal, otomatis pindah ke CPU.")
    if last_err:
        raise last_err
    os.replace(part, out_path)

    thumb = out_path.with_suffix(".jpg")
    subprocess.run([ffmpeg, "-y", "-loglevel", "error", "-ss", f"{info.duration * 0.25:.2f}", "-i", str(out_path),
                    "-frames:v", "1", "-vf", "scale=360:-2", str(thumb)], capture_output=True, timeout=60)
    cb("done", 100, "Selesai")
    return {
        "file": out_path.name,
        "thumbnail": thumb.name if thumb.exists() else None,
        "duration": round(info.duration, 2),
        "encoder": used.label,
        "face_tracked": face_keys is not None,
        "size_bytes": out_path.stat().st_size,
        "notes": notes,
    }

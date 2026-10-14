"""Deteksi encoder video. Encoder GPU hanya dipakai jika test-encode nyata berhasil,
karena muncul di `ffmpeg -encoders` tidak berarti driver/GPU-nya ada."""
from __future__ import annotations

import subprocess
from dataclasses import dataclass
from functools import lru_cache

QUALITY = {
    # nama: (crf x264, cq nvenc/qsv/amf, preset x264)
    "fast": (24, 27, "superfast"),
    "balanced": (21, 23, "veryfast"),
    "high": (18, 19, "medium"),
}


@dataclass(frozen=True)
class Encoder:
    name: str      # nama encoder ffmpeg
    label: str
    hw: bool

    def video_args(self, quality: str = "balanced") -> list[str]:
        crf, cq, preset = QUALITY.get(quality, QUALITY["balanced"])
        if self.name == "libx264":
            return ["-c:v", "libx264", "-preset", preset, "-crf", str(crf), "-profile:v", "high"]
        if self.name == "h264_nvenc":
            return ["-c:v", "h264_nvenc", "-preset", "p5", "-rc", "vbr", "-cq", str(cq),
                    "-b:v", "0", "-profile:v", "high"]
        if self.name == "h264_qsv":
            return ["-c:v", "h264_qsv", "-preset", "medium", "-global_quality", str(cq)]
        if self.name == "h264_amf":
            return ["-c:v", "h264_amf", "-quality", "quality", "-rc", "cqp",
                    "-qp_i", str(cq), "-qp_p", str(cq + 2)]
        if self.name == "h264_videotoolbox":
            return ["-c:v", "h264_videotoolbox", "-b:v", {"fast": "6M", "balanced": "9M", "high": "14M"}.get(quality, "9M")]
        raise ValueError(f"Encoder tidak dikenal: {self.name}")


CPU = Encoder("libx264", "CPU (libx264)", False)
_GPU_CANDIDATES = (
    Encoder("h264_nvenc", "NVIDIA NVENC", True),
    Encoder("h264_qsv", "Intel Quick Sync", True),
    Encoder("h264_amf", "AMD AMF", True),
    Encoder("h264_videotoolbox", "Apple VideoToolbox", True),
)


def _listed(ffmpeg: str) -> str:
    try:
        r = subprocess.run([ffmpeg, "-hide_banner", "-encoders"], capture_output=True, text=True, timeout=20)
        return r.stdout
    except (OSError, subprocess.SubprocessError):
        return ""


def _works(ffmpeg: str, enc: Encoder) -> bool:
    """Encode 3 frame hitam 256x256 memakai argumen produksi persis."""
    cmd = [ffmpeg, "-hide_banner", "-loglevel", "error", "-f", "lavfi",
           "-i", "color=c=black:s=256x256:r=25:d=0.2", *enc.video_args("balanced"),
           "-pix_fmt", "yuv420p", "-f", "null", "-"]
    try:
        return subprocess.run(cmd, capture_output=True, timeout=25).returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


@lru_cache(maxsize=4)
def detect_gpu_encoder(ffmpeg: str = "ffmpeg") -> Encoder | None:
    listed = _listed(ffmpeg)
    for enc in _GPU_CANDIDATES:
        if enc.name in listed and _works(ffmpeg, enc):
            return enc
    return None


def pick_encoder(preference: str = "auto", ffmpeg: str = "ffmpeg") -> Encoder:
    """preference: auto | gpu | cpu. 'gpu' tanpa GPU yang berfungsi tetap jatuh ke CPU."""
    if preference == "cpu":
        return CPU
    return detect_gpu_encoder(ffmpeg) or CPU

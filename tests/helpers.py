import json, shutil, subprocess, tempfile
from pathlib import Path
from backend.config import load_settings
from backend.db import Store
from backend.pipeline import Services

HAVE_FFMPEG = shutil.which("ffmpeg") is not None


def make_video(path: Path, seconds: float, w=1920, h=1080):
    subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-f", "lavfi", "-i", f"testsrc2=s={w}x{h}:r=30:d={seconds}",
                    "-f", "lavfi", "-i", f"sine=f=330:d={seconds}", "-c:v", "libx264", "-pix_fmt", "yuv420p",
                    "-c:a", "aac", "-shortest", str(path)], check=True)
    return path


def fake_json3(duration: int) -> dict:
    events, t, i = [], 0.0, 0
    while t < duration - 1:
        segs = []
        for k in range(8):
            word = f"kata{i}" + ("." if k == 7 else "")
            segs.append({"utf8": (" " if k else "") + word, **({"tOffsetMs": int(k * 450)} if k else {})})
            i += 1
        events.append({"tStartMs": int(t * 1000), "dDurationMs": 3600, "segs": segs})
        t += 4.0
    return {"events": events}


class FakeYtdlp:
    def __init__(self, tmp: Path, duration=120, heatmap=True, subs=True):
        self.tmp, self.duration, self.subs = tmp, duration, subs
        self.heat = [{"start_time": i * duration / 100, "end_time": (i + 1) * duration / 100,
                      "value": 1.0 if 48 <= i <= 56 else 0.2 + 0.2 * (i % 3) / 3} for i in range(100)] if heatmap else []
        self.calls = []
        self.channel, self.channel_url = "Chan", "https://www.youtube.com/@chan"
        self.info_error = None          # exception yang dilempar fetch_info (uji kegagalan kredit sumber)

    def fetch_info(self, url, vid, hooks=None, ttl=0):
        self.calls.append("info")
        if self.info_error:
            raise self.info_error
        return {"id": vid, "title": "Video Uji", "duration": float(self.duration), "channel": self.channel,
                "channel_url": self.channel_url, "thumbnail": "",
                "language": "id", "description": "", "heatmap": self.heat, "chapters": [], "is_live": False, "url": url}

    def fetch_subtitles(self, url, vid, langs, out_dir, hooks=None):
        self.calls.append("subs")
        if not self.subs:
            return None
        out_dir.mkdir(parents=True, exist_ok=True)
        p = out_dir / f"{vid}.id.json3"
        p.write_text(json.dumps(fake_json3(self.duration)), encoding="utf-8")
        return p

    def download_section(self, url, vid, start, end, max_height, hooks=None, on_percent=None):
        self.calls.append("section")
        if on_percent:
            on_percent(1.0)
        return make_video(self.tmp / f"slice_{int(start)}.mp4", end - start)

    def version(self):
        return "test-2026.01.01"

    def download_audio(self, *a, **k):
        raise AssertionError("tidak boleh dipanggil")


class DummyCtx:
    job_id = "j"

    def __init__(self):
        self.events = []
        self.cancelled = False

    def progress(self, pct, stage=None, message=None):
        self.events.append((round(pct, 1), stage))

    def is_cancelled(self):
        return self.cancelled

    def register(self, proc):
        pass

    @property
    def hooks(self):
        return {"register": self.register, "is_cancelled": self.is_cancelled}


def make_services(tmp: Path, ytdlp=None, **env):
    s = load_settings({"DATA_DIR": str(tmp / "data"), **env})
    s.ensure_dirs()
    store = Store(s.db_path)
    return Services(s, store, ytdlp or FakeYtdlp(tmp)), s, store

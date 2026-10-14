"""Server uji E2E: adapter HTTP (stdlib) -> handler backend ASLI lewat stub FastAPI.
Asli: JobManager, pipeline analisis/render, ffmpeg, SSE. Palsu: yt-dlp (FakeYtdlp) dan jaringan Gemini."""
from __future__ import annotations

import inspect
import json
import mimetypes
import re
import subprocess
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest import mock
from urllib.parse import parse_qs, unquote, urlparse

from tests import fakeweb

fakeweb.install()
from backend import main                                            # noqa: E402
from backend.config import load_settings                            # noqa: E402
from backend.security import InputError                             # noqa: E402
from tests.helpers import FakeYtdlp                                 # noqa: E402

mimetypes.add_type("text/javascript", ".js")


def make_source(path: Path, seconds: int = 12) -> Path:
    """Latar tenang bergradasi + dua 'orang' kotak: cukup untuk menilai tata letak secara visual."""
    vf = ("drawbox=x=380:y=300:w=190:h=230:color=0xe3b48f:t=fill,drawbox=x=300:y=540:w=350:h=540:color=0x2f5d8a:t=fill,"
          "drawbox=x=1250:y=330:w=190:h=230:color=0xc99772:t=fill,drawbox=x=1170:y=570:w=350:h=510:color=0x8a3f4a:t=fill,"
          "drawbox=x=0:y=900:w=1920:h=180:color=0x101820@0.55:t=fill")
    subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-f", "lavfi", "-i",
                    f"gradients=s=1920x1080:c0=0x23323f:c1=0x7a8f9f:x0=0:y0=0:x1=1920:y1=1080:d={seconds}:r=30",
                    "-f", "lavfi", "-i", f"sine=f=220:d={seconds}", "-vf", vf, "-c:v", "libx264", "-pix_fmt", "yuv420p",
                    "-c:a", "aac", "-shortest", str(path)], check=True)
    return path


class DemoYtdlp(FakeYtdlp):
    def __init__(self, tmp: Path, duration: int = 1260):
        super().__init__(tmp, duration=duration)
        import math
        bins = 100
        self.heat = [{"start_time": i * duration / bins, "end_time": (i + 1) * duration / bins,
                      "value": round(min(1.0, 0.12 + 0.55 * math.exp(-((i - 22) / 4) ** 2) + 0.9 * math.exp(-((i - 51) / 3.2) ** 2)
                                         + 0.7 * math.exp(-((i - 78) / 5) ** 2) + 0.06 * ((i * 7) % 5) / 5), 4)} for i in range(bins)]
        self._src = make_source(tmp / "demo_src.mp4")

    def download_section(self, url, vid, start, end, max_height, hooks=None, on_percent=None):
        if on_percent:
            on_percent(1.0)
        out = self.tmp / f"sec_{int(start)}_{int(end)}.mp4"
        if not out.exists():
            subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-stream_loop", "-1", "-i", str(self._src), "-t",
                            str(end - start), "-c", "copy", str(out)], check=True)
        return out

    def stream_url(self, url, vid):
        if not hasattr(self, "_long"):               # sumber panjang dari satu gambar: -ss ke menit ke-10 harus berhasil
            still = self.tmp / "still.png"
            subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-ss", "2", "-i", str(self._src), "-frames:v", "1", str(still)], check=True)
            self._long = self.tmp / "long.mp4"
            subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-loop", "1", "-framerate", "1", "-i", str(still), "-t", str(self.duration),
                            "-c:v", "libx264", "-preset", "ultrafast", "-tune", "stillimage", "-g", "30", "-pix_fmt", "yuv420p", str(self._long)], check=True)
        return str(self._long)


DEMO_CANDIDATES = [
    {"start": 640, "end": 676, "score": 94, "title": "Kesalahan terbesar saat memulai bisnis", "hook": "Hampir semua orang salah di langkah pertama ini",
     "reason": "Pembukaan kuat, lengkap dengan contoh nyata, dan berakhir di punchline.", "hashtags": ["bisnis", "tips", "pemula"]},
    {"start": 300, "end": 330, "score": 86, "title": "Cerita gagal yang bikin ngakak", "hook": "Modal habis dalam tiga hari",
     "reason": "Cerita pribadi dengan emosi dan humor di akhir.", "hashtags": ["storytime", "lucu"]},
    {"start": 960, "end": 1000, "score": 78, "title": "Cara menetapkan harga tanpa takut rugi", "hook": "Jangan pernah mulai dari harga pesaing",
     "reason": "Saran praktis yang bisa langsung dipakai.", "hashtags": ["harga", "strategi"]},
    {"start": 120, "end": 148, "score": 63, "title": "Kenapa kami hampir berhenti", "hook": "Kami sudah siap menutup toko",
     "reason": "Konflik jelas, tetapi resolusinya kurang tajam.", "hashtags": ["motivasi"]},
]


class Backend:
    def __init__(self):
        self.tmpd = tempfile.TemporaryDirectory()
        tmp = Path(self.tmpd.name)
        self.settings = load_settings({"DATA_DIR": str(tmp / "data"), "HOST": "127.0.0.1", "CACHE_TTL_HOURS": "1"})
        self.fy = DemoYtdlp(tmp)
        self.patches = [
            mock.patch("backend.pipeline.gemini.list_models", return_value=["gemini-2.5-flash"]),
            mock.patch("backend.pipeline.gemini.find_candidates",
                       side_effect=lambda *a, **k: (list(DEMO_CANDIDATES), ["gemini-2.5-flash"], ["gemini-3-flash: quota", "gemini-2.5-flash: ok"])),
        ]
        for p in self.patches:
            p.start()
        self.app = main.create_app(self.settings, ytdlp=self.fy)
        self.routes = []
        for (method, path), fn in self.app.routes.items():
            names = re.findall(r"\{(\w+)\}", path)
            rx = re.compile("^" + re.sub(r"\{\w+\}", r"([^/]+)", path) + "$")
            self.routes.append((method, rx, names, fn))

    def close(self):
        for p in self.patches:
            p.stop()
        self.app.state.jobs.shutdown()
        self.tmpd.cleanup()

    def dispatch(self, method, path, query, headers, body):
        for m, rx, names, fn in self.routes:
            mt = rx.match(path)
            if m == method and mt:
                return self.invoke(fn, dict(zip(names, map(unquote, mt.groups()))), query, headers, body, method)
        return None

    def invoke(self, fn, path_params, query, headers, body, method):
        kwargs = {}
        for name, p in inspect.signature(fn).parameters.items():
            ann = str(p.annotation).strip("'\"")
            if name in path_params:
                kwargs[name] = path_params[name]
            elif name == "request":
                kwargs[name] = main.Request(headers=headers, cookies={}, method=method)
            elif name == "response":
                kwargs[name] = main.Response()
            elif name == "x_gemini_key":
                kwargs[name] = headers.get("x-gemini-key")
            elif ann.endswith("Req"):
                data = dict(body or {})
                if ann == "RenderReq":
                    data["clips"] = [main.ClipIn(**c) for c in data.get("clips", [])]
                kwargs[name] = getattr(main, ann)(**data)
            elif name in query:
                kwargs[name] = float(query[name][0]) if "float" in ann else query[name][0]
        return fn(**kwargs)


class Handler(BaseHTTPRequestHandler):
    backend: Backend
    frontend: Path
    protocol_version = "HTTP/1.1"

    def log_message(self, *a):
        pass

    def _send(self, code, body: bytes, ctype="application/json", extra=None):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def _handle(self, method):
        u = urlparse(self.path)
        n = int(self.headers.get("Content-Length") or 0)
        body = json.loads(self.rfile.read(n) or b"null") if n else None
        headers = {k.lower(): v for k, v in self.headers.items()}
        if u.path.startswith("/api/"):
            try:
                out = self.backend.dispatch(method, u.path, parse_qs(u.query), headers, body)
            except (InputError, main.HTTPException) as e:
                code = 400 if isinstance(e, InputError) else e.status_code
                return self._send(code, json.dumps({"detail": str(e)}).encode())
            except Exception as e:                                   # noqa: BLE001
                return self._send(500, json.dumps({"detail": f"{type(e).__name__}: {e}"}).encode())
            if out is None:
                return self._send(404, b'{"detail":"not found"}')
            if isinstance(out, main.StreamingResponse):
                self.send_response(200)
                self.send_header("Content-Type", out.media_type)
                self.send_header("Cache-Control", "no-cache")
                self.send_header("Transfer-Encoding", "chunked")
                self.end_headers()
                for chunk in out.iterator:
                    data = chunk.encode()
                    self.wfile.write(f"{len(data):x}\r\n".encode() + data + b"\r\n")
                    self.wfile.flush()
                self.wfile.write(b"0\r\n\r\n")
                return
            if isinstance(out, main.FileResponse):
                extra = dict(out.headers)
                if out.filename:
                    extra["Content-Disposition"] = f'attachment; filename="{out.filename}"'
                return self._send(200, Path(out.path).read_bytes(), out.media_type or "application/octet-stream", extra)
            if isinstance(out, main.Response):
                b = out.body.encode() if isinstance(out.body, str) else (out.body or b"")
                return self._send(out.status_code, b, out.media_type or "text/plain", out.headers)
            return self._send(200, json.dumps(out).encode())
        rel = "index.html" if u.path in ("", "/") else u.path.lstrip("/")
        f = (self.frontend / rel).resolve()
        if self.frontend.resolve() not in f.parents and f != self.frontend.resolve() or not f.is_file():
            return self._send(404, b"not found", "text/plain")
        self._send(200, f.read_bytes(), mimetypes.guess_type(str(f))[0] or "application/octet-stream")

    def do_GET(self):
        self._handle("GET")

    def do_POST(self):
        self._handle("POST")

    def do_DELETE(self):
        self._handle("DELETE")


def serve(port: int = 0):
    backend = Backend()
    Handler.backend, Handler.frontend = backend, backend.settings.frontend_dir
    srv = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    srv.daemon_threads = True
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, backend

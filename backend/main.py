"""Cheat Clip Pro Max — lapisan HTTP (FastAPI). Logika bisnis ada di pipeline.py."""
from __future__ import annotations

import csv
import io
import os
import tempfile
import zipfile
from functools import lru_cache
from pathlib import Path
import mimetypes
from typing import Literal
from urllib.parse import urlparse

from fastapi import APIRouter, Depends, FastAPI, Header, HTTPException, Query, Request, Response
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from starlette.background import BackgroundTask

from . import gemini, hwenc
from .captions import FONTS, PRESETS
from .config import VERSION, Settings, load_settings
from .db import Store, new_id
from .facetrack import detect_face_in_image
from .jobs import JobManager, public
from .layouts import ASPECTS, CORNERS, MODES
from .pipeline import (Services, clear_temp, options_from_style, purge_old, run_analysis, run_render, storage_info)
from .procutil import CommandError
from .security import (InputError, parse_video_url, redact, safe_child, token_ok, valid_id,
                       validate_netscape_cookies)
from .ytdlp import YtDlp

# Windows kadang memetakan .js ke text/plain lewat registry -> browser menolak <script type="module">.
mimetypes.add_type("text/javascript", ".js")
mimetypes.add_type("text/css", ".css")

SSE_HEADERS = {"Cache-Control": "no-cache", "X-Accel-Buffering": "no", "Connection": "keep-alive"}
LOOPBACK = {"127.0.0.1", "localhost", "::1"}
MEDIA = {".mp4": "video/mp4", ".jpg": "image/jpeg", ".srt": "text/plain; charset=utf-8"}


def hostname_of(host_header: str) -> str:
    """'127.0.0.1:8000' -> '127.0.0.1', '[::1]:8000' -> '::1'."""
    h = host_header.strip().lower()
    if h.startswith("["):
        return h[1:].split("]")[0]
    return h.rsplit(":", 1)[0] if ":" in h else h


# --------------------------------------------------------------------------- skema request
class AnalyzeReq(BaseModel):
    url: str = Field(max_length=500)
    preset: Literal["short", "standard", "extended"] = "standard"
    min_len: float | None = Field(None, ge=3, le=600)
    max_len: float | None = Field(None, ge=3, le=600)
    max_clips: int = Field(8, ge=1, le=20)
    focus: str = Field("", max_length=300)
    language: Literal["auto", "id", "en"] = "auto"
    use_cache: bool = True


class ClipIn(BaseModel):
    id: str = Field(max_length=32)
    start: float = Field(ge=0)
    end: float = Field(gt=0)
    title: str = Field("", max_length=120)
    hook: str = Field("", max_length=160)
    reason: str = Field("", max_length=300)
    overlay_title: str = Field("", max_length=120)
    hashtags: list[str] = Field(default_factory=list, max_length=8)


class RenderReq(BaseModel):
    url: str = Field(max_length=500)
    clips: list[ClipIn] = Field(min_length=1, max_length=30)
    style: dict = Field(default_factory=dict)
    language: Literal["auto", "id", "en"] = "auto"


class RawReq(BaseModel):
    url: str = Field(max_length=500)
    max_height: Literal[360, 480, 720, 1080] = 1080


class CookiesReq(BaseModel):
    text: str = Field(max_length=600_000)


class LoginReq(BaseModel):
    token: str = Field(max_length=200)


# --------------------------------------------------------------------------- app
def create_app(settings: Settings | None = None, ytdlp: YtDlp | None = None) -> FastAPI:
    settings = settings or load_settings()
    settings.ensure_dirs()
    store = Store(settings.db_path)
    jobs = JobManager(store, settings.render_workers)
    ytdlp = ytdlp or YtDlp(settings)
    svc = Services(settings, store, ytdlp)
    purge_old(settings, store)

    app = FastAPI(title="Cheat Clip Pro Max", version=VERSION)
    app.state.settings, app.state.jobs, app.state.store = settings, jobs, store

    @app.exception_handler(InputError)
    async def _input_error(_: Request, exc: InputError) -> JSONResponse:
        return JSONResponse({"detail": str(exc)}, status_code=400)

    @app.middleware("http")
    async def guard(request: Request, call_next):
        """Tolak DNS-rebinding (Host aneh) dan CSRF lintas-situs (Origin beda) ke API lokal."""
        host_header = request.headers.get("host", "")
        if settings.host in LOOPBACK and hostname_of(host_header) not in LOOPBACK:
            return JSONResponse({"detail": "Host tidak diizinkan"}, status_code=403)
        if request.method not in ("GET", "HEAD", "OPTIONS"):
            origin = request.headers.get("origin")
            if origin and urlparse(origin).netloc != host_header:
                return JSONResponse({"detail": "Origin tidak diizinkan"}, status_code=403)
        return await call_next(request)

    def require_auth(request: Request) -> None:
        if not settings.app_token:
            return
        tok = request.cookies.get("ccpm_token") or request.headers.get("x-app-token")
        if not token_ok(settings.app_token, tok):
            raise HTTPException(status_code=401, detail="Butuh token akses.")

    pub = APIRouter(prefix="/api")
    api = APIRouter(prefix="/api", dependencies=[Depends(require_auth)])

    # ------------------------------------------------------------------ publik
    @lru_cache(maxsize=1)
    def capabilities() -> dict:
        try:
            import cv2  # noqa: F401
            has_cv = True
        except ImportError:
            has_cv = False
        from .transcript import whisper_available
        gpu = hwenc.detect_gpu_encoder(settings.ffmpeg_bin)
        import shutil
        fonts = [f for f in settings.fonts_dir.glob("*") if f.suffix.lower() in (".ttf", ".otf")]
        return {"ffmpeg": bool(shutil.which(settings.ffmpeg_bin)), "ffprobe": bool(shutil.which(settings.ffprobe_bin)),
                "ytdlp": ytdlp.version(), "gpu_encoder": gpu.label if gpu else None, "opencv": has_cv,
                "whisper": whisper_available(), "custom_fonts": len(fonts)}

    @pub.get("/health")
    def health() -> dict:
        return {"ok": True, "version": VERSION, "auth_required": bool(settings.app_token), **capabilities()}

    @pub.get("/auth-status")
    def auth_status(request: Request) -> dict:
        try:
            require_auth(request)
            authed = True
        except HTTPException:
            authed = False
        return {"required": bool(settings.app_token), "authed": authed}

    @pub.post("/login")
    def login(req: LoginReq, response: Response) -> dict:
        if not token_ok(settings.app_token, req.token):
            raise HTTPException(status_code=401, detail="Token salah.")
        response.set_cookie("ccpm_token", req.token, httponly=True, samesite="strict", max_age=30 * 86400)
        return {"ok": True}

    @pub.post("/logout")
    def logout(response: Response) -> dict:
        response.delete_cookie("ccpm_token")
        return {"ok": True}

    # ------------------------------------------------------------------ meta UI
    @api.get("/presets")
    def presets() -> dict:
        return {
            "captions": {k: {"label": v.label, "inactive": v.inactive, "active": v.active, "outline": v.outline,
                             "uppercase": v.uppercase} for k, v in PRESETS.items()},
            "fonts": list(FONTS), "layouts": list(MODES), "aspects": list(ASPECTS) + ["auto"],
            "corners": list(CORNERS), "max_clip_seconds": settings.max_clip_seconds,
        }

    @api.get("/models")
    def models(x_gemini_key: str | None = Header(default=None)) -> dict:
        key = (x_gemini_key or "").strip()
        if not key or key == "mock":
            raise InputError("Kirim API key lewat header X-Gemini-Key.")
        try:
            return {"models": gemini.list_models(key, settings.gemini_models)}
        except gemini.GeminiError as e:
            raise InputError(str(e)) from None

    # ------------------------------------------------------------------ analisis
    @api.post("/analyze")
    def analyze(req: AnalyzeReq, x_gemini_key: str | None = Header(default=None)) -> dict:
        url, vid = parse_video_url(req.url, settings.allowed_hosts)
        if req.min_len and req.max_len and req.min_len >= req.max_len:
            raise InputError("Durasi minimum harus lebih kecil dari maksimum.")
        key = (x_gemini_key or "").strip()      # hanya hidup di memori closure; tidak masuk DB / log
        params = req.model_dump() | {"url": url, "video_id": vid}
        jid = jobs.submit("analyze", params, lambda ctx, p: run_analysis(svc, ctx, p, key), pool="analyze")
        return {"job_id": jid}

    @api.get("/jobs/{job_id}")
    def job_status(job_id: str) -> dict:
        job = public(store.get_job(valid_id(job_id)))
        if job is None:
            raise HTTPException(status_code=404, detail="Job tidak ditemukan.")
        return job

    @api.get("/jobs/{job_id}/events")
    def job_events(job_id: str) -> StreamingResponse:
        valid_id(job_id)
        return StreamingResponse(jobs.events(job_id), media_type="text/event-stream", headers=SSE_HEADERS)

    @api.post("/jobs/{job_id}/cancel")
    def job_cancel(job_id: str) -> dict:
        return {"cancelled": jobs.cancel(valid_id(job_id))}

    @api.post("/jobs/{job_id}/retry")
    def job_retry(job_id: str) -> dict:
        job = store.get_job(valid_id(job_id))
        if not job or job["kind"] != "render":
            raise InputError("Hanya job render yang bisa diulang.")
        if job["status"] not in ("failed", "cancelled", "interrupted"):
            raise InputError("Job ini belum selesai atau sudah berhasil.")
        if not jobs.resubmit(job_id, lambda ctx, q: run_render(svc, ctx, q), pool="render"):
            raise InputError("Job tidak bisa diulang.")
        return {"job_id": job_id}

    # ------------------------------------------------------------------ preview
    @api.get("/clip-frame")
    def clip_frame(video_id: str = Query(max_length=64), timestamp: float = Query(0, ge=0, le=86400 * 2)):
        vid = valid_id(video_id)
        if len(vid) != 11:
            raise InputError("Preview frame hanya untuk video YouTube.")
        out = settings.cache_dir / "frames" / f"{vid}_{int(timestamp * 2)}.jpg"
        if not out.is_file():
            import subprocess
            url = f"https://www.youtube.com/watch?v={vid}"
            try:
                stream = ytdlp.stream_url(url, vid)
                r = subprocess.run([settings.ffmpeg_bin, "-y", "-loglevel", "error", "-ss", f"{timestamp:.2f}",
                                    "-i", stream, "-frames:v", "1", "-vf", "scale=720:-2", "-q:v", "3", str(out)],
                                   capture_output=True, timeout=90)
            except (CommandError, subprocess.SubprocessError) as e:
                raise HTTPException(status_code=502, detail=str(e)[:200]) from None
            if r.returncode != 0 or not out.is_file():
                raise HTTPException(status_code=502, detail="Gagal mengambil frame.")
        headers = {"Cache-Control": "max-age=600"}
        cx = detect_face_in_image(str(out))
        if cx is not None:
            headers["X-Face-Center"] = f"{cx:.4f}"
        return FileResponse(out, media_type="image/jpeg", headers=headers)

    # ------------------------------------------------------------------ render
    @api.post("/render-batch")
    def render_batch(req: RenderReq) -> dict:
        url, vid = parse_video_url(req.url, settings.allowed_hosts)
        for c in req.clips:
            if c.end <= c.start or c.end - c.start > settings.max_clip_seconds or c.end - c.start < 3:
                raise InputError(f"Klip {c.id}: durasi harus 3–{settings.max_clip_seconds} detik.")
            options_from_style(req.style, c.overlay_title)     # validasi dini -> 400, bukan gagal di tengah
        batch_id = new_id()
        ids = []
        for i, c in enumerate(req.clips, 1):
            params = {"url": url, "video_id": vid, "batch_id": batch_id, "index": i, "clip": c.model_dump(),
                      "style": req.style, "language": req.language}
            ids.append(jobs.submit("render", params, lambda ctx, p: run_render(svc, ctx, p), pool="render",
                                   batch_id=batch_id))
        return {"batch_id": batch_id, "job_ids": ids}

    def batch_snapshot(batch_id: str) -> dict:
        rows = [public(j) for j in store.batch_jobs(valid_id(batch_id))]
        if not rows:
            raise HTTPException(status_code=404, detail="Batch tidak ditemukan.")
        fin = ("done", "failed", "cancelled", "interrupted")
        return {"batch_id": batch_id, "jobs": rows, "total": len(rows),
                "done": sum(1 for r in rows if r["status"] == "done"),
                "finished": all(r["status"] in fin for r in rows)}

    @api.get("/render-progress/{batch_id}")
    def render_progress(batch_id: str) -> dict:
        return batch_snapshot(batch_id)

    @api.get("/batches/{batch_id}/events")
    def batch_events(batch_id: str) -> StreamingResponse:
        valid_id(batch_id)
        return StreamingResponse(jobs.batch_events(batch_id), media_type="text/event-stream", headers=SSE_HEADERS)

    @api.post("/batches/{batch_id}/cancel")
    def batch_cancel(batch_id: str) -> dict:
        return {"cancelled": jobs.cancel_batch(valid_id(batch_id))}

    @api.get("/download-rendered/{file_name}")
    def download_rendered(file_name: str):
        path = safe_child(settings.exports_dir, file_name)
        if not path.is_file() or path.suffix not in MEDIA:
            raise HTTPException(status_code=404, detail="File tidak ditemukan.")
        return FileResponse(path, media_type=MEDIA[path.suffix], filename=path.name)

    def finished_results(batch_id: str) -> list[dict]:
        return [j["result"] for j in store.batch_jobs(valid_id(batch_id)) if j["status"] == "done" and j["result"]]

    def pack_rows(results: list[dict]) -> str:
        buf = io.StringIO()
        w = csv.writer(buf)
        w.writerow(["file", "title", "hook", "hashtags", "start_s", "end_s", "duration_s", "reason"])
        for r in results:
            w.writerow([r.get("file"), r.get("title"), r.get("hook"), " ".join(f"#{t}" for t in r.get("hashtags", [])),
                        r.get("start"), r.get("end"), r.get("duration"), r.get("reason")])
        return buf.getvalue()

    @api.get("/publish-pack/{batch_id}")
    def publish_pack(batch_id: str) -> Response:
        results = finished_results(batch_id)
        if not results:
            raise HTTPException(status_code=404, detail="Belum ada klip yang selesai.")
        body = "\ufeff" + pack_rows(results)       # BOM agar Excel membaca UTF-8
        return Response(body, media_type="text/csv; charset=utf-8",
                        headers={"Content-Disposition": f'attachment; filename="publish-pack-{batch_id}.csv"'})

    @api.get("/download-batch-zip/{batch_id}")
    def download_zip(batch_id: str):
        results = finished_results(batch_id)
        if not results:
            raise HTTPException(status_code=404, detail="Belum ada klip yang selesai.")
        fd, tmp = tempfile.mkstemp(suffix=".zip", dir=settings.work_dir)
        os.close(fd)
        with zipfile.ZipFile(tmp, "w", zipfile.ZIP_STORED) as z:
            for r in results:
                p = safe_child(settings.exports_dir, r["file"])
                if p.is_file():
                    z.write(p, arcname=p.name)
                srt = p.with_suffix(".srt")
                if srt.is_file():
                    z.write(srt, arcname=srt.name)
            z.writestr("publish-pack.csv", "\ufeff" + pack_rows(results))
        return FileResponse(tmp, media_type="application/zip", filename=f"cheat-clip-{batch_id}.zip",
                            background=BackgroundTask(lambda: Path(tmp).unlink(missing_ok=True)))

    # ------------------------------------------------------------------ cookies
    @api.get("/cookies")
    def cookies_status() -> dict:
        f = settings.cookies_file
        if not f.is_file():
            return {"present": False}
        try:
            info = validate_netscape_cookies(f.read_text(encoding="utf-8", errors="ignore"))
        except InputError:
            return {"present": True, "valid": False}
        return {"present": True, "valid": True, **info, "modified": f.stat().st_mtime}

    @api.post("/cookies")
    def cookies_save(req: CookiesReq) -> dict:
        info = validate_netscape_cookies(req.text)
        tmp = settings.cookies_file.with_suffix(".tmp")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(req.text.replace("\r\n", "\n"))
        os.replace(tmp, settings.cookies_file)
        return {"saved": True, **info}

    @api.delete("/cookies")
    def cookies_delete() -> dict:
        settings.cookies_file.unlink(missing_ok=True)
        return {"deleted": True}

    # ------------------------------------------------------------------ unduh video mentah
    def raw_handler(ctx, p: dict) -> dict:
        def on_prog(d: dict) -> None:
            pct = (d["percent"] or 0) * 100
            msg = f"{(d['downloaded'] or 0) / 1e6:.1f} MB"
            if d.get("total"):
                msg += f" / {d['total'] / 1e6:.1f} MB"
            if d.get("speed"):
                msg += f" • {d['speed'] / 1e6:.1f} MB/s"
            if d.get("eta") is not None:
                msg += f" • sisa {int(d['eta'])} dtk"
            ctx.progress(pct, "download", msg)

        path = ytdlp.download_raw(p["url"], p["video_id"], settings.exports_dir, p["max_height"], ctx.hooks, on_prog)
        return {"file": path.name, "size_bytes": path.stat().st_size}

    @api.post("/download-raw-video")
    def download_raw(req: RawReq) -> dict:
        url, vid = parse_video_url(req.url, settings.allowed_hosts)
        jid = jobs.submit("raw", {"url": url, "video_id": vid, "max_height": req.max_height}, raw_handler,
                          pool="download")
        return {"job_id": jid}

    @api.get("/download-raw-status/{job_id}")
    def raw_status(job_id: str) -> dict:
        return job_status(job_id)

    # ------------------------------------------------------------------ penyimpanan & sistem
    @api.get("/temp-storage-info")
    def temp_info() -> dict:
        return storage_info(settings)

    @api.post("/clear-temp")
    def clear() -> dict:
        return clear_temp(settings, busy=store.count_active() > 0)

    @api.post("/system/update-ytdlp")
    def update_ytdlp() -> dict:
        try:
            return {"version": ytdlp.self_update()}
        except CommandError as e:
            raise InputError(redact(str(e))) from None

    app.include_router(pub)
    app.include_router(api)
    if settings.frontend_dir.is_dir():
        app.mount("/", StaticFiles(directory=str(settings.frontend_dir), html=True), name="ui")
    return app

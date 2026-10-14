"""Job manager: antrean berbasis thread pool, progress real-time, pembatalan, dan aliran SSE."""
from __future__ import annotations

import atexit
import json
import subprocess
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Callable, Iterator

from .db import TERMINAL, Store
from .procutil import Cancelled, kill_tree
from .render import RenderCancelled

Handler = Callable[["JobContext", dict], dict]

PUBLIC_FIELDS = ("id", "kind", "batch_id", "status", "stage", "progress", "message", "result", "error",
                 "created", "updated")


def public(job: dict | None) -> dict | None:
    return None if job is None else {k: job.get(k) for k in PUBLIC_FIELDS}


class JobContext:
    def __init__(self, mgr: "JobManager", job_id: str):
        self._mgr, self.job_id = mgr, job_id
        self._last = 0.0
        self._stage = ""

    def progress(self, pct: float, stage: str | None = None, message: str | None = None) -> None:
        now = time.monotonic()
        stage_changed = stage is not None and stage != self._stage
        if now - self._last < 0.25 and pct < 99.5 and not stage_changed:
            return
        self._last = now
        if stage is not None:
            self._stage = stage
        fields: dict[str, Any] = {"progress": round(max(0.0, min(100.0, pct)), 1)}
        if stage is not None:
            fields["stage"] = stage
        if message is not None:
            fields["message"] = message
        self._mgr.store.update_job(self.job_id, **fields)

    def is_cancelled(self) -> bool:
        return self._mgr.is_cancelled(self.job_id)

    def register(self, proc: subprocess.Popen) -> None:
        self._mgr.register_proc(self.job_id, proc)

    @property
    def hooks(self) -> dict:
        return {"register": self.register, "is_cancelled": self.is_cancelled}


class JobManager:
    def __init__(self, store: Store, render_workers: int = 2):
        self.store = store
        self._pools = {
            "analyze": ThreadPoolExecutor(2, thread_name_prefix="analyze"),
            "render": ThreadPoolExecutor(render_workers, thread_name_prefix="render"),
            "download": ThreadPoolExecutor(1, thread_name_prefix="download"),
        }
        self._cancel: set[str] = set()
        self._procs: dict[str, list[subprocess.Popen]] = {}
        self._lock = threading.Lock()
        # Proses anak berada di grup sendiri (lihat procutil.spawn) sehingga Ctrl+C tidak sampai ke mereka:
        # server wajib mematikannya sendiri saat keluar, kalau tidak ffmpeg bisa jadi yatim.
        atexit.register(self.kill_all)

    def kill_all(self) -> None:
        with self._lock:
            procs = [p for ps in self._procs.values() for p in ps]
        for p in procs:
            if p.poll() is None:
                kill_tree(p, 0.5)

    # ------------------------------------------------------------------ kontrol
    def is_cancelled(self, jid: str) -> bool:
        with self._lock:
            return jid in self._cancel

    def register_proc(self, jid: str, proc: subprocess.Popen) -> None:
        with self._lock:
            self._procs.setdefault(jid, []).append(proc)
            kill = jid in self._cancel
        if kill:
            kill_tree(proc, 0.5)

    def cancel(self, jid: str) -> bool:
        job = self.store.get_job(jid)
        if not job or job["status"] in TERMINAL:
            return False
        with self._lock:
            self._cancel.add(jid)
            procs = list(self._procs.get(jid, []))
        for p in procs:
            kill_tree(p)               # termasuk anak-cucu (yt-dlp -> ffmpeg)
        if job["status"] == "queued":
            self.store.update_job(jid, status="cancelled", message="Dibatalkan")
        return True

    def cancel_batch(self, batch_id: str) -> int:
        return sum(1 for j in self.store.batch_jobs(batch_id) if self.cancel(j["id"]))

    # ------------------------------------------------------------------ eksekusi
    def submit(self, kind: str, params: dict, handler: Handler, *, pool: str, batch_id: str | None = None) -> str:
        jid = self.store.create_job(kind, params, batch_id)
        self._pools[pool].submit(self._run, jid, handler, params)
        return jid

    def resubmit(self, jid: str, handler: Handler, *, pool: str) -> bool:
        """Jalankan ulang job yang gagal/dibatalkan dengan ID yang sama (UI tidak menggandakan barisnya)."""
        job = self.store.get_job(jid)
        if not job or job["status"] not in ("failed", "cancelled", "interrupted"):
            return False
        with self._lock:
            self._cancel.discard(jid)
            self._procs.pop(jid, None)
        self.store.update_job(jid, status="queued", stage="", progress=0.0, message="Mengantre", error=None, result=None)
        self._pools[pool].submit(self._run, jid, handler, job["params"])
        return True

    def _run(self, jid: str, handler: Handler, params: dict) -> None:
        job = self.store.get_job(jid)
        if not job or job["status"] != "queued" or self.is_cancelled(jid):
            with self._lock:                      # dibatalkan saat antre: bersihkan flag agar ID bisa dipakai ulang
                self._cancel.discard(jid)
            return
        self.store.update_job(jid, status="running", stage="start", progress=0.0, message="Memulai")
        ctx = JobContext(self, jid)
        try:
            result = handler(ctx, params)
            if self.is_cancelled(jid):
                raise Cancelled()
            self.store.update_job(jid, status="done", progress=100.0, stage="done", message="Selesai",
                                  result=result or {})
        except (Cancelled, RenderCancelled):
            self.store.update_job(jid, status="cancelled", message="Dibatalkan")
        except Exception as e:  # noqa: BLE001 - semua kegagalan dilaporkan ke klien dengan pesan ringkas
            msg = str(e).strip() or e.__class__.__name__
            self.store.update_job(jid, status="failed", error=msg[:900], message="Gagal")
        finally:
            with self._lock:
                self._procs.pop(jid, None)
                self._cancel.discard(jid)

    # ------------------------------------------------------------------ SSE
    def events(self, jid: str, poll: float = 0.5) -> Iterator[str]:
        last, beat = None, time.monotonic()
        while True:
            job = public(self.store.get_job(jid))
            if job is None:
                yield "event: gone\ndata: {}\n\n"
                return
            snap = json.dumps(job)
            if snap != last:
                yield f"data: {snap}\n\n"
                last, beat = snap, time.monotonic()
            elif time.monotonic() - beat > 15:
                yield ": ping\n\n"
                beat = time.monotonic()
            if job["status"] in TERMINAL:
                return
            time.sleep(poll)

    def batch_events(self, batch_id: str, poll: float = 0.6) -> Iterator[str]:
        last, beat = None, time.monotonic()
        while True:
            jobs = [public(j) for j in self.store.batch_jobs(batch_id)]
            snap = json.dumps(jobs)
            if snap != last:
                yield f"data: {snap}\n\n"
                last, beat = snap, time.monotonic()
            elif time.monotonic() - beat > 15:
                yield ": ping\n\n"
                beat = time.monotonic()
            if not jobs or all(j["status"] in TERMINAL for j in jobs):  # type: ignore[index]
                return
            time.sleep(poll)

    def shutdown(self) -> None:
        self.kill_all()
        for p in self._pools.values():
            p.shutdown(wait=False, cancel_futures=True)

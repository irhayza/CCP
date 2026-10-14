"""Jalankan subprocess dengan streaming baris, pembatalan, dan timeout. Tidak pernah memakai shell."""
from __future__ import annotations

import os
import signal
import subprocess
import threading
from typing import Callable

IS_WINDOWS = os.name == "nt"


def spawn(cmd: list[str], **kw) -> subprocess.Popen:
    """Popen di grup proses sendiri, supaya seluruh pohon proses (yt-dlp -> ffmpeg) bisa dimatikan sekaligus."""
    if IS_WINDOWS:
        kw.setdefault("creationflags", subprocess.CREATE_NEW_PROCESS_GROUP)
    else:
        kw.setdefault("start_new_session", True)
    return subprocess.Popen(cmd, **kw)


def _group_alive(pgid: int) -> bool:
    try:
        os.killpg(pgid, 0)
        return True
    except (ProcessLookupError, PermissionError):
        return False


def kill_tree(proc: subprocess.Popen, grace: float = 2.0) -> None:
    """Hentikan proses beserta semua anak-cucunya. Tidak memblokir: eskalasi ke SIGKILL berjalan di timer.

    POSIX   : SIGTERM ke grup proses, lalu SIGKILL bila masih ada setelah `grace` detik.
    Windows : `taskkill /T /F` (membunuh pohon proses; tidak ada tahap sopan).
    Proses yang tidak dibuat lewat spawn() (bukan pemimpin grup) hanya di-terminate biasa."""
    if IS_WINDOWS:
        try:
            subprocess.run(["taskkill", "/PID", str(proc.pid), "/T", "/F"], capture_output=True, timeout=15)
        except (OSError, subprocess.SubprocessError):
            proc.kill()
        return
    try:
        leader = os.getpgid(proc.pid) == proc.pid
    except ProcessLookupError:
        leader = True                      # induk sudah mati; grup bisa saja masih berisi anak
    if not leader:
        proc.terminate()
        return
    try:
        os.killpg(proc.pid, signal.SIGTERM)
    except (ProcessLookupError, PermissionError):
        return

    def escalate() -> None:
        if _group_alive(proc.pid):
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except (ProcessLookupError, PermissionError):
                pass

    t = threading.Timer(grace, escalate)
    t.daemon = True
    t.start()


class Cancelled(Exception):
    pass


class CommandError(RuntimeError):
    def __init__(self, message: str, returncode: int = 1):
        super().__init__(message)
        self.returncode = returncode


def run_streaming(cmd: list[str], *, on_line: Callable[[str], None] | None = None,
                  register: Callable[[subprocess.Popen], None] | None = None,
                  is_cancelled: Callable[[], bool] = lambda: False, timeout: float | None = None,
                  cwd: str | None = None) -> tuple[int, str, str]:
    """Return (returncode, stdout, stderr_tail). Melempar Cancelled bila dibatalkan."""
    proc = spawn(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                 encoding="utf-8", errors="replace", cwd=cwd)
    if register:
        register(proc)
    timer = None
    if timeout:
        timer = threading.Timer(timeout, kill_tree, args=(proc, 0.5))
        timer.daemon = True
        timer.start()
    err: list[str] = []

    def drain() -> None:
        assert proc.stderr is not None
        for line in proc.stderr:
            err.append(line)
            del err[:-80]

    t = threading.Thread(target=drain, daemon=True)
    t.start()
    out: list[str] = []
    assert proc.stdout is not None
    for line in proc.stdout:
        out.append(line)
        if on_line:
            on_line(line.rstrip("\r\n"))
    proc.wait()
    t.join(timeout=2)
    proc.stdout.close()
    if proc.stderr:
        proc.stderr.close()
    if timer:
        timer.cancel()
    if is_cancelled():
        raise Cancelled()
    return proc.returncode, "".join(out), "".join(err)

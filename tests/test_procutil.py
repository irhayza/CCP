import os, signal, subprocess, sys, tempfile, time, unittest
from pathlib import Path

from backend.db import Store
from backend.jobs import JobManager
from backend.procutil import IS_WINDOWS, Cancelled, kill_tree, run_streaming, spawn

PARENT = ("import subprocess,sys,time; "
          "c=subprocess.Popen([sys.executable,'-c','import time; time.sleep(60)']); print(c.pid, flush=True); time.sleep(60)")
# anak menulis file penanda SETELAH memasang SIG_IGN, supaya test tidak balapan dengan startup Python
STUBBORN = ("import subprocess,sys,time; "
            "c=subprocess.Popen([sys.executable,'-c','import signal,sys,time; signal.signal(signal.SIGTERM, signal.SIG_IGN); "
            "open(sys.argv[1],\"w\").write(\"1\"); time.sleep(60)', sys.argv[1]]); print(c.pid, flush=True); time.sleep(60)")


def alive(pid: int) -> bool:
    try:
        return Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()[0] != "Z"   # zombie = sudah mati
    except FileNotFoundError:
        return False


def wait_dead(pid: int, timeout: float = 5.0) -> bool:
    end = time.time() + timeout
    while time.time() < end:
        if not alive(pid):
            return True
        time.sleep(0.05)
    return False


@unittest.skipIf(IS_WINDOWS or not Path("/proc").exists(), "pengecekan proses via /proc (Linux)")
class KillTreeTests(unittest.TestCase):
    def start(self, code, *args):
        p = spawn([sys.executable, "-c", code, *args], stdout=subprocess.PIPE, text=True)
        self.addCleanup(p.stdout.close)
        self.addCleanup(lambda: p.poll() is None and p.kill())
        child = int(p.stdout.readline())
        self.addCleanup(lambda: alive(child) and os.kill(child, signal.SIGKILL))
        return p, child

    def test_child_dies_with_parent(self):
        p, child = self.start(PARENT)
        self.assertTrue(alive(child))
        kill_tree(p)
        p.wait(5)
        self.assertTrue(wait_dead(child), "proses anak harus ikut mati (regresi: dulu selamat)")

    def test_child_that_ignores_sigterm_is_force_killed(self):
        with tempfile.TemporaryDirectory() as d:
            ready = Path(d) / "ready"
            p, child = self.start(STUBBORN, str(ready))
            for _ in range(200):
                if ready.exists():
                    break
                time.sleep(0.02)
            self.assertTrue(ready.exists(), "anak belum memasang SIG_IGN")
            self._stubborn(p, child)

    def _stubborn(self, p, child):
        kill_tree(p, grace=0.3)
        p.wait(5)
        self.assertTrue(alive(child))                 # SIGTERM diabaikan -> belum mati sesaat setelahnya
        self.assertTrue(wait_dead(child, 5), "eskalasi SIGKILL harus mematikannya")

    def test_non_leader_falls_back_to_terminate(self):
        p = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])      # bukan lewat spawn()
        self.addCleanup(lambda: p.poll() is None and p.kill())
        kill_tree(p)
        self.assertIsNotNone(p.wait(5))

    def test_already_dead_is_noop(self):
        p = spawn([sys.executable, "-c", "pass"])
        p.wait()
        kill_tree(p)                                   # tidak boleh melempar

    def test_cancel_through_job_manager_reaches_grandchild(self):
        with tempfile.TemporaryDirectory() as d:
            store = Store(Path(d) / "t.db")
            mgr = JobManager(store, 1)
            seen = []

            def handler(ctx, p):
                code, out, err = run_streaming([sys.executable, "-c", PARENT], on_line=lambda l: seen.append(int(l)),
                                               is_cancelled=ctx.is_cancelled, register=ctx.register)
                return {}
            jid = mgr.submit("t", {}, handler, pool="render")
            for _ in range(100):
                if seen:
                    break
                time.sleep(0.05)
            self.addCleanup(lambda: alive(seen[0]) and os.kill(seen[0], signal.SIGKILL))
            self.assertTrue(alive(seen[0]))
            t0 = time.time()
            mgr.cancel(jid)
            end = time.time() + 10
            while time.time() < end and store.get_job(jid)["status"] not in ("cancelled", "done", "failed"):
                time.sleep(0.05)
            self.assertEqual(store.get_job(jid)["status"], "cancelled")
            self.assertTrue(wait_dead(seen[0]))
            self.assertLess(time.time() - t0, 6)
            mgr.shutdown()

    def test_shutdown_kills_running_processes(self):
        with tempfile.TemporaryDirectory() as d:
            store = Store(Path(d) / "t.db")
            mgr = JobManager(store, 1)
            seen = []

            def handler(ctx, p):
                run_streaming([sys.executable, "-c", PARENT], on_line=lambda l: seen.append(int(l)),
                              is_cancelled=ctx.is_cancelled, register=ctx.register)
                return {}
            mgr.submit("t", {}, handler, pool="render")
            for _ in range(100):
                if seen:
                    break
                time.sleep(0.05)
            self.addCleanup(lambda: alive(seen[0]) and os.kill(seen[0], signal.SIGKILL))
            mgr.shutdown()                              # = yang dijalankan saat server berhenti (atexit)
            self.assertTrue(wait_dead(seen[0]))


class SpawnTests(unittest.TestCase):
    def test_spawn_isolates_process_group_on_posix(self):
        if IS_WINDOWS:
            self.skipTest("POSIX saja")
        p = spawn([sys.executable, "-c", "import time; time.sleep(5)"])
        try:
            self.assertEqual(os.getpgid(p.pid), p.pid)
            self.assertNotEqual(os.getpgid(p.pid), os.getpgid(0))
        finally:
            p.kill()
            p.wait()

    def test_timeout_kills_whole_tree(self):
        if IS_WINDOWS or not Path("/proc").exists():
            self.skipTest("Linux saja")
        seen = []
        t0 = time.time()
        rc, out, err = run_streaming([sys.executable, "-c", PARENT], on_line=lambda l: seen.append(int(l)), timeout=1.0)
        self.assertLess(time.time() - t0, 8)
        self.assertTrue(wait_dead(seen[0]))


if __name__ == "__main__":
    unittest.main()

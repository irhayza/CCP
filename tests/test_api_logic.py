"""Eksekusi handler HTTP di backend/main.py memakai stub FastAPI (aktif hanya bila FastAPI asli tidak terpasang)."""
import asyncio, os, stat, tempfile, time, unittest, zipfile
from pathlib import Path

from tests import fakeweb

fakeweb.install()
FAKE = getattr(__import__("sys").modules.get("fastapi"), "__fake__", False)

from backend.config import load_settings          # noqa: E402
from backend.security import InputError            # noqa: E402
from tests.helpers import HAVE_FFMPEG, FakeYtdlp, make_video   # noqa: E402

if FAKE:
    from backend import main                       # noqa: E402

URL = "https://youtu.be/dQw4w9WgXcQ"
STYLE = {"layout": {"mode": "fill_center"}, "resolution": 720, "captions": {"enabled": False}, "quality": "fast"}


class FlakyYtdlp(FakeYtdlp):
    def __init__(self, *a, **k):
        super().__init__(*a, **k)
        self.fail = True

    def download_section(self, *a, **k):
        if self.fail:
            self.fail = False
            raise RuntimeError("jaringan putus")
        return super().download_section(*a, **k)

    def stream_url(self, url, vid):
        return str(make_video(self.tmp / "stream.mp4", 4))


@unittest.skipUnless(FAKE and HAVE_FFMPEG, "butuh stub FastAPI + ffmpeg")
class ApiLogicTests(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.TemporaryDirectory()
        self.tmp = Path(self.d.name)
        self.settings = load_settings({"DATA_DIR": str(self.tmp / "data"), "HOST": "127.0.0.1", "RENDER_WORKERS": "2"})
        self.fy = FlakyYtdlp(self.tmp)
        self.app = main.create_app(self.settings, ytdlp=self.fy)
        self.r = self.app.routes

    def tearDown(self):
        self.app.state.jobs.shutdown()
        self.d.cleanup()

    def call(self, method, path, *a, **k):
        return self.r[(method, path)](*a, **k)

    def wait_job(self, jid, timeout=60):
        end = time.time() + timeout
        while time.time() < end:
            j = self.call("GET", "/api/jobs/{job_id}", jid)
            if j["status"] in ("done", "failed", "cancelled"):
                return j
            time.sleep(0.1)
        raise AssertionError("timeout")

    def wait_batch(self, bid, timeout=90):
        end = time.time() + timeout
        while time.time() < end:
            b = self.call("GET", "/api/render-progress/{batch_id}", bid)
            if b["finished"]:
                return b
            time.sleep(0.2)
        raise AssertionError("timeout")

    def render(self, n=2, style=STYLE):
        clips = [main.ClipIn(id=f"c{i}", start=10.0 * i, end=10.0 * i + 5, title=f"Klip {i}", hook="Hook", hashtags=["a"])
                 for i in range(1, n + 1)]
        return self.call("POST", "/api/render-batch", main.RenderReq(url=URL, clips=clips, style=style))

    # ------------------------------------------------------------------ dasar
    def test_health_presets_and_models_guard(self):
        h = self.call("GET", "/api/health")
        self.assertTrue(h["ok"] and h["ffmpeg"] and not h["auth_required"])
        p = self.call("GET", "/api/presets")
        self.assertIn("viral_pop", p["captions"])
        self.assertEqual(set(p["layouts"]), {"fill_face", "fill_center", "letterbox", "split", "pip"})
        with self.assertRaises(InputError):
            self.call("GET", "/api/models", None)
        with self.assertRaises(InputError):
            self.call("GET", "/api/models", "mock")

    def test_analyze_end_to_end_and_input_validation(self):
        jid = self.call("POST", "/api/analyze", main.AnalyzeReq(url=URL, preset="short", max_clips=3), None)["job_id"]
        j = self.wait_job(jid)
        self.assertEqual(j["status"], "done", j.get("error"))
        self.assertEqual(j["result"]["video"]["id"], "dQw4w9WgXcQ")
        self.assertTrue(1 <= len(j["result"]["moments"]) <= 3)
        events = list(self.call("GET", "/api/jobs/{job_id}/events", jid).iterator)
        self.assertIn('"status": "done"', events[-1])
        for bad in (dict(url="https://evil.com/x"), dict(url="file:///etc/passwd"), dict(url=URL, min_len=30.0, max_len=20.0)):
            with self.assertRaises(InputError):
                self.call("POST", "/api/analyze", main.AnalyzeReq(**bad), None)
        with self.assertRaises(InputError):
            self.call("GET", "/api/jobs/{job_id}", "../../etc")

    def test_api_key_never_persisted(self):
        key = "AIzaSyDUMMYDUMMYDUMMYDUMMY12345678901"
        jid = self.call("POST", "/api/analyze", main.AnalyzeReq(url=URL, preset="short", max_clips=2), key)["job_id"]
        self.wait_job(jid)
        raw = b"".join(p.read_bytes() for p in self.settings.data_dir.glob("promax.db*"))
        self.assertNotIn(key.encode(), raw)

    # ------------------------------------------------------------------ render batch & unduhan
    def test_batch_render_zip_csv_and_downloads(self):
        self.fy.fail = False
        res = self.render(2)
        self.assertEqual(len(res["job_ids"]), 2)
        b = self.wait_batch(res["batch_id"])
        self.assertEqual((b["done"], b["total"]), (2, 2), [j.get("error") for j in b["jobs"]])

        first = b["jobs"][0]["result"]
        dl = self.call("GET", "/api/download-rendered/{file_name}", first["file"])
        self.assertTrue(Path(dl.path).is_file())
        self.assertEqual(dl.media_type, "video/mp4")
        thumb = self.call("GET", "/api/download-rendered/{file_name}", first["thumbnail"])
        self.assertEqual(thumb.media_type, "image/jpeg")
        for bad in ("../promax.db", "..\\x.mp4", "a/b.mp4"):
            with self.assertRaises(InputError):
                self.call("GET", "/api/download-rendered/{file_name}", bad)
        with self.assertRaises(main.HTTPException) as cm:
            self.call("GET", "/api/download-rendered/{file_name}", "tidak-ada.mp4")
        self.assertEqual(cm.exception.status_code, 404)
        (self.settings.exports_dir / "rahasia.txt").write_text("x")
        with self.assertRaises(main.HTTPException):                       # hanya ekstensi media yang boleh diunduh
            self.call("GET", "/api/download-rendered/{file_name}", "rahasia.txt")

        z = self.call("GET", "/api/download-batch-zip/{batch_id}", res["batch_id"])
        with zipfile.ZipFile(z.path) as zf:
            names = zf.namelist()
            self.assertEqual(sum(n.endswith(".mp4") for n in names), 2)
            csv_text = zf.read("publish-pack.csv").decode("utf-8-sig")
        self.assertIn("Klip 1", csv_text)
        self.assertIn("#a", csv_text)
        z.background.run()
        self.assertFalse(Path(z.path).exists())                           # zip sementara dibersihkan

        pack = self.call("GET", "/api/publish-pack/{batch_id}", res["batch_id"])
        self.assertTrue(pack.body.startswith("\ufeff"))
        self.assertIn("attachment", pack.headers["Content-Disposition"])
        stream = list(self.call("GET", "/api/batches/{batch_id}/events", res["batch_id"]).iterator)
        self.assertIn('"done"', stream[-1])

    def test_render_validation_happens_before_queueing(self):
        for style, clip in ((dict(layout={"mode": "wat"}), dict(start=0, end=10)), ({}, dict(start=0, end=9999)),
                            ({}, dict(start=5, end=6)), ({"quality": "ultra"}, dict(start=0, end=10))):
            c = main.ClipIn(id="x", title="t", **clip)
            with self.assertRaises(InputError):
                self.call("POST", "/api/render-batch", main.RenderReq(url=URL, clips=[c], style=style))
        with self.assertRaises(InputError):
            self.call("POST", "/api/render-batch", main.RenderReq(url="https://evil.com/a", clips=[main.ClipIn(id="x", start=0, end=10)]))
        self.assertEqual(self.app.state.store.count_active(), 0)

    def test_failed_job_retry_keeps_same_id_and_cancel(self):
        res = self.render(1)                         # FlakyYtdlp: percobaan pertama gagal
        b = self.wait_batch(res["batch_id"])
        job = b["jobs"][0]
        self.assertEqual(job["status"], "failed")
        self.assertIn("jaringan putus", job["error"])
        out = self.call("POST", "/api/jobs/{job_id}/retry", job["id"])
        self.assertEqual(out["job_id"], job["id"])
        b2 = self.wait_batch(res["batch_id"])
        self.assertEqual((b2["total"], b2["done"]), (1, 1))
        with self.assertRaises(InputError):          # job sukses tak bisa diulang
            self.call("POST", "/api/jobs/{job_id}/retry", job["id"])
        self.assertEqual(self.call("POST", "/api/jobs/{job_id}/cancel", job["id"]), {"cancelled": False})
        with self.assertRaises(main.HTTPException):
            self.call("GET", "/api/render-progress/{batch_id}", "abcdef0123456789")

    def test_preview_frame(self):
        r = self.call("GET", "/api/clip-frame", "dQw4w9WgXcQ", 1.0)
        self.assertEqual(r.media_type, "image/jpeg")
        self.assertTrue(Path(r.path).stat().st_size > 1000)
        with self.assertRaises(InputError):
            self.call("GET", "/api/clip-frame", "../../x", 1.0)
        with self.assertRaises(InputError):
            self.call("GET", "/api/clip-frame", "short", 1.0)

    # ------------------------------------------------------------------ cookies, storage
    def test_cookies_lifecycle_and_permissions(self):
        self.assertEqual(self.call("GET", "/api/cookies"), {"present": False})
        good = ".youtube.com\tTRUE\t/\tTRUE\t0\tSID\tnilai-rahasia\n"
        out = self.call("POST", "/api/cookies", main.CookiesReq(text=good))
        self.assertEqual(out["count"], 1)
        st = self.call("GET", "/api/cookies")
        self.assertTrue(st["valid"])
        self.assertNotIn("nilai-rahasia", str(st))
        if os.name == "posix":
            self.assertEqual(stat.S_IMODE(self.settings.cookies_file.stat().st_mode), 0o600)
        with self.assertRaises(InputError):
            self.call("POST", "/api/cookies", main.CookiesReq(text="bukan cookies"))
        self.assertTrue(self.settings.cookies_file.is_file())             # input buruk tidak menimpa file lama
        self.call("DELETE", "/api/cookies")
        self.assertFalse(self.call("GET", "/api/cookies")["present"])

    def test_clear_temp_refuses_while_busy(self):
        (self.settings.work_dir / "x.bin").write_bytes(b"1" * 100)
        self.app.state.store.create_job("render", {})                     # job 'queued' = sibuk
        with self.assertRaises(InputError):
            self.call("POST", "/api/clear-temp")
        self.assertTrue((self.settings.work_dir / "x.bin").exists())
        self.app.state.store.update_job(self.app.state.store._db.execute("select id from jobs").fetchone()[0], status="done")
        self.assertEqual(self.call("POST", "/api/clear-temp")["freed_bytes"], 100)
        self.assertIn("temp", self.call("GET", "/api/temp-storage-info"))

    def test_update_ytdlp_disabled_by_default(self):
        from backend.ytdlp import YtDlp
        app = main.create_app(load_settings({"DATA_DIR": str(self.tmp / "upd"), "HOST": "127.0.0.1"}),
                              ytdlp=YtDlp(self.settings))           # YtDlp asli: menolak sebelum menjalankan apa pun
        try:
            with self.assertRaises(InputError) as cm:
                app.routes[("POST", "/api/system/update-ytdlp")]()
            self.assertIn("ALLOW_SELF_UPDATE", str(cm.exception))
        finally:
            app.state.jobs.shutdown()

    # ------------------------------------------------------------------ keamanan
    def run_guard(self, headers, method="GET", app=None):
        app = app or self.app
        hit = []

        async def nxt(req):
            hit.append(1)
            return "OK"
        out = asyncio.run(app.mws[0](main.Request(headers=headers, method=method), nxt))
        return out, bool(hit)

    def test_host_and_origin_guard(self):
        self.assertEqual(self.run_guard({"host": "localhost:8000"}), ("OK", True))
        self.assertEqual(self.run_guard({"host": "127.0.0.1:8000"}), ("OK", True))
        self.assertEqual(self.run_guard({"host": "[::1]:8000"}), ("OK", True))
        out, hit = self.run_guard({"host": "evil.example:8000"})             # DNS rebinding
        self.assertEqual((out.status_code, hit), (403, False))
        self.assertEqual(self.run_guard({"host": "localhost:8000", "origin": "http://localhost:8000"}, "POST"), ("OK", True))
        out, hit = self.run_guard({"host": "localhost:8000", "origin": "https://evil.example"}, "POST")   # CSRF
        self.assertEqual((out.status_code, hit), (403, False))
        self.assertEqual(self.run_guard({"host": "localhost:8000", "origin": "https://evil.example"}, "GET")[1], True)
        # server dibuka ke jaringan: pemeriksaan Host dimatikan (pakai APP_TOKEN)
        lan = main.create_app(load_settings({"DATA_DIR": str(self.tmp / "lan"), "HOST": "0.0.0.0"}), ytdlp=self.fy)
        self.assertEqual(self.run_guard({"host": "192.168.1.5:8000"}, app=lan), ("OK", True))
        lan.state.jobs.shutdown()

    def test_token_auth(self):
        s = load_settings({"DATA_DIR": str(self.tmp / "auth"), "HOST": "127.0.0.1", "APP_TOKEN": "rahasia123"})
        app = main.create_app(s, ytdlp=self.fy)
        try:
            dep = app.auth[("GET", "/api/presets")][0]
            for req in (main.Request(), main.Request(cookies={"ccpm_token": "salah"}), main.Request(headers={"x-app-token": "x"})):
                with self.assertRaises(main.HTTPException) as cm:
                    dep(req)
                self.assertEqual(cm.exception.status_code, 401)
            self.assertIsNone(dep(main.Request(cookies={"ccpm_token": "rahasia123"})))
            self.assertIsNone(dep(main.Request(headers={"x-app-token": "rahasia123"})))
            self.assertEqual(app.auth[("GET", "/api/health")], [])         # health & login tidak butuh token
            self.assertEqual(app.auth[("POST", "/api/login")], [])
            self.assertTrue(all(app.auth[k] for k in app.auth if k[1] not in
                                ("/api/health", "/api/auth-status", "/api/login", "/api/logout")))   # sisanya WAJIB token
            self.assertEqual(app.routes[("GET", "/api/auth-status")](main.Request())["authed"], False)
            resp = main.Response()
            app.routes[("POST", "/api/login")](main.LoginReq(token="rahasia123"), resp)
            val, kw = resp.cookies["ccpm_token"]
            self.assertTrue(kw["httponly"] and kw["samesite"] == "strict")
            with self.assertRaises(main.HTTPException):
                app.routes[("POST", "/api/login")](main.LoginReq(token="salah"), main.Response())
        finally:
            app.state.jobs.shutdown()


if __name__ == "__main__":
    unittest.main()

"""Uji HTTP dengan FastAPI + TestClient ASLI (butuh: pip install -r requirements-dev.txt).
Otomatis dilewati bila FastAPI/httpx tidak terpasang; tests/test_api_logic.py menutup logika yang sama tanpa dependensi."""
import tempfile, unittest
from pathlib import Path

try:
    from fastapi.testclient import TestClient
    HAVE = True
except ImportError:
    HAVE = False

from backend.config import load_settings
from tests.helpers import FakeYtdlp


@unittest.skipUnless(HAVE, "FastAPI/httpx asli tidak terpasang")
class HttpTests(unittest.TestCase):
    def make(self, **env):
        from backend.main import create_app
        d = tempfile.TemporaryDirectory()
        self.addCleanup(d.cleanup)
        s = load_settings({"DATA_DIR": str(Path(d.name) / "data"), "HOST": "0.0.0.0", **env})
        app = create_app(s, ytdlp=FakeYtdlp(Path(d.name)))
        self.addCleanup(app.state.store.close)
        self.addCleanup(app.state.jobs.shutdown)
        return TestClient(app)

    def test_basic_endpoints_and_static_ui(self):
        c = self.make()
        self.assertTrue(c.get("/api/health").json()["ok"])
        self.assertIn("viral_pop", c.get("/api/presets").json()["captions"])
        idx = c.get("/")
        self.assertEqual(idx.status_code, 200)
        self.assertIn("Cheat Clip", idx.text)
        self.assertIn("javascript", c.get("/js/app.js").headers["content-type"])

    def test_validation_errors_are_4xx_never_500(self):
        c = self.make()
        r = c.post("/api/analyze", json={"url": "https://evil.com/x"})
        self.assertEqual(r.status_code, 400)
        self.assertIn("tidak diizinkan", r.json()["detail"])
        self.assertEqual(c.post("/api/analyze", json={}).status_code, 422)
        bad = {"url": "https://youtu.be/dQw4w9WgXcQ", "style": {"layout": {"mode": "wat"}},
               "clips": [{"id": "a", "start": 0, "end": 10}]}
        self.assertEqual(c.post("/api/render-batch", json=bad).status_code, 400)
        self.assertIn(c.get("/api/download-rendered/..%2Fpromax.db").status_code, (400, 404))
        self.assertEqual(c.get("/api/jobs/abc").status_code, 404)
        self.assertEqual(c.get("/api/models").status_code, 400)       # tanpa header X-Gemini-Key

    def test_api_key_travels_in_header_only(self):
        import time
        c = self.make()
        r = c.post("/api/analyze", json={"url": "https://youtu.be/dQw4w9WgXcQ"}, headers={"X-Gemini-Key": "mock"})
        self.assertEqual(r.status_code, 200)
        jid = r.json()["job_id"]
        self.assertTrue(c.get(f"/api/jobs/{jid}").status_code == 200)
        for _ in range(50):
            if c.get(f"/api/jobs/{jid}").json().get("status") in ("done", "failed", "cancelled"):
                break
            time.sleep(0.05)

    def test_host_guard_and_token_auth(self):
        c = self.make(HOST="127.0.0.1")
        self.assertEqual(c.get("/api/health").status_code, 403)                          # Host 'testserver' ditolak
        self.assertEqual(c.get("/api/health", headers={"host": "localhost:8000"}).status_code, 200)
        t = self.make(APP_TOKEN="rahasia123")
        self.assertEqual(t.get("/api/presets").status_code, 401)
        self.assertEqual(t.get("/api/health").status_code, 200)
        self.assertEqual(t.post("/api/login", json={"token": "salah"}).status_code, 401)
        self.assertEqual(t.post("/api/login", json={"token": "rahasia123"}).status_code, 200)
        self.assertEqual(t.get("/api/presets").status_code, 200)                         # cookie httpOnly terpasang


if __name__ == "__main__":
    unittest.main()

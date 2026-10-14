import json, subprocess, sys, tempfile, time, unittest
from pathlib import Path
from unittest import mock

from backend.db import Store
from backend.jobs import JobManager
from backend.pipeline import (clear_temp, options_from_style, run_analysis, run_render, slugify, storage_info)
from backend.render import probe
from backend.security import InputError
from tests.helpers import HAVE_FFMPEG, DummyCtx, FakeYtdlp, make_services

P = {"url": "https://www.youtube.com/watch?v=dQw4w9WgXcQ", "video_id": "dQw4w9WgXcQ", "preset": "short",
     "min_len": None, "max_len": None, "max_clips": 4, "focus": "", "language": "id", "use_cache": True}


def wait(mgr, jid, timeout=15):
    end = time.time() + timeout
    while time.time() < end:
        j = mgr.store.get_job(jid)
        if j["status"] in ("done", "failed", "cancelled"):
            return j
        time.sleep(0.05)
    raise AssertionError("timeout")


class JobManagerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.tmp.name) / "t.db")
        self.mgr = JobManager(self.store, 1)

    def tearDown(self):
        self.mgr.shutdown()
        self.store.close()
        self.tmp.cleanup()

    def test_success_and_progress(self):
        def h(ctx, p):
            ctx.progress(50, "x", "setengah")
            return {"v": p["a"] * 2}
        j = wait(self.mgr, self.mgr.submit("t", {"a": 21}, h, pool="analyze"))
        self.assertEqual((j["status"], j["result"], j["progress"]), ("done", {"v": 42}, 100.0))

    def test_failure_is_reported_not_raised(self):
        def h(ctx, p):
            raise InputError("pesan jelas")
        j = wait(self.mgr, self.mgr.submit("t", {}, h, pool="analyze"))
        self.assertEqual((j["status"], j["error"]), ("failed", "pesan jelas"))

    def test_cancel_kills_running_subprocess(self):
        started = []

        def h(ctx, p):
            proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
            ctx.register(proc)
            started.append(proc)
            proc.wait()
            return {}
        jid = self.mgr.submit("t", {}, h, pool="render")
        for _ in range(100):
            if started:
                break
            time.sleep(0.05)
        t0 = time.time()
        self.assertTrue(self.mgr.cancel(jid))
        j = wait(self.mgr, jid)
        self.assertEqual(j["status"], "cancelled")
        self.assertLess(time.time() - t0, 5)
        self.assertIsNotNone(started[0].poll())

    def test_cancel_queued_job_never_runs(self):
        ran = []
        gate = []

        def slow(ctx, p):
            while not gate:
                time.sleep(0.02)
            return {}
        a = self.mgr.submit("t", {}, slow, pool="render")          # 1 worker -> b antre
        b = self.mgr.submit("t", {}, lambda c, p: ran.append(1) or {}, pool="render")
        self.assertTrue(self.mgr.cancel(b))
        gate.append(1)
        wait(self.mgr, a)
        time.sleep(0.2)
        self.assertEqual(ran, [])
        self.assertEqual(self.store.get_job(b)["status"], "cancelled")

    def test_retry_reuses_same_job_id_after_failure_and_after_queued_cancel(self):
        calls = []

        def flaky(ctx, p):
            calls.append(1)
            if len(calls) == 1:
                raise RuntimeError("gagal pertama")
            return {"ok": True}
        jid = self.mgr.submit("t", {"x": 1}, flaky, pool="render", batch_id="bb")
        self.assertEqual(wait(self.mgr, jid)["status"], "failed")
        self.assertTrue(self.mgr.resubmit(jid, flaky, pool="render"))
        j = wait(self.mgr, jid)
        self.assertEqual((j["status"], j["result"], j["error"]), ("done", {"ok": True}, None))
        self.assertEqual(len(self.store.batch_jobs("bb")), 1)            # tidak ada baris ganda
        self.assertFalse(self.mgr.resubmit(jid, flaky, pool="render"))   # job sukses tidak bisa diulang

        gate = []
        a = self.mgr.submit("t", {}, lambda c, p: [time.sleep(0.02) for _ in iter(lambda: bool(gate), True)] and {}, pool="render")
        b = self.mgr.submit("t", {}, lambda c, p: {"b": 1}, pool="render")
        self.mgr.cancel(b)
        gate.append(1)
        wait(self.mgr, a)
        time.sleep(0.2)
        self.assertEqual(self.store.get_job(b)["status"], "cancelled")
        self.assertFalse(self.mgr.is_cancelled(b))                       # flag tidak bocor
        self.assertTrue(self.mgr.resubmit(b, lambda c, p: {"b": 2}, pool="render"))
        self.assertEqual(wait(self.mgr, b)["result"], {"b": 2})

    def test_batch_cancel_and_cannot_cancel_finished(self):
        j = self.mgr.submit("t", {}, lambda c, p: {}, pool="analyze", batch_id="b1")
        wait(self.mgr, j)
        self.assertFalse(self.mgr.cancel(j))
        self.assertEqual(self.mgr.cancel_batch("b1"), 0)

    def test_events_stream_ends_with_terminal_state(self):
        jid = self.mgr.submit("t", {}, lambda c, p: {"ok": 1}, pool="analyze")
        chunks = list(self.mgr.events(jid, poll=0.02))
        last = json.loads(chunks[-1].split("data: ", 1)[1])
        self.assertEqual(last["status"], "done")
        self.assertEqual(list(self.mgr.events("nope", poll=0.01))[0].split("\n")[0], "event: gone")

    def test_running_jobs_marked_interrupted_after_restart(self):
        jid = self.store.create_job("t", {})
        self.store.update_job(jid, status="running")
        self.store.close()
        again = Store(Path(self.tmp.name) / "t.db")
        self.assertEqual(again.get_job(jid)["status"], "interrupted")
        self.store = again


@unittest.skipUnless(HAVE_FFMPEG, "butuh ffmpeg")
class AnalysisTests(unittest.TestCase):
    def setUp(self):
        self.tmpd = tempfile.TemporaryDirectory()
        self.tmp = Path(self.tmpd.name)
        self._stores: list = []  # daftar Store yang dibuka agar bisa ditutup di tearDown

    def tearDown(self):
        for s in self._stores:
            try:
                s.close()
            except Exception:
                pass
        self.tmpd.cleanup()

    def _svc(self, tmp=None, ytdlp=None, **env):
        """Wrapper make_services yang otomatis mendaftarkan store untuk cleanup."""
        svc, s, store = make_services(tmp or self.tmp, ytdlp, **env)
        self._stores.append(store)
        return svc, s, store

    def test_without_key_uses_heatmap_and_caches(self):
        svc, s, store = self._svc()
        r = run_analysis(svc, DummyCtx(), dict(P), "")
        self.assertEqual(r["meta"]["mode"], "noai")
        self.assertEqual(r["meta"]["transcript"], "youtube")
        ms = r["moments"]
        self.assertTrue(ms)
        self.assertEqual([m["score"] for m in ms], sorted((m["score"] for m in ms), reverse=True))
        self.assertTrue(all(0 <= m["start"] < m["end"] <= 120 for m in ms))
        self.assertTrue(all(8 <= m["duration"] <= 24.5 for m in ms))
        self.assertTrue(48 <= (ms[0]["start"] + ms[0]["end"]) / 2 * 100 / 120 <= 60)   # klip teratas ada di puncak heatmap
        n = len(svc.ytdlp.calls)
        r2 = run_analysis(svc, DummyCtx(), dict(P), "")
        self.assertTrue(r2["from_cache"])
        self.assertEqual(len(svc.ytdlp.calls), n)

    def test_ai_path_with_fallback_note(self):
        svc, s, store = self._svc()
        cands = [{"start": 30.2, "end": 52.0, "score": 92, "title": "Judul AI", "hook": "Hook!", "reason": "r",
                  "hashtags": ["a", "b"]}]
        with mock.patch("backend.pipeline.gemini.list_models", return_value=["m1"]), \
             mock.patch("backend.pipeline.gemini.find_candidates", return_value=(cands, ["m1"], ["m0: quota", "m1: ok"])) as fc:
            r = run_analysis(svc, DummyCtx(), dict(P, use_cache=False), "AIza" + "x" * 30)
        self.assertEqual(r["meta"]["mode"], "ai")
        self.assertEqual(r["moments"][0]["title"], "Judul AI")
        self.assertEqual(r["moments"][0]["source"], "ai")
        self.assertTrue(any("model Gemini" in n for n in r["meta"]["notes"]))
        self.assertEqual(fc.call_args.kwargs["max_clips"], 4)

    def test_ai_total_failure_degrades_gracefully_but_bad_key_fails_loudly(self):
        from backend.gemini import GeminiError
        svc, s, store = self._svc()
        with mock.patch("backend.pipeline.gemini.list_models", return_value=["m1"]), \
             mock.patch("backend.pipeline.gemini.find_candidates", side_effect=GeminiError("quota", "habis")):
            r = run_analysis(svc, DummyCtx(), dict(P, use_cache=False), "K" * 30)
        self.assertTrue(r["moments"])
        self.assertTrue(any("heatmap saja" in n for n in r["meta"]["notes"]))
        with mock.patch("backend.pipeline.gemini.list_models", side_effect=GeminiError("auth", "key salah")):
            with self.assertRaises(InputError):
                run_analysis(svc, DummyCtx(), dict(P, use_cache=False), "K" * 30)

    def test_no_subs_no_heatmap_still_returns_clips(self):
        svc, s, store = self._svc(ytdlp=FakeYtdlp(self.tmp, duration=300, heatmap=False, subs=False))
        with mock.patch("backend.pipeline.whisper_available", return_value=False):
            r = run_analysis(svc, DummyCtx(), dict(P, use_cache=False), "")
        self.assertEqual(r["meta"]["transcript"], "none")
        self.assertTrue(r["moments"])
        self.assertGreaterEqual(len(r["meta"]["notes"]), 2)

    def test_demo_mode_and_short_video_rejected(self):
        svc, s, store = self._svc()
        self.assertEqual(run_analysis(svc, DummyCtx(), dict(P, use_cache=False), "mock")["meta"]["mode"], "demo")
        svc2, *_ = self._svc(tmp=self.tmp / "b", ytdlp=FakeYtdlp(self.tmp, duration=10))
        with self.assertRaises(InputError):
            run_analysis(svc2, DummyCtx(), dict(P, use_cache=False), "")


@unittest.skipUnless(HAVE_FFMPEG, "butuh ffmpeg")
class RenderPipelineTests(unittest.TestCase):
    def setUp(self):
        self.tmpd = tempfile.TemporaryDirectory()
        self.tmp = Path(self.tmpd.name)
        self._stores: list = []

    def tearDown(self):
        for s in self._stores:
            try:
                s.close()
            except Exception:
                pass
        self.tmpd.cleanup()

    def _svc(self, tmp=None, ytdlp=None, **env):
        """Wrapper make_services yang otomatis mendaftarkan store untuk cleanup."""
        svc, s, store = make_services(tmp or self.tmp, ytdlp, **env)
        self._stores.append(store)
        return svc, s, store

    def params(self, **style):
        return {"url": P["url"], "video_id": P["video_id"], "batch_id": "b" * 8, "index": 1, "language": "id",
                "clip": {"id": "c1", "start": 10.0, "end": 16.0, "title": "Momen Seru! Banget", "hook": "Wow", "hashtags": ["x"],
                         "overlay_title": "Judul Overlay"},
                "style": {"title": {"enabled": True, "duration": 3}, "resolution": 720, "export_srt": True,
                          "layout": {"mode": "letterbox"}, **style}}

    def test_full_render_with_captions_title_srt(self):
        svc, s, store = self._svc()
        ctx = DummyCtx()
        res = run_render(svc, ctx, self.params())
        out = s.exports_dir / res["file"]
        self.assertTrue(out.is_file())
        info = probe(out)
        self.assertEqual((info.width, info.height, info.has_audio), (720, 1280, True))
        self.assertAlmostEqual(info.duration, 6.0, delta=0.3)
        self.assertGreater(res["caption_words"], 10)
        self.assertTrue(out.with_suffix(".srt").is_file())
        self.assertIn("kata", out.with_suffix(".srt").read_text())
        self.assertTrue(res["file"].startswith("bbbbbbbb_01_momen-seru-banget"))
        stages = [e[1] for e in ctx.events]
        self.assertEqual(stages[0], "download")
        self.assertIn("encoding", stages)
        self.assertEqual(sorted(e[0] for e in ctx.events), [e[0] for e in ctx.events])    # progres tidak mundur
        self.assertFalse(list((s.work_dir).glob("render_*")))                               # work dir dibersihkan

    def test_no_captions_and_cancel(self):
        svc, s, store = self._svc()
        res = run_render(svc, DummyCtx(), self.params(captions={"enabled": False}, title={"enabled": False}))
        self.assertEqual(res["caption_words"], 0)
        ctx = DummyCtx()
        ctx.cancelled = True
        self.assertEqual(run_render(svc, ctx, self.params()), {})

    def test_bad_input_rejected_before_any_download(self):
        svc, s, store = self._svc()
        p = self.params()
        p["clip"]["end"] = 9999
        with self.assertRaises(InputError):
            run_render(svc, DummyCtx(), p)
        with self.assertRaises(InputError):
            run_render(svc, DummyCtx(), self.params(layout={"mode": "wat"}))
        self.assertNotIn("section", svc.ytdlp.calls)


class StyleAndStorageTests(unittest.TestCase):
    def test_style_defaults_and_validation(self):
        o = options_from_style({})
        self.assertEqual((o.layout.mode, o.captions.preset, o.resolution, o.encoder), ("fill_face", "viral_pop", 1080, "auto"))
        self.assertIsNone(o.title)
        self.assertIsNone(options_from_style({"captions": {"enabled": False}}).captions)
        self.assertEqual(options_from_style({"title": {"enabled": True}}, "Teks").title.text, "Teks")
        for bad in ({"captions": {"preset": "x"}}, {"quality": "ultra"}, {"encoder": "tpu"}, {"layout": {"mode": "x"}}, {"layout": {"cam": "abc"}},
                    {"captions": {"size": "besar"}}, {"layout": None, "title": 5}):
            with self.assertRaises(InputError):
                options_from_style(bad)

    def test_slug(self):
        self.assertEqual(slugify("Halo, Dunia!! ../etc"), "halo-dunia-etc")
        self.assertEqual(slugify("!!!", "c1"), "c1")

    def test_storage_clear_keeps_exports_and_refuses_when_busy(self):
        with tempfile.TemporaryDirectory() as d:
            svc, s, store = make_services(Path(d))
            (s.exports_dir / "keep.mp4").write_bytes(b"x" * 100)
            (s.work_dir / "tmp.bin").write_bytes(b"y" * 2048)
            (s.cache_dir / "slices" / "s.mp4").write_bytes(b"z" * 4096)
            info = storage_info(s)
            self.assertEqual(info["cleanable"]["bytes"], 2048 + 4096)
            with self.assertRaises(InputError):
                clear_temp(s, busy=True)
            self.assertEqual(clear_temp(s, busy=False)["freed_bytes"], 6144)
            self.assertTrue((s.exports_dir / "keep.mp4").is_file())
            self.assertFalse((s.work_dir / "tmp.bin").exists())
            self.assertTrue((s.cache_dir / "slices").is_dir())
            store.close()


if __name__ == "__main__":
    unittest.main()

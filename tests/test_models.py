import contextlib, hashlib, io, tempfile, unittest
from dataclasses import replace
from pathlib import Path

from backend import models
from backend.models import ModelError, ModelSpec, fetch, file_ok

PAYLOAD = b"onnx-bytes-" * 5000


def spec_for(data: bytes, urls=("https://example.test/m.onnx",)) -> ModelSpec:
    return ModelSpec(key="demo", filename="demo.onnx", urls=urls, sha256=hashlib.sha256(data).hexdigest(),
                     size=len(data), license="MIT")


class FakeResp(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *a):
        self.close()


class Opener:
    """Pengganti urlopen: peta url -> bytes (atau Exception)."""
    def __init__(self, table):
        self.table, self.calls = table, []

    def __call__(self, req, timeout=None):
        url = req.full_url
        self.calls.append(url)
        v = self.table[url]
        if isinstance(v, Exception):
            raise v
        return FakeResp(v)


class FetchTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.spec = spec_for(PAYLOAD)
        self.url = self.spec.urls[0]

    def leftovers(self):
        return sorted(p.name for p in self.tmp.iterdir())

    def test_downloads_verifies_and_installs(self):
        op = Opener({self.url: PAYLOAD})
        out = fetch(self.spec, self.tmp, opener=op)
        self.assertEqual(out, self.tmp / "demo.onnx")
        self.assertEqual(out.read_bytes(), PAYLOAD)
        self.assertEqual(self.leftovers(), ["demo.onnx"])          # tidak ada sisa .part

    def test_is_idempotent_when_file_is_already_correct(self):
        (self.tmp / "demo.onnx").write_bytes(PAYLOAD)
        op = Opener({})
        fetch(self.spec, self.tmp, opener=op)
        self.assertEqual(op.calls, [])

    def test_force_redownloads(self):
        (self.tmp / "demo.onnx").write_bytes(PAYLOAD)
        op = Opener({self.url: PAYLOAD})
        fetch(self.spec, self.tmp, opener=op, force=True)
        self.assertEqual(op.calls, [self.url])

    def test_corrupt_existing_file_is_replaced(self):
        (self.tmp / "demo.onnx").write_bytes(b"rusak")
        self.assertFalse(file_ok(self.tmp / "demo.onnx", self.spec))
        fetch(self.spec, self.tmp, opener=Opener({self.url: PAYLOAD}))
        self.assertTrue(file_ok(self.tmp / "demo.onnx", self.spec))

    def test_hash_mismatch_installs_nothing(self):
        evil = b"x" * len(PAYLOAD)                                  # ukuran sama, isi beda
        with self.assertRaises(ModelError) as cm:
            fetch(self.spec, self.tmp, opener=Opener({self.url: evil}))
        self.assertIn("SHA-256", str(cm.exception))
        self.assertEqual(self.leftovers(), [])

    def test_wrong_size_installs_nothing(self):
        with self.assertRaises(ModelError):
            fetch(self.spec, self.tmp, opener=Opener({self.url: PAYLOAD[:-10]}))
        self.assertEqual(self.leftovers(), [])

    def test_oversized_stream_is_cut_off(self):
        huge = PAYLOAD + b"y" * 10_000
        with self.assertRaises(ModelError) as cm:
            fetch(self.spec, self.tmp, opener=Opener({self.url: huge}))
        self.assertIn("melebihi", str(cm.exception))
        self.assertEqual(self.leftovers(), [])

    def test_falls_back_to_second_url(self):
        spec = spec_for(PAYLOAD, urls=("https://a.test/m", "https://b.test/m"))
        op = Opener({"https://a.test/m": OSError("down"), "https://b.test/m": PAYLOAD})
        fetch(spec, self.tmp, opener=op)
        self.assertEqual(op.calls, ["https://a.test/m", "https://b.test/m"])

    def test_all_urls_failing_reports_each_host(self):
        spec = spec_for(PAYLOAD, urls=("https://a.test/m", "https://b.test/m"))
        op = Opener({"https://a.test/m": OSError("down"), "https://b.test/m": b"salah"})
        with self.assertRaises(ModelError) as cm:
            fetch(spec, self.tmp, opener=op)
        self.assertIn("a.test", str(cm.exception))
        self.assertIn("b.test", str(cm.exception))
        self.assertEqual(self.leftovers(), [])

    def test_plain_http_is_refused_without_a_request(self):
        spec = spec_for(PAYLOAD, urls=("http://insecure.test/m",))
        op = Opener({})
        with self.assertRaises(ModelError):
            fetch(spec, self.tmp, opener=op)
        self.assertEqual(op.calls, [])


class CatalogTests(unittest.TestCase):
    def test_yunet_entry_is_well_formed(self):
        s = models.MODELS["yunet"]
        self.assertRegex(s.sha256, r"^[0-9a-f]{64}$")
        self.assertGreater(s.size, 100_000)
        self.assertTrue(all(u.startswith("https://") for u in s.urls))
        self.assertGreaterEqual(len(s.urls), 2)

    def test_filename_is_found_by_facetrack(self):
        """Nama file hasil unduhan harus cocok dengan pola yang dicari find_yunet_model."""
        from backend.facetrack import find_yunet_model
        d = Path(tempfile.mkdtemp())
        (d / models.YUNET.filename).write_bytes(b"x")
        self.assertEqual(find_yunet_model(None, d), d / models.YUNET.filename)


class CliTests(unittest.TestCase):
    def run_main(self, argv, opener):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            rc = models.main(argv, opener=opener)
        return rc, out.getvalue(), err.getvalue()

    def test_installs_into_dir_and_reports(self):
        d = tempfile.mkdtemp()
        real = models.MODELS
        models.MODELS = {"demo": spec_for(PAYLOAD)}
        try:
            rc, out, _ = self.run_main(["--dir", d], Opener({"https://example.test/m.onnx": PAYLOAD}))
        finally:
            models.MODELS = real
        self.assertEqual(rc, 0)
        self.assertIn("OK  demo", out)
        self.assertTrue((Path(d) / "demo.onnx").is_file())

    def test_failure_returns_1(self):
        d = tempfile.mkdtemp()
        real = models.MODELS
        models.MODELS = {"demo": spec_for(PAYLOAD)}
        try:
            rc, _, err = self.run_main(["--dir", d], Opener({"https://example.test/m.onnx": OSError("offline")}))
        finally:
            models.MODELS = real
        self.assertEqual(rc, 1)
        self.assertIn("GAGAL", err)

    def test_unknown_name_returns_2(self):
        rc, _, err = self.run_main(["nope", "--dir", tempfile.mkdtemp()], Opener({}))
        self.assertEqual(rc, 2)
        self.assertIn("nope", err)


if __name__ == "__main__":
    unittest.main()

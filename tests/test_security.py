import tempfile, unittest
from pathlib import Path
from backend.security import (InputError, parse_video_url, redact, safe_channel_url, safe_child, token_ok, valid_id,
                              validate_netscape_cookies)
from backend.config import DEFAULT_ALLOWED_HOSTS as H


class UrlTests(unittest.TestCase):
    def test_variants_normalise(self):
        for u in ("https://youtu.be/dQw4w9WgXcQ", "https://www.youtube.com/watch?v=dQw4w9WgXcQ&t=10",
                  "https://youtube.com/shorts/dQw4w9WgXcQ", "http://m.youtube.com/live/dQw4w9WgXcQ"):
            url, vid = parse_video_url(u, H)
            self.assertEqual(vid, "dQw4w9WgXcQ")
            self.assertEqual(url, "https://www.youtube.com/watch?v=dQw4w9WgXcQ")

    def test_rejects_dangerous(self):
        for u in ("file:///etc/passwd", "ftp://youtube.com/x", "https://evil.com/watch?v=dQw4w9WgXcQ",
                  "https://youtube.com.evil.com/watch?v=dQw4w9WgXcQ", "https://www.youtube.com/watch?v=short",
                  "https://www.youtube.com/", "-o /tmp/x", "http://127.0.0.1:8000/x", "x" * 600):
            with self.assertRaises(InputError, msg=u):
                parse_video_url(u, H)

    def test_extra_host_allowed_only_when_configured(self):
        with self.assertRaises(InputError):
            parse_video_url("https://example.com/v/1", H)
        url, vid = parse_video_url("https://example.com/v/1", H + ("example.com",))
        self.assertTrue(vid.startswith("x"))


class PathTests(unittest.TestCase):
    def test_traversal_blocked(self):
        with tempfile.TemporaryDirectory() as d:
            base = Path(d)
            for bad in ("../x.mp4", "..\\x.mp4", "/etc/passwd", "a/b.mp4", ".hidden", "", "x" * 400):
                with self.assertRaises(InputError, msg=bad):
                    safe_child(base, bad)
            self.assertEqual(safe_child(base, "ok_1.mp4").parent, base.resolve())

    def test_valid_id(self):
        self.assertEqual(valid_id("abc_DEF-123"), "abc_DEF-123")
        with self.assertRaises(InputError):
            valid_id("../etc")


class ChannelUrlTests(unittest.TestCase):
    """Tautan kanal berasal dari data video (pemilik kanal mengendalikannya): hanya https ke YouTube yang lolos."""

    def test_real_channel_urls_pass(self):
        for u in ("https://www.youtube.com/@nama", "https://youtube.com/channel/UCabcdefghijklmnopqrstuv",
                  "https://m.youtube.com/@nama", "https://music.youtube.com/channel/UCx", "https://WWW.YouTube.com/c/Nama"):
            self.assertEqual(safe_channel_url(u), u, u)

    def test_surrounding_whitespace_is_trimmed(self):
        self.assertEqual(safe_channel_url("  https://www.youtube.com/@a "), "https://www.youtube.com/@a")

    def test_dangerous_or_foreign_urls_are_dropped(self):
        bad = ("javascript:alert(1)", "data:text/html,<script>1</script>", "http://www.youtube.com/@a",
               "//www.youtube.com/@a", "https://evil.com/@a", "https://youtube.com.evil.com/@a",
               "https://www.youtube.com.evil.com/@a", "https://evil.com/?u=https://www.youtube.com/@a",
               "https://user@www.youtube.com/@a", "https://user:pw@www.youtube.com/@a",
               "https://www.youtube.com:8443/@a", "https://www.youtube.com/@a b", "https://www.youtube.com/@a\nX",
               "https://www.youtube.com/@a\x00", "ftp://www.youtube.com/@a", "www.youtube.com/@a", "https://", "")
        for u in bad:
            self.assertEqual(safe_channel_url(u), "", repr(u))

    def test_non_strings_and_oversized_values(self):
        for v in (None, 5, [], {"a": 1}, b"https://www.youtube.com/@a"):
            self.assertEqual(safe_channel_url(v), "", repr(v))
        self.assertEqual(safe_channel_url("https://www.youtube.com/@" + "a" * 400), "")


class MiscTests(unittest.TestCase):
    def test_token(self):
        self.assertTrue(token_ok("", None))
        self.assertTrue(token_ok("s3cret", "s3cret"))
        self.assertFalse(token_ok("s3cret", "nope"))
        self.assertFalse(token_ok("s3cret", None))

    def test_redact(self):
        key = "AIzaSyA1234567890abcdefghijklmnopqrstu"
        out = redact(f"error calling with {key} failed", key)
        self.assertNotIn(key, out)
        self.assertNotIn("AIzaSyA1234", redact("AIzaSyA1234567890abcdefghijklmnop"))

    def test_cookies(self):
        good = "# Netscape HTTP Cookie File\n.youtube.com\tTRUE\t/\tTRUE\t0\tSID\tabc\n#HttpOnly_.google.com\tTRUE\t/\tTRUE\t0\tX\ty\n"
        info = validate_netscape_cookies(good)
        self.assertEqual(info["count"], 2)
        self.assertIn("youtube.com", info["domains"])
        self.assertNotIn("abc", str(info))     # nilai cookie tidak bocor
        for bad in ("hello world", "# only comment\n", "a\tb\tc\n"):
            with self.assertRaises(InputError):
                validate_netscape_cookies(bad)


if __name__ == "__main__":
    unittest.main()

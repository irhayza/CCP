import json, unittest
from backend import gemini
from backend.captions import Word
from backend.gemini import GeminiError, generate_json, plan_windows, rank_models
from backend.transcript import make_segments, parse_json3, prompt_lines, words_in_range


AUTO = {"events": [
    {"tStartMs": 1000, "dDurationMs": 3000, "segs": [{"utf8": "halo"}, {"utf8": " semua", "tOffsetMs": 500},
                                                       {"utf8": " apa", "tOffsetMs": 900}, {"utf8": " kabar", "tOffsetMs": 1400}]},
    {"tStartMs": 3900, "dDurationMs": 100, "segs": [{"utf8": "\n"}]},
    {"tStartMs": 4000, "dDurationMs": 2000, "segs": [{"utf8": "hari ini kita bahas."}]},
]}


class TranscriptTests(unittest.TestCase):
    def test_auto_caption_word_timing(self):
        w = parse_json3(AUTO)
        self.assertEqual([x.text for x in w], ["halo", "semua", "apa", "kabar", "hari", "ini", "kita", "bahas."])
        self.assertAlmostEqual(w[1].start, 1.5)
        self.assertAlmostEqual(w[0].end, 1.5)
        self.assertTrue(all(b.start >= a.start for a, b in zip(w, w[1:])))
        self.assertTrue(all(x.end > x.start for x in w))

    def test_manual_subs_spread_evenly(self):
        w = parse_json3({"events": [{"tStartMs": 0, "dDurationMs": 4000, "segs": [{"utf8": "satu dua tiga empat"}]}]})
        self.assertEqual([round(x.start, 1) for x in w], [0.0, 1.0, 2.0, 3.0])

    def test_dedupes_rolling_duplicates(self):
        ev = {"tStartMs": 0, "dDurationMs": 1000, "segs": [{"utf8": "ulang"}]}
        self.assertEqual(len(parse_json3({"events": [ev, dict(ev)]})), 1)

    def test_range_shifts_to_zero(self):
        w = [Word("a", 9, 10), Word("b", 10, 11), Word("c", 12, 13), Word("d", 20, 21)]
        r = words_in_range(w, 10, 13)
        self.assertEqual([(x.text, x.start, x.end) for x in r], [("b", 0, 1), ("c", 2, 3)])

    def test_segments_split_on_gap_punct_and_length(self):
        w = [Word("a", 0, .3), Word("b", .4, .7), Word("c", 3.0, 3.3), Word("d.", 3.4, 3.7), Word("e", 3.8, 4)]
        self.assertEqual([s.text for s in make_segments(w)], ["a b", "c d.", "e"])
        long = [Word(str(i), i * .5, i * .5 + .3) for i in range(60)]
        self.assertTrue(all(s.end - s.start <= 13 for s in make_segments(long)))

    def test_prompt_lines_window(self):
        segs = make_segments(parse_json3(AUTO))
        self.assertIn("[1s | 00:01]", prompt_lines(segs, 0, 10))
        self.assertEqual(prompt_lines(segs, 50, 60), "")


class FakeHttp:
    """Respons berurutan per model; mencatat urutan panggilan & header key tidak pernah ada di URL."""
    def __init__(self, plan):
        self.plan, self.calls = plan, []

    def __call__(self, method, url, key, body, timeout):
        self.calls.append(url)
        assert key not in url
        model = url.split("/models/")[-1].split(":")[0]
        step = self.plan[model].pop(0) if isinstance(self.plan[model], list) else self.plan[model]
        if isinstance(step, GeminiError):
            raise step
        return {"candidates": [{"content": {"parts": [{"text": step}]}}]}


GOOD = json.dumps([{"start": 10, "end": 40, "score": 88, "title": "t", "hook": "h", "reason": "r"}])


class GeminiTests(unittest.TestCase):
    def test_model_ranking_is_dynamic(self):
        names = ["models/gemini-1.5-flash", "models/gemini-2.5-flash-lite", "models/gemini-2.5-flash",
                 "models/gemini-3.0-flash", "models/gemini-2.5-flash-preview-09-2025", "models/gemini-2.5-pro",
                 "models/gemini-2.5-flash-image", "models/gemini-2.0-flash", "models/gemini-flash-latest", "models/gemma-3-27b"]
        self.assertEqual(rank_models(names), ["gemini-3.0-flash", "gemini-2.5-flash", "gemini-2.5-flash-lite",
                                              "gemini-2.0-flash", "gemini-1.5-flash", "gemini-flash-latest"])
        self.assertEqual(rank_models([], ("my-model",)), ["my-model"])
        self.assertTrue(rank_models([]))

    def test_list_models_filters_and_survives_failure(self):
        ok = lambda *a: {"models": [{"name": "models/gemini-2.5-flash", "supportedGenerationMethods": ["generateContent"]},
                                    {"name": "models/gemini-2.0-flash", "supportedGenerationMethods": ["embedContent"]}]}
        self.assertEqual(gemini.list_models("k" * 20, http=ok), ["gemini-2.5-flash"])

        def down(*a):
            raise GeminiError("network", "x")
        self.assertTrue(gemini.list_models("k" * 20, http=down))      # jatuh ke daftar statis

        def bad(*a):
            raise GeminiError("auth", "x")
        with self.assertRaises(GeminiError):
            gemini.list_models("k" * 20, http=bad)

    def test_fallback_on_quota_then_success(self):
        h = FakeHttp({"m1": GeminiError("quota", "q"), "m2": GOOD})
        data, used, log = generate_json("K" * 20, ["m1", "m2"], "p", http=h, sleep=lambda s: None)
        self.assertEqual(used, "m2")
        self.assertEqual(data[0]["score"], 88)
        self.assertIn("m1: quota", log)

    def test_unavailable_retries_once_then_moves_on(self):
        h = FakeHttp({"m1": [GeminiError("unavailable", "u"), GeminiError("unavailable", "u")], "m2": GOOD})
        slept = []
        _, used, _ = generate_json("K" * 20, ["m1", "m2"], "p", http=h, sleep=slept.append)
        self.assertEqual((used, len(slept), len(h.calls)), ("m2", 1, 3))

    def test_auth_error_stops_immediately(self):
        h = FakeHttp({"m1": GeminiError("auth", "bad"), "m2": GOOD})
        with self.assertRaises(GeminiError) as cm:
            generate_json("K" * 20, ["m1", "m2"], "p", http=h)
        self.assertEqual(cm.exception.kind, "auth")
        self.assertEqual(len(h.calls), 1)

    def test_all_models_fail_raises_last(self):
        h = FakeHttp({"m1": GeminiError("quota", "q"), "m2": GeminiError("notfound", "n")})
        with self.assertRaises(GeminiError) as cm:
            generate_json("K" * 20, ["m1", "m2"], "p", http=h)
        self.assertEqual(cm.exception.kind, "notfound")

    def test_tolerates_fenced_and_broken_json(self):
        h = FakeHttp({"m1": "```json\n" + GOOD + "\n```"})
        self.assertEqual(generate_json("K" * 20, ["m1"], "p", http=h)[0][0]["end"], 40)
        h = FakeHttp({"m1": "{not json", "m2": GOOD})
        self.assertEqual(generate_json("K" * 20, ["m1", "m2"], "p", http=h)[1], "m2")
        h = FakeHttp({"m1": '{"a":1}', "m2": GOOD})          # bukan array -> pindah model
        self.assertEqual(generate_json("K" * 20, ["m1", "m2"], "p", http=h)[1], "m2")

    def test_long_videos_are_windowed(self):
        self.assertEqual(len(plan_windows(900)), 1)
        w = plan_windows(3 * 3600)
        self.assertLessEqual(len(w), 9)
        self.assertEqual(w[0][0], 0)
        self.assertGreaterEqual(w[-1][1], 3 * 3600 - 1)

    def test_find_candidates_sends_peaks_and_prefers_working_model(self):
        segs = make_segments(parse_json3(AUTO))
        h = FakeHttp({"m1": GeminiError("quota", "q"), "m2": [GOOD]})
        out, used, log = gemini.find_candidates("K" * 20, segs, 600, "Judul", window=(20, 45, 30), max_clips=3,
                                                language="id", focus="fokus lucu", peaks=[2.0], models=["m1", "m2"],
                                                http=h, sleep=lambda s: None)
        self.assertEqual((len(out), used), (1, ["m2"]))


if __name__ == "__main__":
    unittest.main()

import unittest
from backend.captions import (CaptionStyle, TitleSpec, Word, ass_color, ass_time, build_ass, build_srt, chunk_words,
                              clean_words, sanitize_text, PRESETS)


def W(txt, s, e=None):
    return Word(txt, s, s + 0.3 if e is None else e)


class SanitizeTests(unittest.TestCase):
    def test_removes_noise_keeps_expressive_punctuation(self):
        self.assertEqual(sanitize_text("[LAUGHTER] Hello (cheering) world ♪ la la ♪"), "Hello world")
        self.assertEqual(sanitize_text("Untung 100% & $50?!"), "Untung 100% & $50?!")
        self.assertEqual(sanitize_text("{\\an8}hack\\N{evil}"), "hack")
        self.assertEqual(sanitize_text("&gt;&gt; speaker: hi"), "speaker: hi")
        self.assertEqual(sanitize_text("😀 ok 🔥"), "ok")
        self.assertEqual(sanitize_text("don’t"), "don't")

    def test_multi_token_tags(self):
        ws = [W("halo", 0), W("[", 0.3), W("Music", 0.6), W("]", 0.9), W("dunia", 1.2), W("(applause", 1.5),
              W("keras", 1.8), W("sekali)", 2.1), W("lagi", 2.4)]
        self.assertEqual([w.text for w in clean_words(ws)], ["halo", "dunia", "lagi"])

    def test_token_splits_share_time(self):
        out = clean_words([Word("a{x}b", 1.0, 2.0)])
        self.assertEqual([w.text for w in out], ["a", "b"])
        self.assertLess(out[0].end, out[1].end + 1e-9)


class ChunkTests(unittest.TestCase):
    def test_limits_and_pauses(self):
        ws = [W(f"w{i}", i * 0.4) for i in range(7)]
        self.assertEqual([len(c) for c in chunk_words(ws, 3)], [3, 3, 1])
        gap = [W("a", 0), W("b", 0.4), W("c", 3.0)]
        self.assertEqual([len(c) for c in chunk_words(gap, 3)], [2, 1])

    def test_breaks_after_sentence_end(self):
        ws = [W("Selesai.", 0), W("Lanjut", 0.4), W("lagi", 0.8)]
        self.assertEqual([[w.text for w in c] for c in chunk_words(ws, 3)], [["Selesai."], ["Lanjut", "lagi"]])


class AssTests(unittest.TestCase):
    def test_time_and_color(self):
        self.assertEqual(ass_time(0), "0:00:00.00")
        self.assertEqual(ass_time(61.999), "0:01:02.00")
        self.assertEqual(ass_time(3600.5), "1:00:00.50")
        self.assertEqual(ass_color("#FFD60A"), "&H000AD6FF")      # RGB -> BGR

    def test_one_event_per_word_with_active_highlight(self):
        ws = [W("satu", 0.5), W("dua", 0.9), W("tiga", 1.3)]
        ass = build_ass(ws, 5.0, CaptionStyle(preset="viral_pop"))
        ev = [l for l in ass.splitlines() if l.startswith("Dialogue: 1,")]
        self.assertEqual(len(ev), 3)
        self.assertIn("0:00:00.50", ev[0])
        self.assertIn("\\1c&H4DE1FF&", ev[0])                    # kuning #FFE14D dalam BGR
        self.assertIn("SATU", ev[0])
        self.assertIn("\\pos(540,1344)", ev[0])
        for a, b in zip(ev, ev[1:]):                              # event berurutan tanpa tumpang tindih
            end_a = a.split(",")[2]
            start_b = b.split(",")[1]
            self.assertLessEqual(end_a, start_b)

    def test_multi_chunk_zero_overlap(self):
        # 12 kata berurutan terbagi jadi 4 chunk — buktikan tidak ada tumpang tindih antar-chunk
        ws = [W(f"kata{i}", i * 0.35, i * 0.35 + 0.25) for i in range(12)]
        ass = build_ass(ws, 10.0, CaptionStyle(words_per_chunk=3))
        ev = [l for l in ass.splitlines() if l.startswith("Dialogue: 1,")]
        self.assertEqual(len(ev), 12)
        for a, b in zip(ev, ev[1:]):
            end_a = a.split(",")[2]
            start_b = b.split(",")[1]
            self.assertLessEqual(end_a, start_b, f"Tumpang tindih: {a} dan {b}")

    def test_title_and_clip_bounds(self):
        ass = build_ass([W("lewat", 9.0)], 5.0, title=TitleSpec("Judul {bad} keren", duration=2))
        self.assertIn("Style: Title", ass)
        self.assertNotIn("{bad}", ass)
        self.assertEqual([l for l in ass.splitlines() if l.startswith("Dialogue: 1")], [])  # kata di luar klip dibuang
        title = [l for l in ass.splitlines() if ",Title," in l and l.startswith("Dialogue")][0]
        self.assertIn("0:00:02.00", title)

    def test_font_injection_neutralised(self):
        ass = build_ass([W("a", 0)], 3, CaptionStyle(font="Evil,Font\nStyle: X"))
        self.assertEqual(ass.count("\nStyle: Cap,"), 1)
        self.assertNotIn("Evil,Font", ass)

    def test_all_presets_build(self):
        for name in PRESETS:
            self.assertIn("[Events]", build_ass([W("a", 0), W("b", 0.4)], 3, CaptionStyle(preset=name)))

    def test_srt(self):
        srt = build_srt([W("halo", 0.0), W("dunia", 0.4)])
        self.assertIn("00:00:00,000 --> 00:00:00,", srt)
        self.assertIn("halo dunia", srt)


if __name__ == "__main__":
    unittest.main()

"""Engine caption: pembersihan teks, pengelompokan kata, dan generator ASS (karaoke per kata).

Pendekatan: setiap kata yang sedang diucapkan mendapat satu event Dialogue sendiri yang
menampilkan seluruh chunk dengan kata aktif diwarnai & "pop". Cara ini tidak bergantung pada
semantik tag \\k sehingga hasilnya konsisten di semua versi libass."""
from __future__ import annotations

import html
import re
import unicodedata
from dataclasses import dataclass

PLAY_W, PLAY_H = 1080, 1920
FONTS = ("Outfit", "Montserrat", "Poppins", "Roboto", "Inter", "Bebas Neue", "Anton", "Impact")


@dataclass
class Word:
    text: str
    start: float
    end: float


@dataclass(frozen=True)
class Preset:
    label: str
    inactive: str      # warna kata yang belum/sudah lewat
    active: str        # warna kata yang sedang diucapkan
    outline: str
    outline_w: int = 8
    shadow: int = 2
    uppercase: bool = True
    pop: int = 115     # skala (%) kata aktif; 100 = tanpa animasi


PRESETS: dict[str, Preset] = {
    "viral_pop": Preset("Viral Pop", "#FFFFFF", "#FFE14D", "#000000", outline_w=10, shadow=3, pop=115),
    "the_hormozi": Preset("The Hormozi", "#FFFFFF", "#FFE600", "#000000", outline_w=10, shadow=3, pop=115),
    "beast_punch": Preset("Beast Punch", "#FFFFFF", "#39FF14", "#000000", outline_w=10, shadow=3, pop=115),
    "alert_news": Preset("Alert News", "#FFE600", "#FF2A2A", "#000000", outline_w=10, shadow=3, pop=115),
    "cyber_violet": Preset("Cyber Violet", "#FFFFFF", "#FF2BD6", "#1A0033", outline_w=9, shadow=2, pop=115),
    "fire_red": Preset("Fire Red", "#FFFFFF", "#FF3B30", "#000000", outline_w=10, shadow=2, pop=115),
    "electric_cyan": Preset("Electric Cyan", "#FFFFFF", "#00FFFF", "#00131A", outline_w=9, shadow=2, pop=115),
    "golden_aura": Preset("Golden Aura", "#FFF4D6", "#FFC83D", "#3B2400", outline_w=9, shadow=2, pop=115),
    "ocean_teal": Preset("Ocean Teal", "#FFFFFF", "#2DE2C4", "#00201B", outline_w=9, shadow=2, pop=115),
    "sunset_orange": Preset("Sunset Orange", "#FFFFFF", "#FF8A3D", "#2A0F00", outline_w=9, shadow=2, pop=115),
    "clean_minimal": Preset("Clean Minimal", "#D0D0D0", "#FFFFFF", "#000000", outline_w=5, shadow=3, uppercase=False, pop=106),
    "classic_outline": Preset("Classic Outline", "#FFFFFF", "#FFFFFF", "#000000", outline_w=8, pop=100),
}


@dataclass
class CaptionStyle:
    preset: str = "viral_pop"
    font: str = "Montserrat"
    size: int = 84
    y_frac: float = 0.70            # posisi tengah teks dari atas (0..1) — Safe Zone 60%-70%
    words_per_chunk: int = 3
    uppercase: bool | None = None   # None = ikut preset


@dataclass
class TitleSpec:
    text: str
    y_frac: float = 0.12
    duration: float | None = None   # None = sepanjang klip
    size: int = 66


# --------------------------------------------------------------------------- sanitasi
_TAG = re.compile(r"\[[^\]]*\]|\([^)]*\)|\{[^}]*\}|<[^>]*>|♪[^♪]*♪|♪")
_KEEP_PUNCT = set(".,%&$?!':-/+@#")
_OPEN_CLOSE = (("[", "]"), ("(", ")"), ("♪", "♪"))


def sanitize_text(s: str) -> str:
    """Buang tag non-ucapan ([MUSIC], (cheering), ♪) dan simbol; pertahankan huruf, angka, dan . , % & $ ? ! '"""
    s = html.unescape(s or "")
    s = unicodedata.normalize("NFKC", s).replace("’", "'").replace("‘", "'")
    s = _TAG.sub(" ", s)
    s = re.sub(r"\\[NnhT]", " ", s)          # escape ASS (\N, \n, \h) jangan sampai tersisa huruf
    out = []
    for ch in s:
        if ch.isalnum() or ch in _KEEP_PUNCT:
            out.append(ch)
        elif ch.isspace():
            out.append(" ")
    s = re.sub(r"\s+", " ", "".join(out)).strip()
    s = re.sub(r"(^|\s)[-:/+@#]+(?=\s|$)", " ", s)
    return re.sub(r"\s+", " ", s).strip()


def clean_words(words: list[Word]) -> list[Word]:
    """Bersihkan daftar kata, termasuk tag yang terpecah di beberapa token ("[", "Music", "]")."""
    out: list[Word] = []
    closer: str | None = None
    budget = 0
    for w in words:
        t = w.text or ""
        if closer:
            budget -= 1
            if closer in t or budget <= 0:
                closer = None
            continue
        for op, cl in _OPEN_CLOSE:
            i = t.find(op)
            if i != -1 and cl not in t[i + 1:]:
                closer, budget = cl, 8
                t = t[:i]
                break
        clean = sanitize_text(t)
        if not clean:
            continue
        parts = clean.split(" ")
        if len(parts) == 1:
            out.append(Word(clean, w.start, w.end))
        else:  # satu token terpecah jadi beberapa kata -> bagi waktu rata
            step = max(w.end - w.start, 0.01 * len(parts)) / len(parts)
            for k, p in enumerate(parts):
                out.append(Word(p, w.start + k * step, w.start + (k + 1) * step))
    return out


# --------------------------------------------------------------------------- chunking
def chunk_words(words: list[Word], max_words: int = 3, max_chars: int = 24, pause: float = 0.5) -> list[list[Word]]:
    chunks: list[list[Word]] = []
    cur: list[Word] = []
    for i, w in enumerate(words):
        if cur:
            gap = w.start - cur[-1].end
            chars = sum(len(x.text) + 1 for x in cur) + len(w.text)
            if len(cur) >= max_words or gap >= pause or chars > max_chars:
                chunks.append(cur)
                cur = []
        cur.append(w)
        if w.text[-1:] in ".?!" and len(cur) >= 1 and i + 1 < len(words):
            chunks.append(cur)
            cur = []
    if cur:
        chunks.append(cur)
    return chunks


def _line_breaks(texts: list[str], max_chars: int) -> list[int]:
    """Indeks baris per kata (greedy), maksimal ~2 baris untuk chunk pendek."""
    lines, line, count = [], 0, 0
    for t in texts:
        add = len(t) + (1 if count else 0)
        if count and count + add > max_chars:
            line += 1
            count = 0
            add = len(t)
        count += add
        lines.append(line)
    return lines


# --------------------------------------------------------------------------- ASS
def ass_color(hex_rgb: str, alpha: int = 0) -> str:
    h = hex_rgb.lstrip("#")
    r, g, b = int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)
    return f"&H{alpha:02X}{b:02X}{g:02X}{r:02X}"


def _bgr(hex_rgb: str) -> str:
    return ass_color(hex_rgb)[4:]  # BBGGRR tanpa alpha


def ass_time(t: float) -> str:
    cs = max(0, int(round(t * 100)))
    h, rem = divmod(cs, 360_000)
    m, rem = divmod(rem, 6000)
    s, c = divmod(rem, 100)
    return f"{h}:{m:02d}:{s:02d}.{c:02d}"


def _font(name: str) -> str:
    clean = re.sub(r"[^A-Za-z0-9 _-]", "", name or "")[:40].strip()
    return clean or "Montserrat"


def _max_line_chars(size: int, upper: bool) -> int:
    avail = PLAY_W - 240  # Safe Zone: Margin minimal 120px kiri dan kanan
    return max(8, int(avail / (size * (0.64 if upper else 0.56))))


def build_ass(words: list[Word], clip_duration: float, style: CaptionStyle | None = None,
              title: TitleSpec | None = None) -> str:
    style = style or CaptionStyle()
    preset = PRESETS.get(style.preset, PRESETS["viral_pop"])
    upper = preset.uppercase if style.uppercase is None else style.uppercase
    size = max(36, min(160, int(style.size)))
    y = int(PLAY_H * max(0.05, min(0.95, style.y_frac)))
    font = _font(style.font)

    head = [
        "[Script Info]", "ScriptType: v4.00+", f"PlayResX: {PLAY_W}", f"PlayResY: {PLAY_H}",
        "WrapStyle: 2", "ScaledBorderAndShadow: yes", "",
        "[V4+ Styles]",
        "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, "
        "Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, "
        "Alignment, MarginL, MarginR, MarginV, Encoding",
        f"Style: Cap,{font},{size},{ass_color(preset.inactive)},{ass_color(preset.active)},"
        f"{ass_color(preset.outline)},{ass_color('#000000', 0x70)},-1,0,0,0,100,100,0,0,1,"
        f"{preset.outline_w},{preset.shadow},5,120,120,0,1",
        f"Style: Title,{font},{title.size if title else 66},{ass_color('#FFFFFF')},{ass_color('#FFFFFF')},"
        f"{ass_color('#101418', 0x20)},{ass_color('#000000', 0x80)},-1,0,0,0,100,100,0,0,3,18,0,5,70,70,0,1",
        "", "[Events]", "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text",
    ]
    events: list[str] = []

    if title and sanitize_text(title.text):
        t_text = sanitize_text(title.text)
        t_text = t_text.upper() if upper else t_text
        lines = _line_breaks(t_text.split(" "), _max_line_chars(title.size, upper))
        words_t = t_text.split(" ")
        rendered = ""
        for i, wt in enumerate(words_t):
            if i and lines[i] != lines[i - 1]:
                rendered += "\\N"
            elif i:
                rendered += " "
            rendered += wt
        t_end = clip_duration if title.duration is None else min(clip_duration, max(0.5, title.duration))
        ty = int(PLAY_H * max(0.04, min(0.9, title.y_frac)))
        events.append(f"Dialogue: 0,{ass_time(0)},{ass_time(t_end)},Title,,0,0,0,,"
                      f"{{\\an5\\pos({PLAY_W // 2},{ty})\\fad(250,250)}}{rendered}")

    raw_words = [w for w in clean_words(words) if w.start < clip_duration]
    # Sanitasi urutan waktu: pastikan start bertambah secara monoton
    valid_words: list[Word] = []
    last_s = 0.0
    for w in raw_words:
        if not w.text.strip():
            continue
        s = max(0.0, w.start)
        e = max(s + 0.05, w.end)
        if s < last_s:
            s = last_s
            e = max(s + 0.05, e)
        valid_words.append(Word(w.text.strip(), s, e))
        last_s = s

    max_chars = _max_line_chars(size, upper)
    inactive_c, active_c = _bgr(preset.inactive), _bgr(preset.active)
    chunks = chunk_words(valid_words, max(1, style.words_per_chunk), max_chars=max_chars)
    last_event_end = 0.0

    for c_idx, chunk in enumerate(chunks):
        if not chunk:
            continue

        # Waktu mulai chunk: tidak boleh mendahului akhir event sebelumnya
        c_start = max(chunk[0].start, last_event_end)

        # Waktu mulai chunk berikutnya (lookahead)
        if c_idx + 1 < len(chunks):
            raw_next_start = chunks[c_idx + 1][0].start
        else:
            raw_next_start = clip_duration

        # Jaga agar chunk berikutnya tidak tergeser sebelum durasi minimal chunk ini
        next_start = max(c_start + 0.05 * len(chunk), raw_next_start)

        # Waktu selesai chunk: bertahan sejenak saat jeda hening, namun TIDAK PERNAH melebihi next_start
        last_w = chunk[-1]
        c_end = min(next_start, max(last_w.end, last_w.end + 0.25))
        if c_end <= c_start:
            c_end = min(next_start, c_start + 0.08)
        c_end = min(c_end, clip_duration)

        # Bersihkan tanda baca di akhir kata (titik dan koma dihilangkan agar visual rapi, simpan ? dan !)
        display_texts: list[str] = []
        for w in chunk:
            t = w.text.strip()
            if len(t) > 1 and t.endswith((".", ",")):
                t = t[:-1]
            display_texts.append(t.upper() if upper else t)

        lines = _line_breaks(display_texts, max_chars)

        prev_w_end = c_start
        for i, w in enumerate(chunk):
            w_start = prev_w_end
            if i + 1 < len(chunk):
                w_end = max(w_start + 0.05, min(chunk[i + 1].start, c_end))
            else:
                w_end = c_end

            w_start = min(w_start, clip_duration)
            w_end = min(max(w_end, w_start + 0.05), clip_duration)
            if w_start >= clip_duration:
                break

            parts = []
            for j, t in enumerate(display_texts):
                if j and lines[j] != lines[j - 1]:
                    parts.append("\\N")
                elif j:
                    parts.append(" ")
                if j == i:
                    pop = ""
                    if preset.pop > 100:
                        pop = f"\\t(0,90,\\fscx{preset.pop}\\fscy{preset.pop})"
                    parts.append(f"{{\\1c&H{active_c}&\\fscx100\\fscy100{pop}}}{t}"
                                 f"{{\\1c&H{inactive_c}&\\fscx100\\fscy100}}")
                else:
                    parts.append(t)

            events.append(f"Dialogue: 1,{ass_time(w_start)},{ass_time(w_end)},Cap,,0,0,0,,"
                          f"{{\\an5\\pos({PLAY_W // 2},{y})}}{''.join(parts)}")
            prev_w_end = w_end
            last_event_end = w_end

    return "\n".join(head + events) + "\n"


# --------------------------------------------------------------------------- SRT
def _srt_time(t: float) -> str:
    ms = max(0, int(round(t * 1000)))
    h, rem = divmod(ms, 3_600_000)
    m, rem = divmod(rem, 60_000)
    s, ms = divmod(rem, 1000)
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"


def build_srt(words: list[Word], words_per_chunk: int = 6) -> str:
    out = []
    clean = clean_words(words)
    chunks = chunk_words(clean, words_per_chunk, max_chars=42, pause=0.9)
    last_end = 0.0
    for n, chunk in enumerate(chunks, 1):
        s = max(chunk[0].start, last_end)
        e = max(s + 0.1, chunk[-1].end)
        if n < len(chunks):
            next_s = chunks[n][0].start
            e = min(e, next_s)
        out.append(f"{n}\n{_srt_time(s)} --> {_srt_time(e)}\n"
                   f"{' '.join(w.text for w in chunk)}\n")
        last_end = e
    return "\n".join(out)

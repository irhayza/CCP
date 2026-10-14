"""Pembangun filtergraph ffmpeg untuk layout 9:16. Fungsi murni -> mudah diuji tanpa menjalankan ffmpeg."""
from __future__ import annotations

from dataclasses import dataclass, field

from .facetrack import x_expression

MODES = ("fill_face", "fill_center", "letterbox", "split", "pip", "duo")
# Pusat crop (fraksi lebar frame) tiap panel layout `duo` bila jalur wajah tidak tersedia.
DUO_DEFAULT_CENTERS = (0.28, 0.72)
ASPECTS = {"16:9": 16 / 9, "4:3": 4 / 3, "1:1": 1.0}
CORNERS = ("tl", "tr", "bl", "br")


@dataclass
class LayoutSpec:
    mode: str = "fill_face"
    aspect: str = "16:9"          # untuk letterbox: 16:9 | 4:3 | 1:1 | auto
    backdrop: str = "blur"        # blur | black
    cam: tuple[float, float, float, float] = (0.0, 0.0, 0.30, 0.38)   # x, y, w, h (0..1) kamera streamer
    game_fit: str = "cover"       # cover | contain  (bagian bawah pada mode split)
    pip_corner: str = "tr"
    pip_scale: float = 0.36
    seam_caption: bool = True     # mode duo: caption ditaruh di sambungan kedua panel (dipakai renderer, bukan filtergraph)
    face_keys: list[tuple[float, float]] | None = field(default=None, repr=False)     # fill_face; duo: panel atas
    face_keys_b: list[tuple[float, float]] | None = field(default=None, repr=False)   # duo: panel bawah

    def validate(self) -> "LayoutSpec":
        if self.mode not in MODES:
            raise ValueError(f"Mode layout tidak dikenal: {self.mode}")
        if self.aspect != "auto" and self.aspect not in ASPECTS:
            raise ValueError(f"Rasio tidak dikenal: {self.aspect}")
        if self.backdrop not in ("blur", "black"):
            raise ValueError("backdrop harus blur atau black")
        if self.pip_corner not in CORNERS:
            raise ValueError("pip_corner tidak valid")
        if self.game_fit not in ("cover", "contain"):
            raise ValueError("game_fit tidak valid")
        x, y, w, h = self.cam
        if not (0 <= x < 1 and 0 <= y < 1 and 0.05 <= w <= 1 and 0.05 <= h <= 1):
            raise ValueError("Area kamera di luar rentang 0..1")
        self.pip_scale = min(0.6, max(0.18, float(self.pip_scale)))
        return self


def even(n: float) -> int:
    return max(2, int(n) // 2 * 2)


def cover_crop(src_w: int, src_h: int, target_ar: float) -> tuple[int, int]:
    """Ukuran crop terbesar dengan rasio target di dalam frame sumber (genap)."""
    if src_w / src_h > target_ar:
        ch = even(src_h)
        cw = min(even(ch * target_ar), even(src_w))
    else:
        cw = even(src_w)
        ch = min(even(cw / target_ar), even(src_h))
    return cw, ch


def _cam_rect_px(cam: tuple[float, float, float, float], src_w: int, src_h: int) -> tuple[int, int, int, int]:
    x, y, w, h = cam
    px, py = int(x * src_w), int(y * src_h)
    pw = even(min(w * src_w, src_w - px))
    ph = even(min(h * src_h, src_h - py))
    return px, py, max(16, pw), max(16, ph)


def ff_path(p: str) -> str:
    """Escape path untuk opsi filter ffmpeg (aman untuk Windows: C:\\x -> C\\:/x)."""
    s = str(p).replace("\\", "/").replace(":", "\\:").replace("'", "'\\''")
    return f"'{s}'"


def build_filtergraph(spec: LayoutSpec, src_w: int, src_h: int, out_w: int = 1080, out_h: int = 1920,
                      ass_file: str | None = None, fonts_dir: str | None = None,
                      max_fps: int | None = 30, src_fps: float = 30.0) -> str:
    """Kembalikan string filter_complex yang berakhir di label [vout]."""
    spec.validate()
    out_w, out_h = even(out_w), even(out_h)
    target_ar = out_w / out_h
    chains: list[str] = []

    if spec.mode in ("fill_face", "fill_center"):
        cw, ch = cover_crop(src_w, src_h, target_ar)
        if spec.mode == "fill_face" and spec.face_keys and cw < src_w:
            x = x_expression(spec.face_keys, src_w, cw)
        else:
            x = f"(iw-{cw})/2"
        chains.append(f"[0:v]crop=w={cw}:h={ch}:x='{x}':y='(ih-{ch})/2',"
                      f"scale={out_w}:{out_h}:flags=lanczos,setsar=1[v0]")

    elif spec.mode == "letterbox":
        src_ar = src_w / src_h
        ar = src_ar if spec.aspect == "auto" else ASPECTS[spec.aspect]
        fg_h = min(out_h, even(out_w / ar))
        ccw, cch = cover_crop(src_w, src_h, ar)
        fg = (f"crop=w={ccw}:h={cch}:x='(iw-{ccw})/2':y='(ih-{cch})/2',"
              f"scale={out_w}:{fg_h}:flags=lanczos,setsar=1")
        if spec.backdrop == "blur":
            bw, bh = cover_crop(src_w, src_h, target_ar)
            chains.append("[0:v]split=2[a][b]")
            chains.append(f"[a]crop=w={bw}:h={bh}:x='(iw-{bw})/2':y='(ih-{bh})/2',"
                          f"scale={even(out_w / 6)}:{even(out_h / 6)},boxblur=6:2,"
                          f"scale={out_w}:{out_h}:flags=bilinear,eq=brightness=-0.07:saturation=1.15,setsar=1[bg]")
            chains.append(f"[b]{fg}[fg]")
            chains.append("[bg][fg]overlay=x='(W-w)/2':y='(H-h)/2'[v0]")
        else:
            chains.append(f"[0:v]{fg},pad={out_w}:{out_h}:0:'({out_h}-ih)/2':color=black[v0]")

    elif spec.mode == "split":
        top_h = even(out_h / 2)
        bot_h = out_h - top_h
        cx, cy, cw_, ch_ = _cam_rect_px(spec.cam, src_w, src_h)
        sw, sh = cover_crop(cw_, ch_, out_w / top_h)
        sx, sy = cx + (cw_ - sw) // 2, cy + (ch_ - sh) // 2
        chains.append("[0:v]split=2[a][b]")
        chains.append(f"[a]crop=w={sw}:h={sh}:x={sx}:y={sy},scale={out_w}:{top_h}:flags=lanczos,setsar=1[top]")
        if spec.game_fit == "cover":
            gw, gh = cover_crop(src_w, src_h, out_w / bot_h)
            chains.append(f"[b]crop=w={gw}:h={gh}:x='(iw-{gw})/2':y='(ih-{gh})/2',"
                          f"scale={out_w}:{bot_h}:flags=lanczos,setsar=1[bot]")
        else:
            chains.append(f"[b]scale={out_w}:-2:flags=lanczos,pad={out_w}:{bot_h}:0:'({bot_h}-ih)/2':color=black,"
                          f"setsar=1[bot]")
        chains.append("[top][bot]vstack=inputs=2[v0]")

    elif spec.mode == "duo":
        # Dua pembicara ditumpuk: pembicara kiri di panel atas, kanan di bawah. Tiap panel punya crop dan jalur
        # kamera sendiri. Ide dari OpenShorts (MIT): tumpuk dua wajah dan taruh caption di sambungan.
        top_h = even(out_h / 2)
        bot_h = out_h - top_h
        panes = []
        for label, h_px, keys, default_c in (("top", top_h, spec.face_keys, DUO_DEFAULT_CENTERS[0]),
                                             ("bot", bot_h, spec.face_keys_b, DUO_DEFAULT_CENTERS[1])):
            pw, ph = cover_crop(src_w, src_h, out_w / h_px)
            x = x_expression(keys if keys and pw < src_w else [(0.0, default_c), (1.0, default_c)], src_w, pw)
            if pw >= src_w:
                x = "0"
            panes.append(f"crop=w={pw}:h={ph}:x='{x}':y='(ih-{ph})/2',scale={out_w}:{h_px}:flags=lanczos,setsar=1[{label}]")
        chains.append("[0:v]split=2[a][b]")
        chains.append(f"[a]{panes[0]}")
        chains.append(f"[b]{panes[1]}")
        chains.append("[top][bot]vstack=inputs=2[v0]")

    else:  # pip
        bw, bh = cover_crop(src_w, src_h, target_ar)
        cx, cy, cw_, ch_ = _cam_rect_px(spec.cam, src_w, src_h)
        pip_w = even(out_w * spec.pip_scale)
        margin = even(out_w * 0.045)
        chains.append("[0:v]split=2[a][b]")
        chains.append(f"[a]crop=w={bw}:h={bh}:x='(iw-{bw})/2':y='(ih-{bh})/2',"
                      f"scale={out_w}:{out_h}:flags=lanczos,setsar=1[bg]")
        chains.append(f"[b]crop=w={cw_}:h={ch_}:x={cx}:y={cy},scale={pip_w}:-2:flags=lanczos,"
                      f"pad=iw+8:ih+8:4:4:color=white,setsar=1[cam]")
        # zona aman: judul di ~12% atas, caption di ~70% -> pip menghindari keduanya
        top_y = even(out_h * 0.19)
        bot_edge = even(out_h * 0.60)
        pos = {
            "tl": (f"{margin}", f"{top_y}"),
            "tr": (f"W-w-{margin}", f"{top_y}"),
            "bl": (f"{margin}", f"{bot_edge}-h"),
            "br": (f"W-w-{margin}", f"{bot_edge}-h"),
        }[spec.pip_corner]
        chains.append(f"[bg][cam]overlay=x='{pos[0]}':y='{pos[1]}'[v0]")

    tail = []
    if ass_file:
        opt = f"ass=filename={ff_path(ass_file)}"
        if fonts_dir:
            opt += f":fontsdir={ff_path(fonts_dir)}"
        tail.append(opt)
    if max_fps and src_fps > max_fps + 0.5:
        tail.append(f"fps={max_fps}")
    tail.append("format=yuv420p")
    chains.append(f"[v0]{','.join(tail)}[vout]")
    return ";".join(chains)

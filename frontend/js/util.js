// Fungsi murni tanpa DOM: bisa diuji di Node.

export const clamp = (v, lo, hi) => Math.min(hi, Math.max(lo, v));

export function fmtTime(sec) {
  sec = Math.max(0, Math.floor(Number(sec) || 0));
  const h = Math.floor(sec / 3600), m = Math.floor((sec % 3600) / 60), s = sec % 60;
  const mm = String(m).padStart(h ? 2 : 1, '0'), ss = String(s).padStart(2, '0');
  return h ? `${h}:${mm}:${ss}` : `${mm}:${ss}`;
}

export function fmtBytes(n) {
  n = Number(n) || 0;
  const u = ['B', 'KB', 'MB', 'GB', 'TB'];
  let i = 0;
  while (n >= 1024 && i < u.length - 1) { n /= 1024; i++; }
  return `${i === 0 ? Math.round(n) : n.toFixed(1)} ${u[i]}`;
}

const ESC = { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' };
export const esc = (s) => String(s ?? '').replace(/[&<>"']/g, (c) => ESC[c]);

export function debounce(fn, ms) {
  let t;
  return (...a) => { clearTimeout(t); t = setTimeout(() => fn(...a), ms); };
}

/** Skala tick penggaris waktu: pilih langkah "bulat" agar ~target tick muat di lebar strip. */
export function rulerTicks(duration, target = 8) {
  if (!(duration > 0)) return [];
  const steps = [5, 10, 15, 30, 60, 120, 300, 600, 900, 1800, 3600, 7200];
  const step = steps.find((s) => duration / s <= target) ?? 7200;
  const out = [];
  for (let t = 0; t <= duration; t += step) out.push({ t, pct: (t / duration) * 100 });
  return out;
}

/** Posisi kiri (%) & lebar (%) sebuah rentang waktu pada strip. */
export function spanPct(start, end, duration) {
  const l = clamp(start / duration, 0, 1), r = clamp(end / duration, 0, 1);
  return { left: l * 100, width: Math.max(0.35, (r - l) * 100) };
}

/** Area kamera streamer (x,y,w,h ternormalisasi) dari pojok + ukuran lebar. */
export function camRect(corner, size) {
  const w = clamp(size, 0.12, 0.6), h = clamp(size * 1.15, 0.12, 0.7);
  const x = corner.endsWith('l') ? 0 : 1 - w, y = corner.startsWith('t') ? 0 : 1 - h;
  return [round(x, 4), round(y, 4), round(w, 4), round(h, 4)];
}

const round = (v, d) => Math.round(v * 10 ** d) / 10 ** d;

/**
 * Sub-area sumber (fraksi 0..1) yang tampak bila `rect` ditampilkan "cover" pada wadah ber-rasio `boxAR`.
 * srcAR = lebar/tinggi frame sumber. Cerminan logika crop di backend (layouts.cover_crop).
 */
export function visibleRect(rect, srcAR, boxAR) {
  const [x, y, w, h] = rect;
  const rectAR = (w * srcAR) / h;            // rasio piksel area tsb
  let vw = w, vh = h;
  if (rectAR > boxAR) vw = (h * boxAR) / srcAR;  // terlalu lebar -> potong kiri/kanan
  else vh = (w * srcAR) / boxAR;                  // terlalu tinggi -> potong atas/bawah
  return [x + (w - vw) / 2, y + (h - vh) / 2, vw, vh];
}

/** Geser jendela crop agar berpusat di `cx` (0..1) tanpa keluar frame. Dipakai preview face-tracking. */
export function faceWindow(cx, srcAR, boxAR) {
  const [, , vw] = visibleRect([0, 0, 1, 1], srcAR, boxAR);
  const left = clamp(cx - vw / 2, 0, 1 - vw);
  return [left, 0, vw, 1];
}

/** Style CSS (string) untuk <img> absolut agar hanya sub-area `vr` yang terlihat di wadahnya. */
export function imgStyleFor(vr) {
  const [vx, vy, vw, vh] = vr;
  return `width:${round(100 / vw, 3)}%;height:${round(100 / vh, 3)}%;left:${round((-vx / vw) * 100, 3)}%;` +
         `top:${round((-vy / vh) * 100, 3)}%;`;
}

export function slug(s) {
  return String(s || '').normalize('NFKD').replace(/[^\w]+/g, '-').replace(/^-+|-+$/g, '').toLowerCase().slice(0, 40);
}

/** Ringkas skor 0..100 menjadi kelas warna: tinggi / sedang / rendah. */
export const scoreTier = (s) => (s >= 80 ? 'hi' : s >= 60 ? 'mid' : 'lo');

export function hashtagText(tags) {
  return (tags || []).map((t) => `#${String(t).replace(/^#/, '')}`).join(' ');
}

/** Bulatkan ke 1 desimal untuk tampilan (waktu klip dari backend bisa 675.95). */
export const fmt1 = (v) => String(Math.round((Number(v) || 0) * 10) / 10);

/**
 * Tentukan "jalur" label agar tanda klip yang berdekatan tidak bertumpuk.
 * spans: [{id,left}] dalam persen; gap: jarak minimum antar-label (persen).
 */
export function assignLanes(spans, gap) {
  const lastLeft = [];
  const out = {};
  for (const s of [...spans].sort((a, b) => a.left - b.left)) {
    let lane = lastLeft.findIndex((l) => s.left - l >= gap);
    if (lane === -1) lane = lastLeft.length;
    lastLeft[lane] = s.left;
    out[s.id] = lane;
  }
  return out;
}

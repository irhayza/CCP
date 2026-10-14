import { t } from './i18n.js';
import { api } from './api.js';
import { camRect, clamp, debounce, duoCenters, esc, faceWindow, fmtTime, imgStyleFor, visibleRect } from './util.js';

export const DEFAULT_STYLE = {
  layout: { mode: 'fill_face', aspect: '16:9', backdrop: 'blur', camCorner: 'tl', camSize: 0.3, game_fit: 'cover', pip_corner: 'tr', pip_scale: 0.36, duo_seam: true },
  captions: { enabled: true, preset: 'viral_pop', font: 'Montserrat', size: 84, y_frac: 0.7, words_per_chunk: 3, uppercase: 'auto' },
  title: { enabled: false, source: 'hook', text: '', y_frac: 0.12, duration: 5, size: 66 },
  quality: 'balanced', resolution: 1080, encoder: 'auto', loudnorm: true, export_srt: false,
};

const ASPECT = { '16:9': 16 / 9, '4:3': 4 / 3, '1:1': 1 };

// Dipakai bila frame asli belum/tidak bisa dimuat, supaya simulasi tata letak tetap terbaca.
const PLACEHOLDER = 'data:image/svg+xml,' + encodeURIComponent(
  '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 1600 900"><defs><linearGradient id="g" x1="0" y1="0" x2="1" y2="1">' +
  '<stop offset="0" stop-color="#2a3947"/><stop offset="1" stop-color="#6f8596"/></linearGradient></defs>' +
  '<rect width="1600" height="900" fill="url(#g)"/><rect y="760" width="1600" height="140" fill="#0f161d" opacity=".5"/>' +
  '<circle cx="800" cy="320" r="96" fill="#d9ad8a"/><path d="M590 900c0-210 95-350 210-350s210 140 210 350z" fill="#34628f"/>' +
  '<circle cx="1330" cy="400" r="68" fill="#c39470"/><path d="M1190 900c0-170 62-290 140-290s140 120 140 290z" fill="#8c4650"/>' +
  '<path d="M0 0h1600M0 300h1600M0 600h1600M533 0v900M1066 0v900" stroke="#fff" stroke-opacity=".08" stroke-width="2"/></svg>');
const PHONE_AR = 9 / 16;

export const mergeStyle = (saved) => {
  const out = JSON.parse(JSON.stringify(DEFAULT_STYLE));
  for (const k of Object.keys(out)) {
    if (saved && typeof out[k] === 'object' && typeof saved[k] === 'object') Object.assign(out[k], saved[k]);
    else if (saved && saved[k] !== undefined && typeof out[k] !== 'object') out[k] = saved[k];
  }
  return out;
};

/** Gaya UI -> payload API (membuang kunci khusus UI, menghitung area kamera). */
export function buildApiStyle(s) {
  const L = s.layout;
  return {
    layout: { mode: L.mode, aspect: L.aspect, backdrop: L.backdrop, cam: camRect(L.camCorner, L.camSize),
              game_fit: L.game_fit, pip_corner: L.pip_corner, pip_scale: Number(L.pip_scale), seam_caption: !!L.duo_seam },
    captions: { enabled: s.captions.enabled, preset: s.captions.preset, font: s.captions.font, size: Number(s.captions.size),
                y_frac: Number(s.captions.y_frac), words_per_chunk: Number(s.captions.words_per_chunk),
                uppercase: s.captions.uppercase === 'auto' ? null : s.captions.uppercase === 'on' },
    title: { enabled: s.title.enabled, text: s.title.source === 'custom' ? s.title.text : '', y_frac: Number(s.title.y_frac),
             duration: Number(s.title.duration) || 0, size: Number(s.title.size) },
    quality: s.quality, resolution: Number(s.resolution), encoder: s.encoder, loudnorm: !!s.loudnorm, export_srt: !!s.export_srt,
  };
}

export function overlayTitleFor(s, clip) {
  if (!s.title.enabled) return '';
  if (s.title.source === 'hook') return clip.hook || clip.title || '';
  if (s.title.source === 'title') return clip.title || '';
  return s.title.text || '';
}

/** HTML lapisan preview 9:16. Meniru crop backend (layouts.py) dengan rumus yang sama. */
export function previewLayers(s, frameUrl, srcAR = 16 / 9, faceCx = 0.5, faces = []) {
  const src = frameUrl || PLACEHOLDER;
  const img = (vr, extra = '') => `<img alt="" src="${esc(src)}" style="${imgStyleFor(vr)}${extra}">`;
  const full = [0, 0, 1, 1];
  const L = s.layout;
  const cam = camRect(L.camCorner, L.camSize);
  const camAR = (cam[2] * srcAR) / cam[3];
  switch (L.mode) {
    case 'fill_center':
      return `<div class="pv" style="inset:0">${img(visibleRect(full, srcAR, PHONE_AR))}</div>`;
    case 'letterbox': {
      const ar = L.aspect === 'auto' ? srcAR : ASPECT[L.aspect] || 16 / 9;
      const h = Math.min(1, PHONE_AR / ar);
      const bg = L.backdrop === 'blur' ? `<div class="pv blur" style="inset:0">${img(visibleRect(full, srcAR, PHONE_AR))}</div>`
                                       : '<div class="pv" style="inset:0"></div>';
      return `${bg}<div class="pv" style="left:0;right:0;top:${((1 - h) / 2) * 100}%;height:${h * 100}%">${img(visibleRect(full, srcAR, ar))}</div>`;
    }
    case 'split': {
      const top = `<div class="pv" style="left:0;right:0;top:0;height:50%">${img(visibleRect(cam, srcAR, PHONE_AR / 0.5))}</div>`;
      const bottomImg = L.game_fit === 'contain'
        ? `<img alt="" src="${esc(src)}" style="left:0;width:100%;height:auto;top:50%;transform:translateY(-50%)">`
        : img(visibleRect(full, srcAR, PHONE_AR / 0.5));
      return `${top}<div class="pv" style="left:0;right:0;bottom:0;height:50%">${bottomImg}</div>`;
    }
    case 'pip': {
      const w = clamp(Number(L.pip_scale), 0.18, 0.6);
      const h = (w * PHONE_AR) / camAR;
      const side = L.pip_corner.endsWith('l') ? 'left:4.5%' : 'right:4.5%';
      const vert = L.pip_corner.startsWith('t') ? 'top:19%' : 'bottom:40%';
      return `<div class="pv" style="inset:0">${img(visibleRect(full, srcAR, PHONE_AR))}</div>` +
             `<div class="pv cam" style="${side};${vert};width:${w * 100}%;height:${h * 100}%">${img(visibleRect(cam, srcAR, camAR))}</div>`;
    }
    case 'duo': {                       // dua pembicara ditumpuk; tiap panel 9:8 mengikuti satu wajah (kiri atas, kanan bawah)
      const [a, b] = duoCenters(faces);
      const pane = (c, edge) => `<div class="pv" style="left:0;right:0;${edge}:0;height:50%">${img(faceWindow(c, srcAR, PHONE_AR / 0.5))}</div>`;
      return pane(a, 'top') + pane(b, 'bottom');
    }
    default:
      return `<div class="pv" style="inset:0">${img(faceWindow(faceCx, srcAR, PHONE_AR))}</div>`;
  }
}

export function previewOverlays(s, presets, clip) {
  let html = '';
  const px = (v) => `${((v / 1080) * 100).toFixed(3)}cqw`;
  if (s.captions.enabled) {
    const p = presets?.captions?.[s.captions.preset] || { inactive: '#fff', active: '#ffe14d', outline: '#000', uppercase: true };
    const up = s.captions.uppercase === 'auto' ? p.uppercase : s.captions.uppercase === 'on';
    const words = ['ini', 'contoh', 'caption'].map((w) => (up ? w.toUpperCase() : w));
    const capY = s.layout.mode === 'duo' && s.layout.duo_seam ? 0.5 : s.captions.y_frac;     // layout duo: caption di sambungan
    html += `<div class="pv-cap" style="top:${capY * 100}%;font-size:${px(s.captions.size)};color:${p.inactive};` +
            `-webkit-text-stroke:${px(8)} ${p.outline}">${words[0]} <span style="color:${p.active}">${words[1]}</span> ${words[2]}</div>`;
  }
  const ttl = overlayTitleFor(s, clip || {});
  if (s.title.enabled && ttl) {
    html += `<div class="pv-title" style="top:${s.title.y_frac * 100}%;font-size:${px(s.title.size)}">${esc(ttl)}</div>`;
  }
  return html;
}

// ---------------------------------------------------------------- DOM
const getPath = (o, p) => p.split('.').reduce((a, k) => a?.[k], o);
function setPath(o, p, v) {
  const ks = p.split('.');
  const last = ks.pop();
  ks.reduce((a, k) => a[k], o)[last] = v;
}

const seg = (k, opts, label = (v) => v) =>
  `<div class="seg" role="group">${opts.map((v) => `<button type="button" data-k="${k}" data-v="${esc(v)}" aria-pressed="false">${esc(label(v))}</button>`).join('')}</div>`;
const range = (k, min, max, step, fmt = '') =>
  `<input type="range" data-k="${k}" data-n="1" min="${min}" max="${max}" step="${step}"${fmt ? ` data-fmt="${fmt}"` : ''}>`;
const check = (k, label) => `<label class="check"><input type="checkbox" data-k="${k}"><span>${label}</span></label>`;
const field = (label, inner, when = '') => `<label${when ? ` data-when="${when}"` : ''}><span>${label}</span>${inner}</label>`;
const block = (label, inner, when = '') => `<fieldset${when ? ` data-when="${when}"` : ''}><legend>${label}</legend>${inner}</fieldset>`;
const corners = ['tl', 'tr', 'bl', 'br'];

export function createStudio({ root, presets, health, initial, onStyleChange }) {
  let style = mergeStyle(initial);
  let ctx = null;                         // { clip, video }
  let offset = 0;
  let frame = { url: null, face: 0.5, faces: [], ar: 16 / 9, state: 'idle', key: '' };
  const q = (sel) => root.querySelector(sel);

  function html() {
    const caps = Object.entries(presets.captions);
    return `
    <div class="stage">
      <div class="phone" id="phone" style="container-type:size"></div>
      <div>
        <div class="scrub"><label><span>${t('studio.frame')}</span><input type="range" id="scrub" min="0" max="1" step="0.5" value="0"></label>
          <div class="meta"><span id="scrub-t">0:00</span><span id="frame-state"></span></div></div>
        <div class="src-view" id="src-view" data-when="layout.mode=split|pip"><img alt="" id="src-img"><div class="camrect" id="camrect"></div></div>
      </div>
    </div>
    <div class="groups">
      <details open><summary>${t('studio.layout')}</summary><div class="fields">
        ${seg('layout.mode', presets.layouts, (v) => t(`lay.${v}`))}
        ${block(t('lay.aspect'), seg('layout.aspect', ['16:9', '4:3', '1:1', 'auto']), 'layout.mode=letterbox')}
        ${block(t('lay.backdrop'), seg('layout.backdrop', ['blur', 'black'], (v) => t(`lay.${v}`)), 'layout.mode=letterbox')}
        ${block(t('lay.game'), seg('layout.game_fit', ['cover', 'contain'], (v) => t(`lay.${v}`)), 'layout.mode=split')}
        ${field(t('lay.cam.corner'), `<select data-k="layout.camCorner">${corners.map((c) => `<option value="${c}">${t(`corner.${c}`)}</option>`).join('')}</select>`, 'layout.mode=split|pip')}
        ${field(t('lay.cam.size'), range('layout.camSize', 0.15, 0.6, 0.01), 'layout.mode=split|pip')}
        ${field(t('lay.pip.corner'), `<select data-k="layout.pip_corner">${corners.map((c) => `<option value="${c}">${t(`corner.${c}`)}</option>`).join('')}</select>`, 'layout.mode=pip')}
        ${field(t('lay.pip.size'), range('layout.pip_scale', 0.2, 0.55, 0.01), 'layout.mode=pip')}
        <div data-when="layout.mode=duo">${check('layout.duo_seam', t('lay.duo.seam'))}</div>
      </div></details>
      <details open><summary>${t('studio.caption')}</summary><div class="fields">
        ${check('captions.enabled', t('cap.on'))}
        <div data-when="captions.enabled=true" class="fields">
          ${block(t('cap.preset'), `<div class="swatches">${caps.map(([k, v]) => `<button type="button" class="swatch" data-k="captions.preset" data-v="${k}" aria-pressed="false"><i style="background:linear-gradient(90deg,${v.inactive} 0 50%,${v.active} 50%);box-shadow:inset 0 0 0 2px ${v.outline}"></i>${esc(v.label)}</button>`).join('')}</div>`)}
          ${field(t('cap.font'), `<select data-k="captions.font">${presets.fonts.map((f) => `<option>${esc(f)}</option>`).join('')}</select>`)}
          <div class="two">${field(t('cap.size'), range('captions.size', 56, 120, 2))}${field(t('cap.pos'), range('captions.y_frac', 0.3, 0.9, 0.01))}</div>
          <div class="two">${field(t('cap.words'), range('captions.words_per_chunk', 1, 5, 1))}
            ${block(t('cap.upper'), seg('captions.uppercase', ['auto', 'on', 'off'], (v) => t(`cap.upper.${v}`)))}</div>
          ${check('export_srt', t('cap.srt'))}
        </div>
      </div></details>
      <details><summary>${t('studio.titleov')}</summary><div class="fields">
        ${check('title.enabled', t('ttl.on'))}
        <div data-when="title.enabled=true" class="fields">
          ${field(t('ttl.source'), `<select data-k="title.source"><option value="hook">${t('ttl.src.hook')}</option><option value="title">${t('ttl.src.title')}</option><option value="custom">${t('ttl.src.custom')}</option></select>`)}
          ${field(t('ttl.custom'), '<input type="text" data-k="title.text" maxlength="120">', 'title.source=custom')}
          ${field(t('ttl.pos'), range('title.y_frac', 0.05, 0.4, 0.01))}
          ${field(t('ttl.dur'), `<select data-k="title.duration" data-n="1"><option value="3">${t('ttl.secs', { s: 3 })}</option><option value="5">${t('ttl.secs', { s: 5 })}</option><option value="10">${t('ttl.secs', { s: 10 })}</option><option value="0">${t('ttl.full')}</option></select>`)}
        </div>
      </div></details>
      <details><summary>${t('studio.output')}</summary><div class="fields">
        ${block(t('out.res'), seg('resolution', ['720', '1080'], (v) => `${v}p`))}
        ${block(t('out.quality'), seg('quality', ['fast', 'balanced', 'high'], (v) => t(`out.q.${v}`)))}
        ${block(t('out.enc') + (health?.gpu_encoder ? ` — ${health.gpu_encoder}` : ''), seg('encoder', ['auto', 'gpu', 'cpu'], (v) => t(`out.enc.${v}`)))}
        ${check('loudnorm', t('out.loud'))}
      </div></details>
    </div>`;
  }

  function syncControls() {
    root.querySelectorAll('[data-k]').forEach((el) => {
      const v = getPath(style, el.dataset.k);
      if (el.tagName === 'BUTTON') el.setAttribute('aria-pressed', String(String(v) === el.dataset.v));
      else if (el.type === 'checkbox') el.checked = !!v;
      else if (document.activeElement !== el) el.value = v;
    });
    root.querySelectorAll('[data-when]').forEach((el) => {
      const [k, vals] = el.dataset.when.split('=');
      el.hidden = !vals.split('|').includes(String(getPath(style, k)));
    });
  }

  function paint() {
    const phone = q('#phone');
    if (!phone || !ctx) return;
    const layers = previewLayers(style, frame.url, frame.ar, frame.face, frame.faces);
    phone.innerHTML = layers + previewOverlays(style, presets, ctx.clip) +
      (frame.state === 'error' ? `<div class="pv-msg">${t('studio.noframe')}</div>` : frame.state === 'loading' ? `<div class="pv-msg">${t('studio.loading')}</div>` : '');
    const sv = q('#src-view');
    if (sv) {
      q('#src-img').src = frame.url || PLACEHOLDER;
      const [x, y, w, h] = camRect(style.layout.camCorner, style.layout.camSize);
      Object.assign(q('#camrect').style, { left: `${x * 100}%`, top: `${y * 100}%`, width: `${w * 100}%`, height: `${h * 100}%` });
    }
    q('#frame-state').textContent = frame.state === 'loading' ? t('studio.loading') : '';
  }

  const loadFrame = debounce(async () => {
    if (!ctx) return;
    const at = ctx.clip.start + offset;
    const key = `${ctx.video.id}@${at.toFixed(1)}`;
    if (frame.key === key && frame.state === 'ready') return;
    frame = { ...frame, state: 'loading', key };
    paint();
    try {
      const res = await fetch(api.frameUrl(ctx.video.id, at), { credentials: 'same-origin' });
      if (!res.ok) throw new Error(String(res.status));
      const face = parseFloat(res.headers.get('X-Face-Center'));
      const faces = (res.headers.get('X-Face-Centers') || '').split(',').map(parseFloat).filter(Number.isFinite);
      const blob = await res.blob();
      if (frame.key !== key) return;                       // sudah pindah posisi
      if (frame.url?.startsWith('blob:')) URL.revokeObjectURL(frame.url);
      const url = URL.createObjectURL(blob);
      const probe = new Image();
      await new Promise((ok, bad) => { probe.onload = ok; probe.onerror = bad; probe.src = url; });
      frame = { url, face: Number.isFinite(face) ? face : 0.5, faces, ar: probe.naturalWidth / probe.naturalHeight, state: 'ready', key };
    } catch {
      if (frame.key === key) frame = { ...frame, url: null, state: 'error' };
    }
    paint();
  }, 280);

  function mount() {
    const wasOpen = [...root.querySelectorAll('details')].map((d) => d.open);     // jangan reset akordeon tiap ganti klip
    root.innerHTML = html();
    root.querySelectorAll('details').forEach((d, i) => { if (wasOpen[i] !== undefined) d.open = wasOpen[i]; });
    syncControls();
    const scrub = q('#scrub');
    const dur = ctx.clip.end - ctx.clip.start;
    scrub.max = String(Math.max(0.5, Math.floor(dur * 2) / 2));
    scrub.value = String(offset);
    q('#scrub-t').textContent = fmtTime(ctx.clip.start + offset);
    scrub.addEventListener('input', () => {
      offset = Number(scrub.value);
      q('#scrub-t').textContent = fmtTime(ctx.clip.start + offset);
      loadFrame();
    });
    root.onclick = (e) => {
      const b = e.target.closest('button[data-k]');
      if (!b) return;
      setPath(style, b.dataset.k, b.dataset.v);
      changed();
    };
    root.oninput = root.onchange = (e) => {
      const el = e.target.closest('[data-k]');
      if (!el || el.tagName === 'BUTTON') return;
      const raw = el.type === 'checkbox' ? el.checked : el.value;
      setPath(style, el.dataset.k, el.dataset.n ? Number(raw) : raw);
      changed();
    };
    paint();
  }

  function changed() {
    syncControls();
    paint();
    onStyleChange(style);
  }

  return {
    get style() { return style; },
    setStyle(s) { style = mergeStyle(s); if (ctx) { syncControls(); paint(); } },
    setClip(clip, video) {
      const first = !ctx || ctx.video.id !== video.id;
      ctx = { clip, video };
      offset = Math.min(2, Math.max(0, (clip.end - clip.start) / 2));
      if (first) frame = { url: null, face: 0.5, faces: [], ar: 16 / 9, state: 'idle', key: '' };
      mount();
      loadFrame();
    },
    refreshClip(clip) {                                  // judul/hook berubah -> overlay ikut berubah
      if (ctx && ctx.clip.id === clip.id) { ctx.clip = clip; paint(); }
    },
    rerender() { if (ctx) { mount(); } },
    clear() { ctx = null; },
  };
}

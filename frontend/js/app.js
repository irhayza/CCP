import { api, ApiError, followBatch, followJob } from './api.js';
import { getLang, setLang, t } from './i18n.js';
import { captionText, jobLine, renderQueue, summarize } from './queue.js';
import { renderStrip } from './strip.js';
import { buildApiStyle, createStudio, overlayTitleFor } from './studio.js';
import { clamp, esc, fmt1, fmtBytes, fmtTime, scoreTier } from './util.js';

const $ = (s, r = document) => r.querySelector(s);
const $$ = (s, r = document) => [...r.querySelectorAll(s)];

const store = {
  get(k, d = null) { try { return localStorage.getItem(k) ?? sessionStorage.getItem(k) ?? d; } catch { return d; } },
  set(k, v, persist = true) {
    try { (persist ? localStorage : sessionStorage).setItem(k, v); (persist ? sessionStorage : localStorage).removeItem(k); } catch { /* mode privat */ }
  },
};

const state = {
  health: null, presets: null, video: null, heatmap: [], moments: [], picked: new Set(), activeId: null,
  stopAnalysis: null, stopBatch: null, batch: null, preset: 'standard', jobs: [],
  transcriptSource: 'none',
};
let studio = null;

// ------------------------------------------------------------------ notifikasi browser
const notif = {
  _asked: false,
  async ask() {
    if (this._asked || !('Notification' in window) || Notification.permission !== 'default') return;
    this._asked = true;
    if (confirm(t('notif.ask'))) await Notification.requestPermission();
  },
  send(title, body) {
    if (!('Notification' in window) || Notification.permission !== 'granted') return;
    new Notification(title, { body, icon: '/favicon.ico' });
  },
};

// ------------------------------------------------------------------ riwayat URL
const URL_HISTORY_KEY = 'ccpm.urlHistory';
const MAX_HISTORY = 8;
function pushHistory(url) {
  try {
    let h = JSON.parse(localStorage.getItem(URL_HISTORY_KEY) || '[]');
    h = [url, ...h.filter((u) => u !== url)].slice(0, MAX_HISTORY);
    localStorage.setItem(URL_HISTORY_KEY, JSON.stringify(h));
    renderHistory();
  } catch { /* mode privat */ }
}
function renderHistory() {
  const dl = document.getElementById('url-history');
  if (!dl) return;
  try {
    const h = JSON.parse(localStorage.getItem(URL_HISTORY_KEY) || '[]');
    dl.innerHTML = h.map((u) => `<option value="${esc(u)}"></option>`).join('');
  } catch { /* abaikan */ }
}


// ------------------------------------------------------------------ util UI
let toastTimer;
function toast(msg) {
  const el = $('#toast');
  el.textContent = msg;
  el.classList.add('show');
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => el.classList.remove('show'), 2600);
}
const errText = (e) => (e instanceof ApiError ? (e.status === 0 ? t('err.net') : e.message) : t('err.generic'));
const getKey = () => (store.get('ccpm.key') || '').trim();

function applyI18n() {
  document.documentElement.lang = getLang();
  $$('[data-i18n]').forEach((el) => { el.textContent = t(el.dataset.i18n); });
  $$('[data-i18n-ph]').forEach((el) => { el.placeholder = t(el.dataset.i18nPh); });
  $('#lang-id').setAttribute('aria-pressed', String(getLang() === 'id'));
  $('#lang-en').setAttribute('aria-pressed', String(getLang() === 'en'));
  renderChips(); renderBanners(); renderRenderButton();
  if (state.video) { renderClips(); drawStrip(false); renderNotes(); }
  studio?.rerender();
  if (state.jobs.length) paintQueue(state.jobs);
}

// ------------------------------------------------------------------ status
function renderChips() {
  const h = state.health;
  $('#chips').innerHTML = h ? [
    `<span class="chip ${h.gpu_encoder ? 'on' : ''}">${esc(h.gpu_encoder ? t('chip.gpu', { name: h.gpu_encoder }) : t('chip.cpu'))}</span>`,
    h.opencv ? '' : `<span class="chip">${t('chip.nofaces')}</span>`,
    h.custom_fonts ? '' : `<span class="chip">${t('chip.nofonts')}</span>`,
    h.ytdlp ? `<span class="chip">yt-dlp ${esc(h.ytdlp)}</span>` : '',
  ].join('') : '';
}
function renderBanners(offline = false) {
  const h = state.health, out = [];
  if (offline) out.push(t('banner.offline'));
  if (h && !h.ffmpeg) out.push(t('banner.ffmpeg'));
  if (h && !h.ytdlp) out.push(t('banner.ytdlp'));
  $('#banners').innerHTML = out.map((m) => `<div class="banner" role="alert">${esc(m)}</div>`).join('');
}

// ------------------------------------------------------------------ analisis
function formError(msg) {
  const el = $('#form-error');
  el.textContent = msg || '';
  el.hidden = !msg;
}
function setBusy(busy) {
  $('#go').hidden = busy; $('#stop').hidden = !busy; $('#progress-line').hidden = !busy;
}
const stageText = (j) => {
  const k = `stage.${j.stage}`;
  const s = t(k);
  return `${s === k ? t('ana.working') : s}${j.message && s === k ? ` ${j.message}` : ''}${j.message && s !== k ? ` — ${j.message}` : ''}`;
};

async function analyze() {
  formError('');
  const url = $('#url').value.trim();
  if (!url) return formError(t('err.url'));
  const custom = state.preset === 'custom';
  const body = {
    url, preset: custom ? 'standard' : state.preset, max_clips: clamp(Number($('#count').value) || 8, 1, 20),
    focus: $('#focus').value.trim(), language: $('#alang').value, use_cache: !$('#nocache').checked,
    ...(custom ? { min_len: Number($('#minlen').value), max_len: Number($('#maxlen').value) } : {}),
  };
  setBusy(true);
  $('#ana-progress').value = 0;
  $('#ana-msg').textContent = t('ana.working');
  try {
    const { job_id } = await api.analyze(body, getKey());
    state.stopAnalysis = followJob(job_id, (j) => {
      $('#ana-progress').value = j.progress || 0;
      $('#ana-msg').textContent = stageText(j);
      state.analysisJob = j.id;
      if (j.status === 'done') { setBusy(false); loadResult(j.result); }
      else if (j.status === 'failed') { setBusy(false); formError(j.error || t('err.generic')); }
      else if (j.status === 'cancelled' || j.status === 'interrupted') setBusy(false);
    });
    state.analysisJob = job_id;
  } catch (e) {
    setBusy(false);
    formError(errText(e));
  }
}

function loadResult(r) {
  state.video = r.video; state.heatmap = r.heatmap || []; state.meta = r.meta || {}; state.fromCache = r.from_cache;
  state.transcriptSource = r.meta?.transcript || 'none';
  state.moments = r.moments.map((m) => ({ ...m }));
  state.picked = new Set(state.moments.slice(0, 3).map((m) => m.id));
  state.activeId = state.moments[0]?.id || null;
  pushHistory(r.video?.url || $('#url').value.trim());
  $('#strip-wrap').hidden = false;
  $('#video-title').textContent = r.video.title;
  $('#video-meta').textContent = `${r.video.channel} · ${fmtTime(r.video.duration)}`;
  drawStrip(true); renderNotes(); renderClips(); renderRenderButton();
  if (state.activeId) activate(state.activeId, false);
  $('#strip-wrap').scrollIntoView({ behavior: 'smooth', block: 'start' });
}

function renderNotes() {
  if (!state.meta) return;
  const m = state.meta, items = [t(`ana.mode.${m.mode}`)];
  if (m.models?.length) items.push(t('ana.models', { names: [...new Set(m.models)].join(', ') }));
  if (state.fromCache) items.push(t('ana.cached'));
  if (!state.heatmap.length) items.push(t('strip.none'));
  items.push(...(m.notes || []));
  $('#notes').innerHTML = items.map((x) => `<li>${esc(x)}</li>`).join('');
}

function drawStrip(develop) {
  if (!state.video) return;
  renderStrip({ stripEl: $('#strip'), rulerEl: $('#ruler'), duration: state.video.duration, heatmap: state.heatmap,
    moments: state.moments, activeId: state.activeId, picked: state.picked, develop, label: t('clips.score'),
    onSelect: (id) => { activate(id, true); } });
}

// ------------------------------------------------------------------ daftar klip
function clipHtml(m) {
  const tags = (m.hashtags || []).map((h) => `<span>#${esc(h)}</span>`).join('');
  const field = (f, label) => `<label>${label}<span class="stepper"><button type="button" data-step="${f}:-0.5" aria-label="−0.5">−</button>` +
    `<input type="number" data-f="${f}" step="0.5" min="0" value="${fmt1(m[f])}"><button type="button" data-step="${f}:0.5" aria-label="+0.5">+</button></span></label>`;
  const src = state.transcriptSource;
  const badgeLabel = src && src !== 'none' ? t(`transcript.${src}`) : '';
  const badge = badgeLabel ? `<span class="tx-badge tx-${esc(src)}">${esc(badgeLabel)}</span>` : '';
  const nocap = (!src || src === 'none') ? `<p class="reason muted">${esc(t('q.nocaption'))}</p>` : '';
  return `<li class="clip ${m.id === state.activeId ? 'active' : ''}" data-id="${esc(m.id)}">
    <input type="checkbox" class="pick" ${state.picked.has(m.id) ? 'checked' : ''} aria-label="${t('clips.pick')}">
    <div class="score ${scoreTier(m.score)}">${Math.round(m.score)}<small>${t('clips.score')}</small></div>
    <div class="clip-body">
      <input class="clip-title" type="text" value="${esc(m.title)}" maxlength="90" aria-label="${t('clips.title.label')}">
      ${m.hook ? `<p class="clip-hook">"${esc(m.hook)}"</p>` : ''}
      <div class="times">${field('start', t('clips.start'))}${field('end', t('clips.end'))}<span class="dur" data-dur>${t('clips.dur', { s: Math.round(m.end - m.start) })}</span></div>
      ${m.reason ? `<p class="reason">${esc(m.reason)}</p>` : ''}
      ${nocap}
      ${tags ? `<div class="tags">${tags}</div>` : ''}
      ${badge}
      <button type="button" class="open ghost">${t('clips.studio')}</button>
    </div></li>`;
}
function renderClips() {
  const ul = $('#clip-list');
  ul.innerHTML = state.moments.length ? state.moments.map(clipHtml).join('') : `<li class="empty">${t('clips.empty')}</li>`;
}
function renderRenderButton() {
  const n = state.picked.size, b = $('#render');
  b.textContent = t('clips.render', { n });
  b.disabled = n === 0;
}

function activate(id, scroll) {
  state.activeId = id;
  $$('.clip').forEach((el) => el.classList.toggle('active', el.dataset.id === id));
  drawStrip(false);
  const m = state.moments.find((x) => x.id === id);
  if (m && studio) studio.setClip(m, state.video);
  if (scroll) $(`.clip[data-id="${CSS.escape(id)}"]`)?.scrollIntoView({ behavior: 'smooth', block: 'nearest' });
}

function normalizeTimes(m, changed) {
  const dur = state.video.duration, max = state.presets?.max_clip_seconds || 180;
  m.start = clamp(Math.round(Number(m.start) * 2) / 2, 0, Math.max(0, dur - 3));
  m.end = clamp(Math.round(Number(m.end) * 2) / 2, m.start + 3, Math.min(dur, m.start + max));
  if (changed === 'end') m.start = Math.min(m.start, m.end - 3);
}

function onClipEvent(e) {
  const row = e.target.closest('.clip');
  if (!row) return;
  const m = state.moments.find((x) => x.id === row.dataset.id);
  if (!m) return;
  if (e.type === 'click') {
    const step = e.target.closest('[data-step]');
    if (step) {
      const [f, d] = step.dataset.step.split(':');
      m[f] = Number(m[f]) + Number(d);
      normalizeTimes(m, f);
      return refreshTimes(row, m);
    }
    if (e.target.closest('.open') || (!e.target.closest('input, button, label') && e.target.closest('.clip-body, .score'))) activate(m.id, false);
  } else if (e.type === 'change') {
    if (e.target.classList.contains('pick')) {
      e.target.checked ? state.picked.add(m.id) : state.picked.delete(m.id);
      renderRenderButton(); drawStrip(false);
    } else if (e.target.dataset.f) {
      m[e.target.dataset.f] = Number(e.target.value);
      normalizeTimes(m, e.target.dataset.f);
      refreshTimes(row, m);
    }
  } else if (e.type === 'input' && e.target.classList.contains('clip-title')) {
    m.title = e.target.value;
    studio?.refreshClip(m);
  }
}
function refreshTimes(row, m) {
  row.querySelector('[data-f="start"]').value = fmt1(m.start);
  row.querySelector('[data-f="end"]').value = fmt1(m.end);
  row.querySelector('[data-dur]').textContent = t('clips.dur', { s: Math.round(m.end - m.start) });
  drawStrip(false);
  if (state.activeId === m.id) studio?.setClip(m, state.video);
}

// ------------------------------------------------------------------ render & antrean
async function startRender() {
  const picked = state.moments.filter((m) => state.picked.has(m.id));
  if (!picked.length) return;
  notif.ask();
  const style = studio.style;
  const body = {
    url: state.video.url, language: $('#alang').value, style: buildApiStyle(style),
    clips: picked.map((m) => ({ id: m.id, start: m.start, end: m.end, title: m.title, hook: m.hook || '', reason: m.reason || '',
      hashtags: m.hashtags || [], overlay_title: overlayTitleFor(style, m) })),
  };
  $('#render').disabled = true;
  try {
    const { batch_id, job_ids } = await api.renderBatch(body);
    state.stopBatch?.();
    state.batch = { id: batch_id, titles: Object.fromEntries(job_ids.map((id, i) => [id, picked[i].title])) };
    $('#queue').hidden = false;
    $('#zip').href = api.zipUrl(batch_id);
    $('#csv').href = api.csvUrl(batch_id);
    follow();
    $('#queue').scrollIntoView({ behavior: 'smooth', block: 'start' });
  } catch (e) {
    toast(errText(e));
  } finally {
    renderRenderButton();
  }
}
function follow() {
  state.stopBatch?.();
  state.stopBatch = followBatch(state.batch.id, (jobs) => { state.jobs = jobs; paintQueue(jobs); });
}
function paintQueue(jobs) {
  const s = summarize(jobs);
  $('#queue-summary').textContent = t('q.done', { d: s.done, n: s.total });
  $('#queue-progress').value = s.pct;
  $('#zip').hidden = $('#csv').hidden = s.done === 0;
  $('#cancel-batch').hidden = jobs.every((j) => !['queued', 'running'].includes(j.status));
  renderQueue({ listEl: $('#queue-list'), jobs, titles: state.batch?.titles || {} });
  if (s.done === jobs.length && s.done > 0 && !paintQueue._notified) {
    paintQueue._notified = true;
    notif.send(t('q.done', { d: s.done, n: s.total }), t('toast.render.done'));
    toast(t('toast.render.done'));
  }
  if (s.done < jobs.length) paintQueue._notified = false;
}
async function onQueueClick(e) {
  const btn = e.target.closest('[data-act]');
  if (!btn) return;
  const id = btn.closest('.job').dataset.id, job = state.jobs.find((j) => j.id === id);
  try {
    if (btn.dataset.act === 'stop') await api.cancelJob(id);
    else if (btn.dataset.act === 'retry') { await api.retryJob(id); follow(); }
    else if (btn.dataset.act === 'copy' && job?.result) { await copyText(captionText(job.result)); toast(t('q.copied')); }
  } catch (err) { toast(errText(err)); }
}
async function copyText(text) {
  try { await navigator.clipboard.writeText(text); } catch {
    const ta = Object.assign(document.createElement('textarea'), { value: text });
    document.body.append(ta); ta.select(); document.execCommand('copy'); ta.remove();
  }
}

// ------------------------------------------------------------------ dialog
const dlg = (id) => document.getElementById(id);
function wireDialogs() {
  $$('[data-open]').forEach((b) => b.addEventListener('click', () => {
    const d = dlg(b.dataset.open);
    d.showModal();
    ({ 'dlg-cookies': loadCookies, 'dlg-storage': loadStorage, 'dlg-settings': () => { $('#apikey').value = getKey(); } })[b.dataset.open]?.();
  }));
  // simpan key saat dialog pengaturan ditutup
  dlg('dlg-settings').addEventListener('close', () => {
    const v = $('#apikey').value.trim();
    store.set('ccpm.key', v, $('#remember').checked);
  });
  $('#test-key').onclick = async () => {
    const out = $('#test-result');
    out.textContent = '…';
    try { const r = await api.models($('#apikey').value.trim()); out.textContent = t('set.test.ok', { n: r.models.length }); }
    catch (e) { out.textContent = errText(e); }
  };
  $('#update-ytdlp').onclick = async () => {
    const out = $('#update-result');
    out.textContent = '…';
    try { out.textContent = `yt-dlp ${(await api.updateYtdlp()).version}`; } catch (e) { out.textContent = errText(e); }
  };
  // cookies
  $('#ck-save').onclick = async () => {
    $('#ck-error').hidden = true;
    try { await api.saveCookies($('#ck-text').value); $('#ck-text').value = ''; toast(t('toast.saved')); loadCookies(); }
    catch (e) { $('#ck-error').textContent = errText(e); $('#ck-error').hidden = false; }
  };
  $('#ck-del').onclick = async () => {
    if (!confirm(t('ck.confirm.delete'))) return;
    await api.deleteCookies().catch(() => {}); toast(t('toast.deleted')); loadCookies();
  };
  // storage
  $('#st-clear').onclick = async () => {
    try { const r = await api.clearTemp(); toast(t('st.cleared', { size: r.freed })); loadStorage(); } catch (e) { toast(errText(e)); }
  };
  // raw download
  $('#raw-go').onclick = startRaw;
  // login
  $('#login-form').addEventListener('submit', async (e) => {
    e.preventDefault();
    try { await api.login($('#token').value); dlg('dlg-login').close(); await boot(true); }
    catch (err) { $('#login-error').textContent = errText(err); $('#login-error').hidden = false; }
  });
  dlg('dlg-login').addEventListener('cancel', (e) => e.preventDefault());
}
async function loadCookies() {
  try {
    const c = await api.cookies();
    $('#ck-status').textContent = !c.present ? t('ck.status.none') : c.valid ? t('ck.status.ok', { n: c.count, d: c.domains.join(', ') }) : t('ck.status.bad');
  } catch (e) { $('#ck-status').textContent = errText(e); }
}
async function loadStorage() {
  try {
    const s = await api.storage();
    const rows = [['st.temp', s.temp], ['st.slices', s.slices], ['st.frames', s.frames], ['st.exports', s.exports]];
    $('#st-list').innerHTML = rows.map(([k, v]) => `<dt>${t(k)}</dt><dd>${fmtBytes(v.bytes)} · ${v.files}</dd>`).join('');
  } catch (e) { $('#st-list').innerHTML = `<dt>${esc(errText(e))}</dt><dd></dd>`; }
}
async function startRaw() {
  const url = $('#url').value.trim();
  const err = $('#raw-error');
  err.hidden = true;
  if (!url) { err.textContent = t('err.url'); err.hidden = false; return; }
  $('#raw-link').hidden = true;
  $('#raw-progress').hidden = false;
  try {
    const { job_id } = await api.rawDownload(url, Number($('#raw-h').value));
    followJob(job_id, (j) => {
      $('#raw-progress').value = j.progress || 0;
      $('#raw-msg').textContent = j.status === 'failed' ? '' : j.message || '';
      if (j.status === 'failed') { err.textContent = j.error; err.hidden = false; }
      if (j.status === 'done') { $('#raw-link').href = api.fileUrl(j.result.file); $('#raw-link').hidden = false; }
    });
  } catch (e) { err.textContent = errText(e); err.hidden = false; }
}

// ------------------------------------------------------------------ boot
async function boot(afterLogin = false) {
  try {
    const [health, auth] = await Promise.all([api.health(), api.authStatus()]);
    state.health = health;
    if (auth.required && !auth.authed) { renderBanners(); dlg('dlg-login').showModal(); return; }
    if (!state.presets) state.presets = await api.presets();
  } catch (e) {
    renderBanners(true);
    return;
  }
  renderChips(); renderBanners();
  if (!studio) {
    let saved = null;
    try { saved = JSON.parse(store.get('ccpm.style') || 'null'); } catch { /* abaikan */ }
    studio = createStudio({ root: $('#studio-body'), presets: state.presets, health: state.health, initial: saved,
      onStyleChange: (s) => store.set('ccpm.style', JSON.stringify(s)) });
  }
  if (afterLogin) applyI18n();
}

function init() {
  setLang(store.get('ccpm.lang') || (navigator.language?.startsWith('id') ? 'id' : 'en'));
  try { $('#remember').checked = !sessionStorage.getItem('ccpm.key'); } catch { /* mode privat */ }
  $('#lang-id').onclick = () => { setLang('id'); store.set('ccpm.lang', 'id'); applyI18n(); };
  $('#lang-en').onclick = () => { setLang('en'); store.set('ccpm.lang', 'en'); applyI18n(); };
  $$('#preset-seg button').forEach((b) => b.addEventListener('click', () => {
    state.preset = b.dataset.v;
    $$('#preset-seg button').forEach((x) => x.setAttribute('aria-checked', String(x === b)));
    $$('.custom-len').forEach((el) => { el.hidden = state.preset !== 'custom'; });
  }));
  renderHistory();
  $('#go').onclick = analyze;
  $('#url').addEventListener('keydown', (e) => { if (e.key === 'Enter') analyze(); });
  $('#url').addEventListener('focus', async () => {
    try {
      const text = await navigator.clipboard.readText();
      if (text && /youtube\.com\/watch|youtu\.be\//.test(text) && !$('#url').value.trim()) {
        $('#url').value = text;
      }
    } catch { /* izin clipboard ditolak atau tidak tersedia */ }
  });
  $('#stop').onclick = () => { if (state.analysisJob) api.cancelJob(state.analysisJob).catch(() => {}); state.stopAnalysis?.(); setBusy(false); };
  const list = $('#clip-list');
  ['click', 'change', 'input'].forEach((ev) => list.addEventListener(ev, onClipEvent));
  $('#sel-all').onclick = () => { state.picked = new Set(state.moments.map((m) => m.id)); renderClips(); renderRenderButton(); drawStrip(false); };
  $('#sel-none').onclick = () => { state.picked.clear(); renderClips(); renderRenderButton(); drawStrip(false); };
  $('#render').onclick = startRender;
  $('#queue-list').addEventListener('click', onQueueClick);
  $('#cancel-batch').onclick = () => state.batch && api.cancelBatch(state.batch.id).catch((e) => toast(errText(e)));
  wireDialogs();
  applyI18n();
  boot();
}

init();

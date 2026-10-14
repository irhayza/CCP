// Klien API tipis. Same-origin; cookie token (httpOnly) diatur server.

export class ApiError extends Error {
  constructor(message, status) { super(message); this.status = status; }
}

export const TERMINAL = ['done', 'failed', 'cancelled', 'interrupted'];

async function request(path, { method = 'GET', body, headers = {} } = {}) {
  let res;
  try {
    res = await fetch(`/api${path}`, {
      method, credentials: 'same-origin',
      headers: { ...(body !== undefined ? { 'Content-Type': 'application/json' } : {}), ...headers },
      body: body !== undefined ? JSON.stringify(body) : undefined,
    });
  } catch {
    throw new ApiError('network', 0);
  }
  if (!res.ok) {
    let detail = '';
    try {
      const j = await res.json();
      detail = typeof j.detail === 'string' ? j.detail : JSON.stringify(j.detail);
    } catch { /* bukan JSON */ }
    throw new ApiError(detail || `HTTP ${res.status}`, res.status);
  }
  return res.status === 204 ? null : res.json();
}

const keyHeader = (key) => (key ? { 'X-Gemini-Key': key } : {});

export const api = {
  health: () => request('/health'),
  authStatus: () => request('/auth-status'),
  login: (token) => request('/login', { method: 'POST', body: { token } }),
  presets: () => request('/presets'),
  models: (key) => request('/models', { headers: keyHeader(key) }),
  analyze: (body, key) => request('/analyze', { method: 'POST', body, headers: keyHeader(key) }),
  job: (id) => request(`/jobs/${id}`),
  cancelJob: (id) => request(`/jobs/${id}/cancel`, { method: 'POST' }),
  retryJob: (id) => request(`/jobs/${id}/retry`, { method: 'POST' }),
  renderBatch: (body) => request('/render-batch', { method: 'POST', body }),
  batch: (id) => request(`/render-progress/${id}`),
  cancelBatch: (id) => request(`/batches/${id}/cancel`, { method: 'POST' }),
  cookies: () => request('/cookies'),
  saveCookies: (text) => request('/cookies', { method: 'POST', body: { text } }),
  deleteCookies: () => request('/cookies', { method: 'DELETE' }),
  storage: () => request('/temp-storage-info'),
  clearTemp: () => request('/clear-temp', { method: 'POST' }),
  rawDownload: (url, max_height) => request('/download-raw-video', { method: 'POST', body: { url, max_height } }),
  updateYtdlp: () => request('/system/update-ytdlp', { method: 'POST' }),
  fileUrl: (name) => `/api/download-rendered/${encodeURIComponent(name)}`,
  zipUrl: (batch) => `/api/download-batch-zip/${batch}`,
  csvUrl: (batch) => `/api/publish-pack/${batch}`,
  frameUrl: (videoId, t) => `/api/clip-frame?video_id=${encodeURIComponent(videoId)}&timestamp=${Number(t).toFixed(1)}`,
};

/**
 * Ikuti job/batch lewat SSE; bila SSE putus, pindah ke polling. Mengembalikan fungsi stop().
 * onData menerima objek job (atau array job untuk batch).
 */
export function follow(path, pollFn, onData, isDone) {
  let es, timer, stopped = false;
  const finish = () => { stopped = true; es?.close(); clearTimeout(timer); };
  const poll = async () => {
    if (stopped) return;
    try {
      const d = await pollFn();
      onData(d);
      if (isDone(d)) return finish();
    } catch { /* coba lagi */ }
    timer = setTimeout(poll, 1500);
  };
  if (typeof EventSource === 'undefined') { poll(); return finish; }
  es = new EventSource(`/api${path}`);
  es.onmessage = (ev) => {
    try {
      const d = JSON.parse(ev.data);
      onData(d);
      if (isDone(d)) finish();
    } catch { /* abaikan frame rusak */ }
  };
  es.addEventListener('gone', finish);
  es.onerror = () => { es.close(); if (!stopped) poll(); };   // EventSource akan reconnect tanpa henti: kita ambil alih
  return finish;
}

export const followJob = (id, onData) =>
  follow(`/jobs/${id}/events`, () => api.job(id), onData, (j) => TERMINAL.includes(j.status));

export const followBatch = (id, onData) =>
  follow(`/batches/${id}/events`, async () => (await api.batch(id)).jobs, onData,
    (jobs) => jobs.length > 0 && jobs.every((j) => TERMINAL.includes(j.status)));

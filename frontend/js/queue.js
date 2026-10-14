import { t } from './i18n.js';
import { api } from './api.js';
import { esc, hashtagText } from './util.js';

const label = (stage, fallback) => {
  const k = `stage.${stage}`;
  const s = t(k);
  return s === k ? (fallback || '') : s;
};

export function captionText(r) {
  return [r.title, r.hook, hashtagText(r.hashtags)].filter(Boolean).join('\n\n');
}

export function jobLine(j) {
  if (j.status === 'failed') return j.error || t('q.status.failed');
  if (j.status === 'done') return `${t('q.status.done')} · ${j.result?.encoder || ''}${j.result?.face_tracked ? ` · ${t('q.face')}` : ''}`;
  if (j.status === 'queued') return t('q.status.queued');
  if (['cancelled', 'interrupted'].includes(j.status)) return t(`q.status.${j.status}`);
  return `${label(j.stage, j.message)} ${Math.round(j.progress || 0)}%`;
}

export function summarize(jobs) {
  const done = jobs.filter((j) => j.status === 'done').length;
  const pct = jobs.length ? jobs.reduce((a, j) => a + (j.status === 'done' ? 100 : j.progress || 0), 0) / jobs.length : 0;
  return { done, total: jobs.length, pct };
}

export function renderQueue({ listEl, jobs, titles }) {
  listEl.innerHTML = jobs.map((j) => {
    const r = j.result || {};
    const title = r.title || titles[j.id] || '';
    const active = j.status === 'queued' || j.status === 'running';
    const retry = ['failed', 'cancelled', 'interrupted'].includes(j.status);
    return `<li class="job ${j.status}" data-id="${esc(j.id)}">
      <div class="thumb">${r.thumbnail ? `<img alt="" src="${api.fileUrl(r.thumbnail)}">` : ''}</div>
      <div class="job-main"><strong>${esc(title)}</strong>
        ${active ? `<progress max="100" value="${j.progress || 0}"></progress>` : ''}
        <span class="job-status">${esc(jobLine(j))}${(r.notes || []).map((n) => ` · ${esc(n)}`).join('')}</span></div>
      <div class="job-actions">
        ${j.status === 'done' ? `<a class="btn primary" href="${api.fileUrl(r.file)}" download>${t('q.dl')}</a>
           <button type="button" data-act="copy">${t('q.copy')}</button>` : ''}
        ${active ? `<button type="button" class="danger" data-act="stop">${t('q.stop')}</button>` : ''}
        ${retry ? `<button type="button" data-act="retry">${t('q.retry')}</button>` : ''}
      </div></li>`;
  }).join('');
}

import { assignLanes, esc, fmtTime, rulerTicks, spanPct } from './util.js';

const LANE_PX = 24;

/** Gambar strip film: batang heatmap + tanda klip (bracket merah) + penggaris waktu. */
export function renderStrip({ stripEl, rulerEl, duration, heatmap, moments, activeId, picked, develop, onSelect, label }) {
  const bars = (heatmap || []).map((h, i) => {
    const { left, width } = spanPct(h.start_time, h.end_time, duration);
    return `<span class="heat" style="left:${left}%;width:${width}%;--v:${h.value};--i:${i}"><i></i></span>`;
  }).join('');
  const spans = (moments || []).map((m) => ({ id: m.id, ...spanPct(m.start, m.end, duration) }));
  const width0 = stripEl.clientWidth || 1000;
  const lanes = assignLanes(spans, (34 / width0) * 100);          // label ~34px; hindari tumpang tindih
  const maxLane = Math.max(0, ...Object.values(lanes));
  stripEl.style.marginTop = `${6 + maxLane * LANE_PX}px`;
  const marks = (moments || []).map((m, i) => {
    const { left, width } = spans[i];
    const cls = ['mark', m.id === activeId ? 'active' : '', picked.has(m.id) ? 'picked' : ''].join(' ');
    const name = `${label} ${Math.round(m.score)} · ${fmtTime(m.start)}–${fmtTime(m.end)} · ${m.title}`;
    return `<button type="button" class="${cls}" data-id="${esc(m.id)}" style="left:${left}%;width:${width}%" ` +
           `aria-label="${esc(name)}" title="${esc(name)}"><b style="top:${-30 - lanes[m.id] * LANE_PX}px;--stem:${lanes[m.id] * LANE_PX}px">${Math.round(m.score)}</b></button>`;
  }).join('');
  stripEl.classList.toggle('developing', !!develop);
  stripEl.innerHTML = `<div class="strip-inner">${bars}${marks}</div>`;
  stripEl.querySelectorAll('.mark').forEach((b) => b.addEventListener('click', () => onSelect(b.dataset.id)));
  rulerEl.innerHTML = rulerTicks(duration).map((k) => `<span style="left:${k.pct}%">${fmtTime(k.t)}</span>`).join('');
}

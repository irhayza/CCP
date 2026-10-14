// Jalankan: node --test tests/frontend.test.mjs
import test from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync, readdirSync } from 'node:fs';
import { fileURLToPath, pathToFileURL } from 'node:url';
import { dirname, join } from 'node:path';

const root = join(dirname(fileURLToPath(import.meta.url)), '..', 'frontend');
const importMod = (rel) => import(pathToFileURL(join(root, rel)).href);
const U = await importMod('js/util.js');
const I = await importMod('js/i18n.js');
const S = await importMod('js/studio.js');
const Q = await importMod('js/queue.js');

test('fmtTime & fmtBytes', () => {
  assert.equal(U.fmtTime(0), '0:00'); assert.equal(U.fmtTime(75), '1:15');
  assert.equal(U.fmtTime(3725), '1:02:05'); assert.equal(U.fmtTime(-5), '0:00'); assert.equal(U.fmtTime('x'), '0:00');
  assert.equal(U.fmtBytes(0), '0 B'); assert.equal(U.fmtBytes(1536), '1.5 KB'); assert.equal(U.fmtBytes(5 * 1024 ** 3), '5.0 GB');
});

test('esc mencegah injeksi HTML', () => {
  assert.equal(U.esc('<img src=x onerror="a()">&\''), '&lt;img src=x onerror=&quot;a()&quot;&gt;&amp;&#39;');
  assert.equal(U.esc(null), '');
});

test('ruler memilih langkah bulat', () => {
  const t = U.rulerTicks(600, 8);
  assert.deepEqual(t.map((x) => x.t).slice(0, 3), [0, 120, 240]);
  assert.equal(U.rulerTicks(0).length, 0);
  assert.ok(U.rulerTicks(4 * 3600, 8).length <= 9);
});

test('spanPct: klip sangat pendek tetap terlihat & tak keluar strip', () => {
  const a = U.spanPct(10, 40, 100);
  assert.equal(a.left, 10); assert.ok(Math.abs(a.width - 30) < 1e-9);
  assert.ok(U.spanPct(10, 10.1, 10000).width >= 0.35);
  assert.ok(U.spanPct(-5, 500, 100).left >= 0);
});

test('assignLanes: label berdekatan dipisah ke jalur berbeda', () => {
  const l = U.assignLanes([{ id: 'a', left: 10 }, { id: 'b', left: 11 }, { id: 'c', left: 12 }, { id: 'd', left: 40 }], 5);
  assert.deepEqual(l, { a: 0, b: 1, c: 2, d: 0 });
  assert.deepEqual(U.assignLanes([], 5), {});
});

test('fmt1 membulatkan tampilan waktu', () => {
  assert.equal(U.fmt1(675.95), '676'); assert.equal(U.fmt1(640), '640'); assert.equal(U.fmt1(12.34), '12.3'); assert.equal(U.fmt1('x'), '0');
});

test('camRect: pojok & batas', () => {
  const tl = U.camRect('tl', 0.3), br = U.camRect('br', 0.3);
  assert.deepEqual(tl.slice(0, 2), [0, 0]);
  assert.ok(br[0] + br[2] <= 1.0001 && br[1] + br[3] <= 1.0001);
  assert.ok(U.camRect('tr', 5)[2] <= 0.6);
});

test('visibleRect meniru cover_crop backend (16:9 -> 9:16 = 606/1920)', () => {
  const [x, y, w, h] = U.visibleRect([0, 0, 1, 1], 16 / 9, 9 / 16);
  assert.ok(Math.abs(w - 606 / 1920) < 0.002); assert.equal(h, 1); assert.ok(Math.abs(x - (1 - w) / 2) < 1e-9);
  const [, , w2, h2] = U.visibleRect([0, 0, 1, 1], 9 / 16, 16 / 9);        // sumber vertikal -> potong atas/bawah
  assert.equal(w2, 1); assert.ok(h2 < 0.4);
});

test('faceWindow tidak keluar frame', () => {
  for (const cx of [0, 0.1, 0.5, 0.9, 1]) {
    const [l, , w] = U.faceWindow(cx, 16 / 9, 9 / 16);
    assert.ok(l >= 0 && l + w <= 1.0000001, `cx=${cx}`);
  }
  assert.ok(Math.abs(U.faceWindow(0.5, 16 / 9, 9 / 16)[0] - (1 - 0.3156) / 2) < 0.005);
});

test('i18n: kunci ID dan EN identik, tidak ada yang kosong', () => {
  const id = Object.keys(I.STRINGS.id).sort(), en = Object.keys(I.STRINGS.en).sort();
  assert.deepEqual(id, en);
  for (const l of ['id', 'en']) for (const [k, v] of Object.entries(I.STRINGS[l])) assert.ok(v.trim(), `${l}:${k} kosong`);
  for (const k of id) {      // placeholder {x} harus sama di kedua bahasa
    const ph = (s) => (s.match(/\{\w+\}/g) || []).sort().join();
    assert.equal(ph(I.STRINGS.id[k]), ph(I.STRINGS.en[k]), k);
  }
});

test('i18n: semua kunci yang dipakai di HTML & JS ada kamusnya', () => {
  const keys = new Set(Object.keys(I.STRINGS.id));
  const missing = [];
  const html = readFileSync(join(root, 'index.html'), 'utf8');
  for (const m of html.matchAll(/data-i18n(?:-ph)?="([^"]+)"/g)) if (!keys.has(m[1])) missing.push(`html:${m[1]}`);
  for (const f of readdirSync(join(root, 'js')).filter((n) => n !== 'i18n.js')) {
    const src = readFileSync(join(root, 'js', f), 'utf8');
    for (const m of src.matchAll(/\bt\('([\w.]+)'/g)) if (!keys.has(m[1])) missing.push(`${f}:${m[1]}`);
    for (const m of src.matchAll(/\bt\(`([\w.]+)\$\{/g)) {          // t(`lay.${v}`) -> semua varian harus ada
      assert.ok([...keys].some((k) => k.startsWith(m[1])), `${f}: awalan ${m[1]} tak ada`);
    }
  }
  assert.deepEqual(missing, []);
});

test('i18n: varian dinamis lengkap', () => {
  const k = new Set(Object.keys(I.STRINGS.id));
  for (const v of ['fill_face', 'fill_center', 'letterbox', 'split', 'pip']) assert.ok(k.has(`lay.${v}`), v);
  for (const v of ['blur', 'black', 'cover', 'contain']) assert.ok(k.has(`lay.${v}`), v);
  for (const v of ['auto', 'on', 'off']) assert.ok(k.has(`cap.upper.${v}`) && k.has(`out.enc.${v === 'on' ? 'gpu' : v === 'off' ? 'cpu' : 'auto'}`));
  for (const v of ['fast', 'balanced', 'high']) assert.ok(k.has(`out.q.${v}`));
  for (const v of ['tl', 'tr', 'bl', 'br']) assert.ok(k.has(`corner.${v}`));
  for (const v of ['queued', 'running', 'done', 'failed', 'cancelled', 'interrupted']) assert.ok(k.has(`q.status.${v}`));
  for (const v of ['download', 'transcript', 'whisper', 'tracking', 'captions', 'encoding', 'done', 'start', 'info', 'ai', 'fuse']) assert.ok(k.has(`stage.${v}`), v);
  for (const v of ['ai', 'noai', 'demo']) assert.ok(k.has(`ana.mode.${v}`));
});

test('t() interpolasi & fallback', () => {
  I.setLang('en'); assert.equal(I.t('clips.render', { n: 3 }), 'Render 3 clips');
  I.setLang('id'); assert.equal(I.t('clips.render', { n: 3 }), 'Render 3 klip');
  assert.equal(I.t('tidak.ada'), 'tidak.ada'); assert.equal(I.setLang('xx'), 'id');
});

test('mergeStyle: nilai tersimpan lama tidak merusak default baru', () => {
  const s = S.mergeStyle({ layout: { mode: 'pip' }, quality: 'high', captions: { size: 100 }, kunciHilang: 1 });
  assert.equal(s.layout.mode, 'pip'); assert.equal(s.layout.camSize, 0.3); assert.equal(s.quality, 'high');
  assert.equal(s.captions.size, 100); assert.equal(s.captions.preset, 'viral_pop');
  assert.deepEqual(S.mergeStyle(null), S.DEFAULT_STYLE);
});

test('overlayTitleFor mengikuti sumber', () => {
  const s = S.mergeStyle({ title: { enabled: true, source: 'hook' } }), c = { hook: 'H', title: 'T' };
  assert.equal(S.overlayTitleFor(s, c), 'H');
  s.title.source = 'title'; assert.equal(S.overlayTitleFor(s, c), 'T');
  s.title.source = 'custom'; s.title.text = 'X'; assert.equal(S.overlayTitleFor(s, c), 'X');
  s.title.enabled = false; assert.equal(S.overlayTitleFor(s, c), '');
  assert.equal(S.overlayTitleFor(S.mergeStyle({ title: { enabled: true, source: 'hook' } }), { title: 'T' }), 'T');
});

test('previewLayers: tiap mode menghasilkan lapisan yang benar', () => {
  const mk = (layout) => S.previewLayers(S.mergeStyle({ layout }), 'blob:x', 16 / 9, 0.3);
  assert.equal((mk({ mode: 'fill_face' }).match(/<img/g) || []).length, 1);
  assert.equal((mk({ mode: 'fill_center' }).match(/<img/g) || []).length, 1);
  assert.match(mk({ mode: 'letterbox', backdrop: 'blur' }), /pv blur/);
  assert.doesNotMatch(mk({ mode: 'letterbox', backdrop: 'black' }), /pv blur/);
  assert.equal((mk({ mode: 'split' }).match(/<img/g) || []).length, 2);
  assert.match(mk({ mode: 'pip' }), /pv cam/);
  assert.match(S.previewLayers(S.mergeStyle(null), null), /data:image\/svg\+xml/);   // tanpa frame -> placeholder, bukan kosong
  const letter = mk({ mode: 'letterbox', aspect: '1:1' });         // 1:1 pada kanvas 9:16 -> tinggi 56.25%
  assert.match(letter, /height:56\.25%/);
});

test('previewOverlays: judul dan caption di-escape', () => {
  const s = S.mergeStyle({ title: { enabled: true, source: 'custom', text: '<b>x</b>' } });
  const html = S.previewOverlays(s, { captions: {}, fonts: [] }, {});
  assert.doesNotMatch(html, /<b>x/); assert.match(html, /&lt;b&gt;x/);
  assert.equal(S.previewOverlays(S.mergeStyle({ captions: { enabled: false } }), {}, {}), '');
});

test('queue helpers', () => {
  assert.deepEqual(Q.summarize([{ status: 'done' }, { status: 'running', progress: 50 }, { status: 'failed', progress: 0 }, { status: 'queued' }]),
    { done: 1, total: 4, pct: 37.5 });
  assert.match(Q.jobLine({ status: 'failed', error: 'boom' }), /boom/);
  assert.equal(Q.captionText({ title: 'T', hook: 'H', hashtags: ['a', '#b'] }), 'T\n\nH\n\n#a #b');
});

test('buildApiStyle -> JSON untuk kontrak Python', () => {
  const s = S.mergeStyle({ layout: { mode: 'split', camCorner: 'br', camSize: 0.25 }, resolution: '720',
    captions: { uppercase: 'off' }, title: { enabled: true, source: 'custom', text: 'Halo', duration: '0' } });
  const api = S.buildApiStyle(s);
  assert.equal(api.resolution, 720); assert.equal(api.captions.uppercase, false); assert.equal(api.title.duration, 0);
  assert.equal(api.layout.cam.length, 4); assert.ok(!('camCorner' in api.layout)); assert.ok(!('source' in api.title));
  if (process.env.EMIT_STYLES) {
    const out = [api];
    for (const mode of ['fill_face', 'fill_center', 'letterbox', 'split', 'pip']) out.push(S.buildApiStyle(S.mergeStyle({ layout: { mode } })));
    out.push(S.buildApiStyle(S.mergeStyle(null)));
    process.stdout.write('\n@@STYLES@@' + JSON.stringify(out) + '@@END@@\n');
  }
});

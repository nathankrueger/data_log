// Logger page: recording switch, chart grid, chart editor, storage, CSV export.

import {
  $,
  api,
  applyFlow,
  dragHandle,
  fmtAgo,
  fmtBytes,
  fmtDur,
  fmtNum,
  fmtSpan,
  h,
  icon,
  initScrollFades,
  initTheme,
  Poller,
  prefs,
  renderTopbar,
  scrollFade,
  setAge,
  showMenu,
  sortable,
  startAgeTicker,
  toast,
} from './common.js';
import { buildData, TimeChart, TIER_LABEL } from './chart.js';

const RANGES = [
  ['15m', 900],
  ['1h', 3600],
  ['6h', 21600],
  ['24h', 86400],
  ['3d', 259200],
  ['7d', 604800],
  ['All', 0],
];
const RETENTIONS = [
  ['1 hour', 3600],
  ['6 hours', 21600],
  ['1 day', 86400],
  ['3 days', 259200],
  ['1 week', 604800],
  ['2 weeks', 1209600],
  ['4 weeks', 2419200],
];
const LIVE_MS = 5000;
const STATE_MS = 15000;
const FULL_REFRESH_MS = 120000; // periodic re-sync (lets the store re-pick resolution)
const BYTES_PER_SAMPLE = 36; // raw row + b-tree overhead + rollup tiers, measured-ish
const AUTO_MIN_SPAN_S = 300; // auto range never shows less than this (matches the gateway)
const PER_ROW = 2; // charts side by side; a chart alone in its row spans the width

const S = {
  logger: null,
  catalog: new Map(),
  range: prefs.get('logger.range', 0),
  live: prefs.get('logger.live', true),
  views: new Map(),
  offset: 0, // server clock - browser clock, seconds
  pendingOrder: null, // {ids, newRows} while a reorder is being saved
};

let chartSort = null; // sortable controller for the chart grid

const serverNow = () => Date.now() / 1000 + S.offset;
const maxSeries = () => S.logger?.limits?.max_series_per_chart ?? 8;

// ---------------------------------------------------------------------------
// Chart view: one card, its data, and its uPlot instance
// ---------------------------------------------------------------------------

class ChartView {
  constructor(cfg) {
    this.cfg = cfg;
    this.cols = new Map(); // series id -> server columns
    this.tier = null;
    this.zoom = null; // [min, max] while zoomed
    this.lastFull = 0;
    this.busy = false;
    this.version = 0; // bumped by every full refresh; stale responses are dropped
    this.pendingFull = false;
    this.first = null; // oldest stored sample (auto range)
    this.savingAuto = null; // auto_range value being saved (wins over stale polls)
    this.collapsed = prefs.get(`chart.${cfg.id}.collapsed`, false);
    this._build();
    this._paintCollapsed();
  }

  _build() {
    this.titleEl = h('h3', { class: 'chart-title' });
    this.metaEl = h('div', { class: 'chart-meta' });
    this.zoomChip = h(
      'button',
      { class: 'zoom-chip', type: 'button', hidden: true, title: 'Back to the live window (or double-click the chart)', onclick: () => this.resetZoom() },
      icon('reset'),
      'Reset zoom',
    );
    const more = h(
      'button',
      {
        class: 'btn-icon',
        type: 'button',
        title: 'More',
        'aria-label': 'More actions',
        onclick: (e) => {
          e.stopPropagation();
          this._menu(more);
        },
      },
      icon('more'),
    );
    this.legendEl = h('div', { class: 'legend' });
    this.plotEl = h('div', { class: 'plot' });
    this.collapseBtn = h(
      'button',
      { class: 'chart-collapse', type: 'button', onclick: () => this.setCollapsed(!this.collapsed) },
      icon('chevron'),
    );
    this.el = h(
      'article',
      { class: 'card chart-card', 'data-id': this.cfg.id },
      dragHandle('Drag to move chart'),
      h(
        'header',
        { class: 'chart-head' },
        this.collapseBtn,
        h('div', { class: 'chart-titles' }, this.titleEl, this.metaEl),
        h(
          'div',
          { class: 'chart-actions' },
          this.zoomChip,
          h('button', { class: 'btn-icon', type: 'button', title: 'Download CSV of this view', 'aria-label': 'Download CSV', onclick: () => this.download() }, icon('download')),
          h('button', { class: 'btn-icon', type: 'button', title: 'Edit chart', 'aria-label': 'Edit chart', onclick: () => openEditor(this.cfg) }, icon('edit')),
          h('div', { class: 'menu-wrap' }, more),
        ),
      ),
      this.legendEl,
      this.plotEl,
    );
    this.chart = new TimeChart(this.plotEl, {
      onZoom: (range) => this.setZoom(range),
      onResetZoom: () => this.resetZoom(),
    });
    this.renderHeader();
  }

  setConfig(cfg) {
    if (this.savingAuto != null) cfg = { ...cfg, auto_range: this.savingAuto };
    const dataChanged =
      JSON.stringify(cfg.series.map((s) => s.id)) !== JSON.stringify(this.cfg.series.map((s) => s.id)) ||
      cfg.max_age_s !== this.cfg.max_age_s;
    const rangeChanged = (cfg.auto_range !== false) !== this.autoRange();
    this.cfg = cfg;
    if (dataChanged) {
      this.cols = new Map();
      this.tier = null;
      this.zoom = null;
      this.first = null;
      this.refresh(true).catch(() => {});
    } else if (rangeChanged) {
      this.first = null;
      this.refresh(true).catch(() => {});
    } else if (!this.collapsed) {
      this.draw();
    }
  }

  autoRange() {
    return this.cfg.auto_range !== false;
  }

  async setAutoRange(on) {
    const prev = this.autoRange();
    if (on === prev) return;
    this.savingAuto = on;
    this.cfg = { ...this.cfg, auto_range: on };
    this.first = null;
    this.refresh(true).catch(() => {});
    try {
      await api(`/api/logger/charts/${encodeURIComponent(this.cfg.id)}/options`, { method: 'PUT', body: { auto_range: on } });
      const c = S.logger?.charts.find((x) => x.id === this.cfg.id);
      if (c) c.auto_range = on;
    } catch (e) {
      toast(`Could not save auto range: ${e.message}`, 'error');
      this.cfg = { ...this.cfg, auto_range: prev };
      this.first = null;
      this.refresh(true).catch(() => {});
    } finally {
      this.savingAuto = null;
    }
  }

  setCollapsed(collapsed) {
    this.collapsed = collapsed;
    prefs.set(`chart.${this.cfg.id}.collapsed`, collapsed);
    this._paintCollapsed();
    if (!collapsed) this.refresh(true).catch(() => {}); // nothing is fetched while collapsed
  }

  _paintCollapsed() {
    this.el.classList.toggle('collapsed', this.collapsed);
    this.collapseBtn.setAttribute('aria-expanded', String(!this.collapsed));
    const label = this.collapsed ? 'Expand chart' : 'Collapse chart';
    this.collapseBtn.title = label;
    this.collapseBtn.setAttribute('aria-label', label);
  }

  meta() {
    return this.cfg.series.map((s) => {
      const c = S.catalog.get(s.id);
      const name = c?.name ?? s.id;
      const node = c?.node ?? '';
      return { id: s.id, color: s.color, name, node, units: c?.units ?? this.cfg.units, label: node ? `${node} · ${name}` : name };
    });
  }

  span() {
    return S.range ? Math.min(S.range, this.cfg.max_age_s) : this.cfg.max_age_s;
  }

  window() {
    if (this.zoom) return this.zoom;
    const now = serverNow();
    let start = now - this.span();
    // Auto range: begin at the oldest stored sample (the gateway clips the
    // same way, so the resolution suits the data actually held)
    if (this.autoRange() && this.first != null) start = Math.max(start, Math.min(this.first, now - AUTO_MIN_SPAN_S));
    return [start, now];
  }

  async refresh(full = false) {
    if (this.collapsed) return; // fetched (in full) when expanded
    // A full refresh means the window, zoom or series changed: any response
    // still in flight is for the old view and must not be drawn or merged.
    if (full) this.version = (this.version || 0) + 1;
    if (this.busy) {
      if (full) this.pendingFull = true; // re-run once the in-flight request ends
      return;
    }
    const version = this.version || 0;
    const auto = this.autoRange() && !this.zoom;
    const [start, end] = this.window();
    let since = null;
    if (!full && !this.zoom && this.tier != null && Date.now() - this.lastFull < FULL_REFRESH_MS) {
      for (const d of this.cols.values()) if (d.t.length) since = Math.max(since ?? -Infinity, d.t[d.t.length - 1]);
    }
    const q = new URLSearchParams();
    for (const s of this.cfg.series) q.append('s', s.id);
    q.set('start', start.toFixed(3));
    q.set('end', end.toFixed(3));
    q.set('points', String(this.chart.pointsBudget()));
    if (auto) q.set('auto', '1');
    if (since != null) {
      q.set('tier', String(this.tier));
      q.set('since', String(since));
    }

    this.busy = true;
    if (since == null) this.chart.setLoading(true);
    try {
      const res = await api(`/api/logger/data?${q}`);
      S.offset = res.now - Date.now() / 1000;
      if (version !== (this.version || 0)) return; // stale: a full refresh follows
      if (auto) this.first = res.first ?? null;
      if (since != null && res.tier === this.tier) {
        this._merge(res, since, start);
      } else {
        this.tier = res.tier;
        this.cols = new Map(Object.entries(res.series));
        if (!this.zoom) this.lastFull = Date.now();
      }
      this.draw();
    } catch (e) {
      if (since == null && version === (this.version || 0)) this.chart.setEmpty(['Could not load data', e.message]);
      throw e;
    } finally {
      this.busy = false;
      this.chart.setLoading(false);
      if (this.pendingFull) {
        this.pendingFull = false;
        this.refresh(true).catch(() => {});
      }
    }
  }

  _merge(res, since, start) {
    const keys = this.tier ? ['t', 'avg', 'min', 'max'] : ['t', 'v'];
    const minT = this.tier ? start - this.tier : start;
    for (const s of this.cfg.series) {
      const inc = res.series[s.id];
      const d = this.cols.get(s.id);
      if (!d) {
        if (inc) this.cols.set(s.id, inc);
        continue;
      }
      if (inc && inc.t.length) {
        // Replace everything from the first returned row on (the last bucket
        // may have grown; raw rows at the boundary are re-sent)
        const from = Math.min(since, inc.t[0]);
        let cut = d.t.length;
        while (cut > 0 && d.t[cut - 1] >= from) cut--;
        for (const k of keys) {
          d[k].length = cut;
          const src = inc[k];
          for (let i = 0; i < src.length; i++) d[k].push(src[i]);
        }
      }
      let drop = 0;
      while (drop < d.t.length && d.t[drop] < minT) drop++;
      if (drop) for (const k of keys) d[k].splice(0, drop);
    }
  }

  draw() {
    const meta = this.meta();
    const tier = this.tier || 0;
    const range = this.window();
    const data = buildData(meta, this.cols, tier, serverNow());
    this.chart.render(meta, tier, data, range);
    this.chart.setEmpty(data[0].length ? null : this._emptyMessage());
    this.renderHeader();
    this.renderLegend();
  }

  _emptyMessage() {
    if (this.zoom) return ['Nothing recorded here', 'Double-click or use Reset zoom to go back.'];
    if (!S.logger?.recording) return ['Recording is off', 'Switch recording on to capture these series.'];
    const quiet = this.cfg.series.every((s) => !S.catalog.get(s.id)?.live);
    if (quiet) return ['Waiting for data', 'These series have not been heard since the gateway started.'];
    return ['Recording…', `Samples appear as they arrive (written every ${S.logger.recorder.flush_interval_s}s).`];
  }

  renderHeader() {
    this.titleEl.textContent = this.cfg.title;
    const parts = [h('span', { class: 'badge' }, this.cfg.units || 'no units'), h('span', null, `keeps ${fmtSpan(this.cfg.max_age_s)}`)];
    if (!this.zoom && this.autoRange() && this.first != null && !this.collapsed) {
      const [a, b] = this.window();
      if (b - a < this.span() - 60) {
        parts.push(h('span', { class: 'sep' }), h('span', { title: 'Auto range: fitted to the data this chart holds' }, `showing ${fmtDur(b - a)}`));
      }
    }
    if (this.tier != null && !this.collapsed) {
      parts.push(h('span', { class: 'sep' }), h('span', { title: 'Resolution chosen for this window' }, TIER_LABEL[this.tier] ?? ''));
    }
    if (this.zoom) parts.push(h('span', { class: 'sep' }), h('span', null, `zoomed to ${fmtDur(this.zoom[1] - this.zoom[0])}`));
    this.metaEl.replaceChildren(...parts);
    this.zoomChip.hidden = !this.zoom;
  }

  renderLegend() {
    const items = this.meta().map((m) => {
      const d = this.cols.get(m.id);
      const col = d ? (this.tier ? d.avg : d.v) : null;
      const last = col && col.length ? col[col.length - 1] : null;
      const off = this.chart.hidden.has(m.id);
      return h(
        'button',
        {
          type: 'button',
          class: `legend-item${off ? ' off' : ''}`,
          title: `${m.id}\n${off ? 'Click to show' : 'Click to hide'}`,
          'aria-pressed': !off,
          onclick: () => {
            this.chart.setVisible(m.id, off);
            this.renderLegend();
          },
        },
        h('span', { class: 'legend-key', style: { background: `var(--series-${m.color + 1})` } }),
        h('span', { class: 'legend-name' }, m.node ? `${m.node} ` : '', h('b', null, m.name)),
        h('span', { class: 'legend-val' }, fmtNum(last), m.units ? h('small', null, m.units) : null),
      );
    });
    this.legendEl.replaceChildren(...items);
  }

  setZoom(range) {
    if (range[1] - range[0] < 2) return;
    this.zoom = range;
    this.renderHeader();
    this.refresh(true).catch(() => {});
  }

  resetZoom() {
    if (!this.zoom) return;
    this.zoom = null;
    this.refresh(true).catch(() => {});
  }

  download() {
    const [start, end] = this.window();
    const q = new URLSearchParams();
    for (const s of this.cfg.series) q.append('s', s.id);
    q.set('start', start.toFixed(3));
    q.set('end', end.toFixed(3));
    q.set('name', this.cfg.title);
    downloadCsv(q);
  }

  _menu(anchor) {
    const ids = [...S.views.keys()];
    const i = ids.indexOf(this.cfg.id);
    showMenu(anchor, [
      {
        label: 'Auto range',
        icon: 'fit',
        checked: this.autoRange(),
        title: `Fit the time axis to the data this chart holds, up to ${fmtSpan(this.cfg.max_age_s)}`,
        onToggle: (on) => this.setAutoRange(on),
      },
      'sep',
      { label: 'Move up', icon: 'up', disabled: i <= 0, onClick: () => moveChart(this.cfg.id, -1) },
      { label: 'Move down', icon: 'down', disabled: i >= ids.length - 1, onClick: () => moveChart(this.cfg.id, 1) },
      'sep',
      { label: 'Clear recorded data…', icon: 'reset', onClick: () => clearChartData(this) },
      { label: 'Delete chart…', icon: 'trash', danger: true, onClick: () => deleteChart(this) },
    ]);
  }

  destroy() {
    this.chart.destroy();
    this.el.remove();
  }
}

// ---------------------------------------------------------------------------
// State
// ---------------------------------------------------------------------------

async function loadState() {
  const st = await api('/api/logger');
  applyState(st);
}

function applyState(st) {
  if (S.pendingOrder) {
    // A reorder is still being saved: don't let a stale poll undo it
    const { ids, newRows } = S.pendingOrder;
    const rank = new Map(ids.map((id, i) => [id, i]));
    st.charts.sort((a, b) => (rank.get(a.id) ?? 1e9) - (rank.get(b.id) ?? 1e9));
    if (newRows) {
      const starts = new Set(newRows);
      for (const c of st.charts) c.new_row = starts.has(c.id);
    }
  }
  S.logger = st;
  S.offset = st.now - Date.now() / 1000;
  S.catalog = new Map(st.catalog.map((c) => [c.id, c]));
  renderRecorder();
  syncCharts();
}

function renderRecorder() {
  const st = S.logger;
  const toggle = $('#rec-toggle');
  if (!toggle.disabled) toggle.checked = st.enabled;
  $('#recorder').classList.toggle('on', st.recording);
  $('#rec-title').textContent = st.recording ? 'Recording' : st.enabled ? 'Recorder stopped' : 'Recording off';

  let sub;
  if (st.recording) {
    sub = st.storage.series
      ? `Logging ${st.storage.series} series on the gateway, written every ${st.recorder.flush_interval_s}s. Old samples roll off per chart.`
      : 'Recording, but no chart uses any series yet — add a chart to choose what to log.';
  } else if (st.enabled) {
    sub = 'Recording is switched on but the recorder is not running — check the gateway log.';
  } else if (st.charts.length) {
    sub = 'Charts show what is already stored. Turn on to capture new samples — no browser needed.';
  } else {
    sub = 'Add a chart to choose series, then turn this on. The gateway records in the background.';
  }
  $('#rec-sub').textContent = sub;
  $('#st-size').textContent = fmtBytes(st.storage.bytes);
  $('#st-series').textContent = String(st.storage.series);
  const flush = $('#st-flush');
  if (st.recorder.last_flush) setAge(flush, st.now - st.recorder.last_flush);
  else {
    delete flush.dataset.ageBase;
    flush.textContent = st.recording ? 'pending' : '—';
  }

  const err = $('#rec-error');
  const problem = st.recorder.last_error || (st.recorder.dropped ? `${st.recorder.dropped} samples dropped (write buffer full)` : null);
  err.hidden = !problem;
  if (problem) err.replaceChildren(icon('alert'), h('span', null, `Recorder problem: ${problem}`));

  $('#rec-pill').hidden = !st.recording;
}

function syncCharts() {
  const host = $('#charts');
  const cfgs = S.logger.charts;
  const seen = new Set();
  const dragging = chartSort?.active; // never shuffle cards under a drag
  cfgs.forEach((cfg, i) => {
    seen.add(cfg.id);
    let v = S.views.get(cfg.id);
    if (!v) {
      v = new ChartView(cfg);
      S.views.set(cfg.id, v);
      host.insertBefore(v.el, dragging ? null : host.children[i] || null);
      v.refresh(true).catch(() => {});
    } else {
      if (!dragging && host.children[i] !== v.el) host.insertBefore(v.el, host.children[i] || null);
      if (JSON.stringify(v.cfg) !== JSON.stringify(cfg)) v.setConfig(cfg);
      else {
        v.renderHeader();
        v.renderLegend();
      }
    }
    if (!dragging) {
      if (cfg.new_row) v.el.dataset.newRow = '1';
      else delete v.el.dataset.newRow;
    }
  });
  for (const [id, v] of S.views) {
    if (!seen.has(id) && !dragging) {
      v.destroy();
      S.views.delete(id);
    }
  }
  S.views = new Map(cfgs.map((c) => [c.id, S.views.get(c.id)]));
  if (!dragging) applyFlow([...S.views.values()].map((v) => v.el), PER_ROW);
  $('#empty').hidden = cfgs.length > 0;
  $('#add-chart').disabled = cfgs.length >= (S.logger.limits?.max_charts ?? 32);
}

async function refreshAll(full) {
  for (const v of S.views.values()) {
    if (!full && v.zoom) continue;
    try {
      await v.refresh(full);
    } catch {
      /* shown on the chart; keep going */
    }
  }
}

// ---------------------------------------------------------------------------
// Chart actions
// ---------------------------------------------------------------------------

function moveChart(id, dir) {
  const v = S.views.get(id);
  if (v) chartSort.move(v.el, dir); // trades places with its neighbour, then saves
}

async function saveChartOrder(ids, newRows = null) {
  const byId = new Map(S.logger.charts.map((c) => [c.id, c]));
  const starts = newRows ? new Set(newRows) : null;
  S.logger.charts = ids
    .map((x) => byId.get(x))
    .filter(Boolean)
    .map((c) => (starts ? { ...c, new_row: starts.has(c.id) } : c));
  S.pendingOrder = { ids, newRows };
  syncCharts();
  try {
    await api('/api/logger/order', { method: 'PUT', body: newRows ? { ids, new_rows: newRows } : { ids } });
  } catch (e) {
    toast(`Could not save chart order: ${e.message}`, 'error');
    S.pendingOrder = null;
    loadState();
  } finally {
    S.pendingOrder = null;
  }
}

async function deleteChart(v) {
  const ok = confirm(
    `Delete the chart “${v.cfg.title}”?\n\nIts recorded samples stay on the gateway until they age out. Use Storage to clear them now.`,
  );
  if (!ok) return;
  try {
    await api(`/api/logger/charts/${encodeURIComponent(v.cfg.id)}`, { method: 'DELETE' });
    toast('Chart deleted');
    await loadState();
  } catch (e) {
    toast(e.message, 'error');
  }
}

async function clearChartData(v) {
  const n = v.cfg.series.length;
  const ok = confirm(
    `Delete every recorded sample for the ${n} series in “${v.cfg.title}”?\n\nOther charts showing the same series lose that history too. This cannot be undone.`,
  );
  if (!ok) return;
  try {
    await api('/api/logger/purge', { method: 'POST', body: { series: v.cfg.series.map((s) => s.id) } });
    toast('Recorded data cleared');
    await refreshAll(true);
    await loadState();
  } catch (e) {
    toast(e.message, 'error');
  }
}

function downloadCsv(params) {
  try {
    params.set('tz', Intl.DateTimeFormat().resolvedOptions().timeZone || '');
  } catch {
    /* older browsers: offset only */
  }
  params.set('tzo', String(-new Date().getTimezoneOffset()));
  const a = h('a', { href: `/api/logger/export.csv?${params}`, download: '' });
  document.body.append(a);
  a.click();
  a.remove();
  toast('Preparing CSV download…');
}

// ---------------------------------------------------------------------------
// Chart editor
// ---------------------------------------------------------------------------

const ED = { editing: null, selected: [], colors: {}, retention: 604800, query: '' };

function openEditor(cfg = null) {
  if (!S.logger) return; // state not loaded yet
  ED.editing = cfg;
  ED.selected = cfg ? cfg.series.map((s) => s.id) : [];
  ED.colors = cfg ? Object.fromEntries(cfg.series.map((s) => [s.id, s.color])) : {};
  ED.retention = cfg ? cfg.max_age_s : prefs.get('logger.lastRetention', 604800);
  ED.query = '';
  $('#cd-heading').textContent = cfg ? 'Edit chart' : 'New chart';
  $('#cd-save').textContent = cfg ? 'Save changes' : 'Create chart';
  $('#cd-name').value = cfg ? cfg.title : '';
  $('#cd-search').value = '';
  $('#cd-error').hidden = true;
  renderPicker();
  renderRetention();
  $('#chart-dialog').showModal();
  $('#cd-series').scrollTop = 0;
}

function selectedUnits() {
  for (const id of ED.selected) {
    const c = S.catalog.get(id);
    if (c) return c.units;
  }
  return null;
}

/** Same rule as the server: keep existing colors, then lowest free slot. */
function previewColors() {
  const out = {};
  const used = new Set();
  for (const id of ED.selected) {
    const c = ED.colors[id];
    if (Number.isInteger(c) && !used.has(c)) {
      out[id] = c;
      used.add(c);
    }
  }
  for (const id of ED.selected) {
    if (out[id] != null) continue;
    let slot = 0;
    while (used.has(slot)) slot++;
    out[id] = slot;
    used.add(slot);
  }
  return out;
}

function toggleSeries(id, on) {
  if (on && !ED.selected.includes(id)) ED.selected.push(id);
  if (!on) {
    ED.selected = ED.selected.filter((x) => x !== id);
    delete ED.colors[id];
  }
  ED.colors = { ...ED.colors, ...previewColors() };
  renderPicker();
  updateEstimate();
}

function renderPicker() {
  const host = $('#cd-series');
  const scroll = host.scrollTop;
  const q = ED.query.toLowerCase();
  const units = selectedUnits();
  const colors = previewColors();
  const max = maxSeries();
  const now = S.logger.now;

  const items = [...S.catalog.values()].filter(
    (c) => !q || `${c.node} ${c.name} ${c.units} ${c.id}`.toLowerCase().includes(q),
  );
  const out = [];
  if (!items.length) {
    out.push(
      h(
        'div',
        { class: 'picker-empty' },
        S.catalog.size ? 'No series match your search.' : 'No series yet — they appear here once the gateway receives readings.',
      ),
    );
  }
  let group = null;
  for (const c of items) {
    const node = c.node || '—';
    if (node !== group) {
      group = node;
      out.push(h('div', { class: 'picker-group' }, node, c.local ? h('span', { class: 'badge' }, 'gateway') : null));
    }
    const sel = ED.selected.includes(c.id);
    const mismatch = !sel && units != null && c.units !== units;
    const full = !sel && ED.selected.length >= max;
    const disabled = mismatch || full;
    const sub = [
      c.value != null ? `${fmtNum(c.value)}${c.units ? ` ${c.units}` : ''}` : null,
      c.live ? (c.ts ? fmtAgo(now - c.ts) : null) : 'not heard since restart',
      c.interval_s ? `every ~${fmtDur(c.interval_s)}` : null,
    ]
      .filter(Boolean)
      .join(' · ');
    out.push(
      h(
        'label',
        {
          class: `pick${disabled ? ' disabled' : ''}`,
          title: mismatch ? `Different units (${c.units || 'none'}) — a chart has one y-axis` : full ? `A chart holds up to ${max} series` : c.id,
        },
        h('input', { type: 'checkbox', checked: sel, disabled, onchange: (e) => toggleSeries(c.id, e.target.checked) }),
        h('span', { class: 'pick-key', style: sel ? { background: `var(--series-${colors[c.id] + 1})` } : {} }),
        h('span', { class: 'pick-main' }, h('span', { class: 'pick-name' }, c.name), h('span', { class: 'pick-sub' }, sub || c.id)),
        h('span', { class: 'badge' }, c.units || '—'),
      ),
    );
  }
  host.replaceChildren(...out);
  host.scrollTop = scroll;

  $('#cd-count').textContent = `${ED.selected.length} of ${max} selected`;
  $('#cd-units').textContent = units != null
    ? `Showing on one axis in ${units || '(no units)'} — other units are disabled.`
    : 'Pick one or more series that share units.';
  const first = S.catalog.get(ED.selected[0]);
  $('#cd-name').placeholder = first ? first.name : 'e.g. Temperatures';
}

function renderRetention() {
  const seg = $('#cd-ret');
  seg.replaceChildren(
    ...RETENTIONS.map(([label, secs]) =>
      h(
        'button',
        {
          type: 'button',
          'aria-pressed': ED.retention === secs,
          onclick: () => {
            ED.retention = secs;
            renderRetention();
          },
        },
        label,
      ),
    ),
  );
  const unit = ED.retention % 86400 === 0 ? 86400 : 3600;
  $('#cd-ret-u').value = String(unit);
  $('#cd-ret-n').value = String(Number((ED.retention / unit).toFixed(2)));
  $('#cd-ret-label').textContent = fmtSpan(ED.retention);
  updateEstimate();
}

function readCustomRetention() {
  const n = Number($('#cd-ret-n').value);
  const unit = Number($('#cd-ret-u').value);
  if (!Number.isFinite(n) || n <= 0) return;
  const lim = S.logger.limits;
  ED.retention = Math.round(Math.min(lim.retention_max_s, Math.max(lim.retention_min_s, n * unit)));
  renderRetention();
}

function updateEstimate() {
  const el = $('#cd-estimate');
  if (!ED.selected.length) {
    el.textContent = 'Older samples roll off automatically once they pass this age.';
    return;
  }
  let samples = 0;
  let unknown = 0;
  for (const id of ED.selected) {
    const c = S.catalog.get(id);
    if (c?.interval_s) samples += ED.retention / c.interval_s;
    else unknown++;
  }
  const parts = [];
  if (samples) {
    parts.push(
      `≈ ${fmtBytes(samples * BYTES_PER_SAMPLE)} on the gateway at current report rates (${Math.round(samples).toLocaleString()} samples)`,
    );
  }
  if (unknown) parts.push(`${unknown} series with unknown rate`);
  parts.push('older samples roll off automatically');
  el.replaceChildren(icon('database'), parts.join(' · '));
}

async function saveEditor() {
  const err = $('#cd-error');
  err.hidden = true;
  if (!ED.selected.length) {
    err.textContent = 'Pick at least one series.';
    err.hidden = false;
    return;
  }
  const colors = previewColors();
  const body = {
    title: $('#cd-name').value.trim(),
    series: ED.selected.map((id) => ({ id, color: colors[id] })),
    max_age_s: ED.retention,
  };
  const btn = $('#cd-save');
  btn.disabled = true;
  try {
    if (ED.editing) {
      await api(`/api/logger/charts/${encodeURIComponent(ED.editing.id)}`, { method: 'PUT', body });
    } else {
      await api('/api/logger/charts', { method: 'POST', body });
    }
    prefs.set('logger.lastRetention', ED.retention);
    $('#chart-dialog').close();
    toast(
      ED.editing
        ? 'Chart updated'
        : S.logger.enabled
          ? 'Chart created — its series are being recorded'
          : 'Chart created — switch recording on to capture data',
    );
    await loadState();
  } catch (e) {
    err.textContent = e.message;
    err.hidden = false;
  } finally {
    btn.disabled = false;
  }
}

function initEditor() {
  const dlg = $('#chart-dialog');
  $('#cd-close').append(icon('x'));
  $('#cd-search-wrap').prepend(icon('search'));
  $('#cd-close').addEventListener('click', () => dlg.close());
  $('#cd-cancel').addEventListener('click', () => dlg.close());
  $('#cd-save').addEventListener('click', saveEditor);
  $('#cd-name').addEventListener('keydown', (e) => {
    if (e.key === 'Enter') saveEditor();
  });
  let t;
  $('#cd-search').addEventListener('input', (e) => {
    clearTimeout(t);
    t = setTimeout(() => {
      ED.query = e.target.value.trim();
      renderPicker();
    }, 80);
  });
  $('#cd-ret-n').addEventListener('change', readCustomRetention);
  $('#cd-ret-u').addEventListener('change', readCustomRetention);
  dlg.addEventListener('click', (e) => {
    if (e.target === dlg) dlg.close(); // backdrop
  });
}

// ---------------------------------------------------------------------------
// Storage dialog
// ---------------------------------------------------------------------------

async function openStorage() {
  const dlg = $('#storage-dialog');
  if (!dlg.open) dlg.showModal();
  const body = $('#sd-body');
  if (!body.childElementCount) body.append(h('div', { class: 'picker-empty' }, 'Loading…'));
  try {
    renderStorage(await api('/api/logger/storage'));
  } catch (e) {
    body.replaceChildren(h('div', { class: 'field-error' }, e.message));
  }
}

function renderStorage(st) {
  const now = serverNow();
  const rows = st.series.filter((s) => s.first_ts != null || s.charts.length);
  const summary = h(
    'div',
    { class: 'storage-summary' },
    h(
      'div',
      { class: 'mini-stat', title: st.disk_bytes != null ? `${fmtBytes(st.disk_bytes)} on disk, including SQLite's write log` : null },
      h('span', { class: 'mini-label' }, 'Stored'),
      h('span', { class: 'mini-value' }, fmtBytes(st.bytes)),
    ),
    h('div', { class: 'mini-stat' }, h('span', { class: 'mini-label' }, 'Series stored'), h('span', { class: 'mini-value' }, String(rows.length))),
  );
  const scroller = h('div', { class: 'scroll storage-scroll', 'data-fade-sticky': 'thead' });
  const table = rows.length
    ? h(
        'div',
        { class: 'storage-frame' },
        scroller,
      )
    : h('div', { class: 'picker-empty' }, 'Nothing stored yet.');
  if (rows.length) {
    scroller.append(h(
          'table',
          { class: 'table storage-table' },
          h('thead', null, h('tr', null, h('th', null, 'Series'), h('th', null, 'Units'), h('th', null, 'Keeps'), h('th', null, 'History'), h('th', null, 'Charts'), h('th'))),
          h(
            'tbody',
            null,
            rows.map((s) =>
              h(
                'tr',
                null,
                h('td', null, h('div', { class: 'name' }, s.name || s.id), h('div', { class: 'sub' }, s.node ? `${s.node} · ${s.id}` : s.id)),
                h('td', null, s.units || '—'),
                h('td', null, fmtSpan(s.retention_s)),
                h('td', { class: 'num' }, s.first_ts != null ? `${fmtDur(now - s.first_ts)}` : 'empty', s.recording ? h('div', { class: 'sub' }, 'recording') : null),
                h('td', null, s.charts.length ? s.charts.join(', ') : h('span', { class: 'muted' }, 'none — ages out')),
                h(
                  'td',
                  { class: 'r' },
                  s.first_ts != null
                    ? h('button', { class: 'btn btn-sm btn-danger', type: 'button', onclick: () => purgeSeries(s) }, 'Clear')
                    : null,
                ),
              ),
            ),
          ),
        ));
    scrollFade(scroller);
  }
  $('#sd-body').replaceChildren(summary, table, h('div', { class: 'storage-path' }, st.path));
}

async function purgeSeries(s) {
  if (!confirm(`Delete all recorded samples for “${s.node ? `${s.node} · ` : ''}${s.name}”? This cannot be undone.`)) return;
  try {
    await api('/api/logger/purge', { method: 'POST', body: { series: [s.id] } });
    toast('Series data cleared');
    await openStorage();
    refreshAll(true);
    loadState();
  } catch (e) {
    toast(e.message, 'error');
  }
}

async function purgeAll() {
  if (!confirm('Delete ALL recorded history on the gateway? Charts are kept. This cannot be undone.')) return;
  try {
    await api('/api/logger/purge', { method: 'POST', body: { all: true } });
    toast('All recorded data deleted');
    await openStorage();
    refreshAll(true);
    loadState();
  } catch (e) {
    toast(e.message, 'error');
  }
}

function initStorage() {
  const dlg = $('#storage-dialog');
  $('#sd-close').append(icon('x'));
  $('#sd-close').addEventListener('click', () => dlg.close());
  $('#sd-done').addEventListener('click', () => dlg.close());
  $('#sd-purge-all').addEventListener('click', purgeAll);
  $('#storage-btn').addEventListener('click', openStorage);
  dlg.addEventListener('click', (e) => {
    if (e.target === dlg) dlg.close();
  });
  $('#export-all').addEventListener('click', () => {
    const q = new URLSearchParams({ start: '0', end: serverNow().toFixed(3), layout: 'long', name: 'gateway-history' });
    downloadCsv(q);
  });
}

// ---------------------------------------------------------------------------
// Toolbar
// ---------------------------------------------------------------------------

function initToolbar() {
  const seg = $('#range-seg');
  const paint = () => {
    for (const b of seg.children) b.setAttribute('aria-pressed', String(Number(b.dataset.secs) === S.range));
  };
  for (const [label, secs] of RANGES) {
    seg.append(
      h(
        'button',
        {
          type: 'button',
          'data-secs': String(secs),
          title: secs ? `Last ${label}` : "Each chart's full history",
          onclick: () => {
            S.range = secs;
            prefs.set('logger.range', secs);
            paint();
            for (const v of S.views.values()) v.zoom = null;
            refreshAll(true);
          },
        },
        label,
      ),
    );
  }
  paint();

  const live = $('#live-btn');
  const paintLive = () => live.setAttribute('aria-pressed', String(S.live));
  live.addEventListener('click', () => {
    S.live = !S.live;
    prefs.set('logger.live', S.live);
    paintLive();
    if (S.live) livePoller.start();
    else livePoller.stop();
  });
  paintLive();

  for (const id of ['#add-chart', '#empty-add']) {
    $(id).append(icon('plus'), 'Add chart');
    $(id).addEventListener('click', () => openEditor(null));
  }
  $('#empty-icon').append(icon('chart'));

  $('#rec-toggle').addEventListener('change', async (e) => {
    const want = e.target.checked;
    e.target.disabled = true;
    try {
      const st = await api('/api/logger', { method: 'PUT', body: { enabled: want } });
      e.target.disabled = false;
      applyState(st);
      toast(want ? 'Recording on — the gateway logs in the background' : 'Recording off — stored history is kept');
      refreshAll(true);
    } catch (err) {
      e.target.disabled = false;
      e.target.checked = !want;
      toast(`Could not ${want ? 'start' : 'stop'} recording: ${err.message}`, 'error');
    }
  });
}

// ---------------------------------------------------------------------------

const livePoller = new Poller(() => refreshAll(false), LIVE_MS);
const statePoller = new Poller(loadState, STATE_MS);
const topPoller = new Poller(async () => renderTopbar(await api('/api/status')), 30000);

window.addEventListener('themechange', () => {
  for (const v of S.views.values()) v.chart.invalidate();
});

initTheme();
initToolbar();
initEditor();
initStorage();
initScrollFades();
chartSort = sortable($('#charts'), { item: '.chart-card', handle: '.drag-handle', rows: PER_ROW, onDrop: saveChartOrder });
startAgeTicker();

loadState()
  .catch((e) => toast(`Could not load logger: ${e.message}`, 'error'))
  .finally(() => {
    statePoller.start();
    topPoller.start();
    if (S.live) livePoller.start();
  });

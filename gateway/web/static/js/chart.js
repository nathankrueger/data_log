// Time-series chart built on uPlot (vendored, works offline).
//
// Raw tier: one 2px line per series.
// Aggregate tiers: the bucket average as the line plus a 13% min-max envelope,
// so spikes stay visible however far you zoom out.
//
// Gaps: `null` breaks a line (real dropout, e.g. a node out of range);
// `undefined` is an alignment hole between nodes reporting at different
// instants and is spanned (uPlot treats the two differently).

import { cssVar, fmtDateTime, fmtNum, fmtTick, h } from './common.js';

const fmtHM = (ts) => new Date(ts * 1000).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' });

export const TIER_LABEL = {
  0: 'raw samples',
  60: '1-min averages',
  300: '5-min averages',
  1800: '30-min averages',
  10800: '3-hour averages',
};

function hexA(hex, alpha) {
  const m = /^#?([0-9a-f]{6})$/i.exec(hex);
  if (!m) return hex;
  const n = parseInt(m[1], 16);
  return `rgba(${(n >> 16) & 255}, ${(n >> 8) & 255}, ${n & 255}, ${alpha})`;
}

function gapThreshold(t, tier) {
  if (tier) return tier * 2.5;
  const n = t.length;
  if (n < 4) return Infinity;
  const m = Math.min(n - 1, 300);
  const dts = new Float64Array(m);
  for (let i = 0; i < m; i++) dts[i] = t[n - 1 - i] - t[n - 2 - i];
  dts.sort();
  // A couple of missed packets keeps the line; a longer silence breaks it
  return Math.max(dts[m >> 1] * 3.5, 20);
}

function withGaps(d, tier, nowS) {
  const t = d.t;
  const src = tier ? [d.avg, d.max, d.min] : [d.v];
  const out = { x: [], c: src.map(() => []) };
  const gap = gapThreshold(t, tier);
  let prevX = null;
  for (let i = 0; i < t.length; i++) {
    // Buckets plot at their midpoint; the open bucket never past "now"
    const x = tier ? Math.max(t[i], Math.min(t[i] + tier / 2, nowS)) : t[i];
    if (i > 0 && t[i] - t[i - 1] > gap) {
      out.x.push((prevX + x) / 2);
      for (const c of out.c) c.push(null);
    }
    out.x.push(x);
    for (let k = 0; k < src.length; k++) out.c[k].push(src[k][i]);
    prevX = x;
  }
  return out;
}

/**
 * Build uPlot's aligned data table: [xs, ...columns]. Column order per series
 * is [value] for raw, [avg, max, min] for aggregates.
 */
export function buildData(series, cols, tier, nowS) {
  const ncols = tier ? 3 : 1;
  const parts = series.map((s) => {
    const d = cols.get(s.id);
    return d && d.t.length ? withGaps(d, tier, nowS) : { x: [], c: Array.from({ length: ncols }, () => []) };
  });
  if (parts.length === 1) return [parts[0].x, ...parts[0].c];

  const set = new Set();
  for (const p of parts) for (const x of p.x) set.add(x);
  const xs = Float64Array.from(set).sort();
  const pos = new Map();
  for (let i = 0; i < xs.length; i++) pos.set(xs[i], i);

  const data = [Array.from(xs)];
  for (const p of parts) {
    for (let k = 0; k < ncols; k++) {
      const col = new Array(xs.length).fill(undefined);
      const src = p.c[k];
      for (let i = 0; i < p.x.length; i++) col[pos.get(p.x[i])] = src[i];
      data.push(col);
    }
  }
  return data;
}

function axisSize(u, values, axisIdx, cycleNum) {
  const axis = u.axes[axisIdx];
  if (cycleNum > 1) return axis._size;
  let size = axis.ticks.size + axis.gap;
  const longest = (values || []).reduce((acc, v) => (v.length > acc.length ? v : acc), '');
  if (longest) {
    u.ctx.font = axis.font[0];
    size += u.ctx.measureText(longest).width / devicePixelRatio;
  }
  return Math.ceil(size) + 2;
}

export class TimeChart {
  /**
   * @param {HTMLElement} host  sized container (.plot)
   * @param {{onZoom?: (range: number[]) => void, onResetZoom?: () => void}} opts
   */
  constructor(host, opts = {}) {
    this.host = host;
    this.opts = opts;
    this.u = null;
    this.key = null;
    this.series = [];
    this.map = [];
    this.tier = 0;
    this.hidden = new Set();
    this.expect = null; // x-range we set ourselves (anything else is a user zoom)
    this.last = null; // {series, tier, data, range} for rebuilds

    this.tip = h('div', { class: 'tt', role: 'tooltip' });
    this.emptyEl = h('div', { class: 'plot-empty' });
    this.emptyEl.hidden = true;
    host.append(this.tip, this.emptyEl);

    let raf = 0;
    this.ro = new ResizeObserver(() => {
      cancelAnimationFrame(raf);
      raf = requestAnimationFrame(() => this._resize());
    });
    this.ro.observe(host);
  }

  get width() {
    return Math.max(200, Math.floor(this.host.clientWidth));
  }

  /** Points to request: about one per horizontal pixel. */
  pointsBudget() {
    return Math.max(100, Math.min(2000, this.width));
  }

  render(series, tier, data, range) {
    this.last = { series, tier, data, range };
    const key = JSON.stringify([series.map((s) => [s.id, s.color]), tier > 0]);
    if (!this.u || key !== this.key) {
      this._build(series, tier, data);
      this.key = key;
    }
    this.series = series;
    this.tier = tier;
    this.expect = range;
    this.u.batch(() => {
      this.u.setData(data, false);
      this.u.setScale('x', { min: range[0], max: range[1] });
    });
  }

  /** Rebuild with current theme colors (call on theme change). */
  invalidate() {
    this.key = null;
    if (this.last) {
      const { series, tier, data, range } = this.last;
      this.render(series, tier, data, range);
    }
  }

  setVisible(id, visible) {
    if (visible) this.hidden.delete(id);
    else this.hidden.add(id);
    const i = this.series.findIndex((s) => s.id === id);
    if (i < 0 || !this.u) return;
    const m = this.map[i];
    for (const idx of [m.avg, m.hi, m.lo]) if (idx != null) this.u.setSeries(idx, { show: visible });
  }

  setEmpty(message) {
    this.emptyEl.hidden = !message;
    if (message) this.emptyEl.replaceChildren(h('div', null, h('b', null, message[0]), message[1]));
  }

  setLoading(on) {
    this.host.classList.toggle('loading', on);
  }

  destroy() {
    this.ro.disconnect();
    this.u?.destroy();
    this.u = null;
  }

  // -------------------------------------------------------------------------

  _build(series, tier, data) {
    this.u?.destroy();
    const agg = tier > 0;
    const surface = cssVar('--surface') || '#fff';
    const text3 = cssVar('--text-3') || '#888';
    const grid = cssVar('--grid') || '#ddd';
    const font = `11px ${cssVar('--font') || 'system-ui, sans-serif'}`;

    const useries = [{}];
    const colors = [null];
    const bands = [];
    this.map = [];
    series.forEach((s) => {
      const color = cssVar(`--series-${s.color + 1}`) || '#2a78d6';
      const show = !this.hidden.has(s.id);
      const m = { avg: useries.length };
      useries.push({
        label: s.label,
        stroke: color,
        width: 2,
        spanGaps: false,
        show,
        points: { size: 8, width: 2, stroke: surface, fill: color },
      });
      colors.push(color);
      if (agg) {
        const helper = { stroke: 'rgba(0,0,0,0)', width: 0, spanGaps: false, show, class: 'band-helper', points: { show: false } };
        m.hi = useries.length;
        useries.push({ ...helper, label: `${s.label} max` });
        colors.push(color);
        m.lo = useries.length;
        useries.push({ ...helper, label: `${s.label} min` });
        colors.push(color);
        bands.push({ series: [m.hi, m.lo], fill: hexA(color, 0.13) });
      }
      this.map.push(m);
    });

    const axis = {
      stroke: text3,
      font,
      grid: { stroke: grid, width: 1 },
      ticks: { stroke: grid, width: 1, size: 4 },
    };

    const opts = {
      width: this.width,
      height: Math.max(120, this.host.clientHeight),
      padding: [12, 14, 0, 4],
      legend: { show: false },
      scales: { x: { time: true } },
      series: useries,
      bands,
      axes: [
        { ...axis, space: 70 },
        {
          ...axis,
          space: 30,
          size: axisSize,
          values: (u, splits, axisIdx, space, incr) => splits.map((v) => fmtTick(v, incr)),
        },
      ],
      // No cursor sync: the crosshair and readout belong to the hovered chart only
      cursor: {
        drag: { x: true, y: false, dist: 8 },
        points: {
          size: 9,
          width: 2,
          stroke: () => surface,
          fill: (u, si) => colors[si] || text3,
        },
        bind: {
          dblclick: () => (e) => {
            if (e.button === 0) this.opts.onResetZoom?.();
            return null;
          },
        },
      },
      hooks: {
        setCursor: [(u) => this._tooltip(u)],
        setScale: [(u, key) => key === 'x' && this._onScale(u)],
      },
    };

    this.u = new uPlot(opts, data, this.host);
    this.host.append(this.tip, this.emptyEl); // keep overlays above the canvas
    this._bindTouch();
  }

  _resize() {
    if (!this.u || !this.host.clientWidth) return; // hidden (collapsed): resize when shown
    const width = this.width;
    const height = Math.max(120, this.host.clientHeight);
    if (width !== this.u.width || height !== this.u.height) this.u.setSize({ width, height });
  }

  _onScale(u) {
    const { min, max } = u.scales.x;
    if (min == null || max == null) return;
    const e = this.expect;
    if (e && Math.abs(min - e[0]) < 0.5 && Math.abs(max - e[1]) < 0.5) return;
    this.expect = [min, max];
    this.opts.onZoom?.([min, max]);
  }

  _bindTouch() {
    // uPlot's cursor is mouse-driven; let a finger scrub the crosshair too.
    // Listeners are passive so vertical page scrolling still works.
    const over = this.u.over;
    const move = (e) => {
      const t = e.touches[0];
      if (!t || !this.u) return;
      const r = over.getBoundingClientRect();
      this.u.setCursor({ left: t.clientX - r.left, top: t.clientY - r.top });
    };
    over.addEventListener('touchstart', move, { passive: true });
    over.addEventListener('touchmove', move, { passive: true });
  }

  _tooltip(u) {
    const idx = u.cursor.idx;
    if (idx == null || !this.series.length || u.cursor.left < 0) {
      this.tip.classList.remove('show');
      return;
    }
    const x = u.data[0][idx];
    const tier = this.tier;
    const rows = [];
    this.series.forEach((s, i) => {
      if (this.hidden.has(s.id)) return;
      const m = this.map[i];
      const di = u.cursor.idxs?.[m.avg] ?? idx;
      const v = di == null ? null : u.data[m.avg][di];
      rows.push(
        h(
          'div',
          { class: 'tt-row' },
          h('span', { class: 'tt-key', style: { background: `var(--series-${s.color + 1})` } }),
          h('span', { class: 'tt-val' }, fmtNum(v), s.units ? h('small', null, s.units) : null),
          h('span', { class: 'tt-name' }, s.label),
          tier && v != null
            ? h('span', { class: 'tt-range' }, `range ${fmtNum(u.data[m.lo][di])} – ${fmtNum(u.data[m.hi][di])}`)
            : null,
        ),
      );
    });
    if (!rows.length) {
      this.tip.classList.remove('show');
      return;
    }

    let when;
    if (tier) {
      const start = Math.floor(x / tier) * tier;
      when = `${fmtDateTime(start, false)} – ${fmtHM(start + tier)}`;
    } else {
      when = fmtDateTime(x);
    }
    this.tip.replaceChildren(h('div', { class: 'tt-time' }, when), ...rows);
    this.tip.classList.add('show');

    const overRect = u.over.getBoundingClientRect();
    const hostRect = this.host.getBoundingClientRect();
    const px = overRect.left - hostRect.left + u.cursor.left;
    const tw = this.tip.offsetWidth;
    const hostW = this.host.clientWidth;
    let left = px + 14;
    if (left + tw > hostW - 4) left = px - 14 - tw;
    this.tip.style.left = `${Math.max(4, left)}px`;
  }
}

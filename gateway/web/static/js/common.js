// Shared helpers for the gateway UI: API, DOM, formatting, theme, polling.

export const $ = (sel, root = document) => root.querySelector(sel);
export const $$ = (sel, root = document) => Array.from(root.querySelectorAll(sel));

// ---------------------------------------------------------------------------
// DOM builder. Text children are inserted as text nodes (never parsed as HTML),
// so node ids / sensor names from the radio can't inject markup.
// ---------------------------------------------------------------------------

export function h(tag, props, ...children) {
  const el = document.createElement(tag);
  if (props) {
    for (const [k, v] of Object.entries(props)) {
      if (v == null) continue;
      if (k.startsWith('aria-') || k.startsWith('data-')) {
        el.setAttribute(k, String(v));
        continue;
      }
      if (v === false) continue;
      if (k === 'class') el.className = v;
      else if (k === 'style' && typeof v === 'object') {
        for (const [sk, sv] of Object.entries(v)) {
          if (sk.startsWith('--')) el.style.setProperty(sk, sv);
          else el.style[sk] = sv;
        }
      } else if (k === 'dataset') Object.assign(el.dataset, v);
      else if (k.startsWith('on') && typeof v === 'function') el.addEventListener(k.slice(2), v);
      else if (typeof v === 'boolean' || (k in el && typeof v !== 'string')) el[k] = v;
      else el.setAttribute(k, v);
    }
  }
  appendChildren(el, children);
  return el;
}

function appendChildren(el, children) {
  for (const c of children.flat(Infinity)) {
    if (c == null || c === false) continue;
    el.append(c instanceof Node ? c : String(c));
  }
}

// ---------------------------------------------------------------------------
// Icons (24px grid, stroked)
// ---------------------------------------------------------------------------

const ICONS = {
  plus: '<path d="M12 5v14M5 12h14"/>',
  download: '<path d="M12 4v11m0 0-4.5-4.5M12 15l4.5-4.5M5 19.5h14"/>',
  edit: '<path d="M4 20h4.5L19 9.5 14.5 5 4 15.5V20z"/><path d="m12.5 7 4.5 4.5"/>',
  trash: '<path d="M4 7h16M10 11v6M14 11v6M6 7l1 13h10l1-13M9 7V4h6v3"/>',
  more: '<circle cx="5" cy="12" r="1.4" fill="currentColor" stroke="none"/><circle cx="12" cy="12" r="1.4" fill="currentColor" stroke="none"/><circle cx="19" cy="12" r="1.4" fill="currentColor" stroke="none"/>',
  pause: '<path d="M8.5 5v14M15.5 5v14"/>',
  play: '<path d="M7 5.5v13l11-6.5z"/>',
  search: '<circle cx="11" cy="11" r="6.5"/><path d="m20 20-4.2-4.2"/>',
  sun: '<circle cx="12" cy="12" r="4"/><path d="M12 2.5v2M12 19.5v2M4.6 4.6 6 6M18 18l1.4 1.4M2.5 12h2M19.5 12h2M4.6 19.4 6 18M18 6l1.4-1.4"/>',
  moon: '<path d="M20 14.5A8.5 8.5 0 1 1 9.5 4a7 7 0 0 0 10.5 10.5z"/>',
  contrast: '<circle cx="12" cy="12" r="8.5"/><path d="M12 3.5v17a8.5 8.5 0 0 0 0-17z" fill="currentColor" stroke="none"/>',
  chevron: '<path d="m9 6 6 6-6 6"/>',
  up: '<path d="M12 19V5M5.5 11.5 12 5l6.5 6.5"/>',
  down: '<path d="M12 5v14M5.5 12.5 12 19l6.5-6.5"/>',
  x: '<path d="M6 6l12 12M18 6 6 18"/>',
  reset: '<path d="M3.5 12a8.5 8.5 0 1 0 2.6-6.1L3.5 8.5"/><path d="M3.5 3.5v5h5"/>',
  database: '<ellipse cx="12" cy="6" rx="7.5" ry="3"/><path d="M4.5 6v12c0 1.7 3.4 3 7.5 3s7.5-1.3 7.5-3V6M4.5 12c0 1.7 3.4 3 7.5 3s7.5-1.3 7.5-3"/>',
  pulse: '<path d="M3 12.5h4l2.5-7 5 14 2.5-7H21"/>',
  chart: '<path d="M4 4v16h16"/><path d="m7.5 14.5 4-4.5 3 3 5-6"/>',
  check: '<path d="m5 12.5 4.5 4.5L19 7.5"/>',
  fit: '<path d="M3.5 12h17M7 8l-4 4 4 4M17 8l4 4-4 4"/>',
  alert: '<circle cx="12" cy="12" r="8.5"/><path d="M12 7.5v5.5M12 16.5v.01"/>',
  antenna: '<path d="M12 12v9M8.5 21h7"/><circle cx="12" cy="9.5" r="2"/><path d="M7.8 5.3a6 6 0 0 0 0 8.4M16.2 5.3a6 6 0 0 1 0 8.4M5 2.5a10 10 0 0 0 0 14M19 2.5a10 10 0 0 1 0 14"/>',
  clock: '<circle cx="12" cy="12" r="8.5"/><path d="M12 7.5V12l3 2"/>',
  nodes: '<rect x="3.5" y="3.5" width="7" height="7" rx="1.5"/><rect x="13.5" y="3.5" width="7" height="7" rx="1.5"/><rect x="3.5" y="13.5" width="7" height="7" rx="1.5"/><rect x="13.5" y="13.5" width="7" height="7" rx="1.5"/>',
  sliders: '<path d="M4 7h10M18 7h2M4 17h4M12 17h8"/><circle cx="16" cy="7" r="2"/><circle cx="10" cy="17" r="2"/>',
  list: '<path d="M8 6h12M8 12h12M8 18h12M4 6h.01M4 12h.01M4 18h.01"/>',
  grip: '<circle cx="9" cy="6" r="1.5" fill="currentColor" stroke="none"/><circle cx="15" cy="6" r="1.5" fill="currentColor" stroke="none"/><circle cx="9" cy="12" r="1.5" fill="currentColor" stroke="none"/><circle cx="15" cy="12" r="1.5" fill="currentColor" stroke="none"/><circle cx="9" cy="18" r="1.5" fill="currentColor" stroke="none"/><circle cx="15" cy="18" r="1.5" fill="currentColor" stroke="none"/>',
};

export function icon(name, cls = '') {
  const svg = document.createElementNS('http://www.w3.org/2000/svg', 'svg');
  svg.setAttribute('viewBox', '0 0 24 24');
  svg.setAttribute('fill', 'none');
  svg.setAttribute('stroke', 'currentColor');
  svg.setAttribute('stroke-width', '2');
  svg.setAttribute('stroke-linecap', 'round');
  svg.setAttribute('stroke-linejoin', 'round');
  svg.setAttribute('aria-hidden', 'true');
  svg.setAttribute('class', `icon ${cls}`.trim());
  svg.innerHTML = ICONS[name] || '';
  return svg;
}

// ---------------------------------------------------------------------------
// Preferences (per-browser conveniences only; storage may be unavailable)
// ---------------------------------------------------------------------------

export const prefs = {
  get(key, fallback) {
    try {
      const raw = localStorage.getItem(`gw.${key}`);
      return raw == null ? fallback : JSON.parse(raw);
    } catch {
      return fallback;
    }
  },
  set(key, value) {
    try {
      localStorage.setItem(`gw.${key}`, JSON.stringify(value));
    } catch {
      /* private mode / storage disabled */
    }
  },
};

// ---------------------------------------------------------------------------
// API
// ---------------------------------------------------------------------------

export class ApiError extends Error {
  constructor(status, message, body) {
    super(message);
    this.status = status;
    this.body = body;
  }
}

let online = null;

function setConnection(ok) {
  if (ok === online) return;
  online = ok;
  const el = $('#conn');
  if (!el) return;
  el.dataset.state = ok ? 'online' : 'offline';
  const label = el.querySelector('.conn-label');
  if (label) label.textContent = ok ? 'Connected' : 'Offline';
  el.title = ok ? 'Gateway reachable' : 'Gateway unreachable - retrying';
}

export async function api(path, { method = 'GET', body, signal } = {}) {
  let resp;
  try {
    resp = await fetch(path, {
      method,
      headers: body !== undefined ? { 'Content-Type': 'application/json' } : undefined,
      body: body !== undefined ? JSON.stringify(body) : undefined,
      cache: 'no-store',
      signal,
    });
  } catch (e) {
    if (e.name === 'AbortError') throw e;
    setConnection(false);
    throw new ApiError(0, 'Gateway unreachable');
  }
  setConnection(true);
  const text = await resp.text();
  let data = null;
  if (text) {
    try {
      data = JSON.parse(text);
    } catch {
      data = null;
    }
  }
  if (!resp.ok) {
    let msg = `HTTP ${resp.status}`;
    if (data && typeof data === 'object') {
      msg = data.error === 'unavailable' && data.message ? data.message : data.error || data.message || msg;
    }
    throw new ApiError(resp.status, msg, data);
  }
  return data;
}

// ---------------------------------------------------------------------------
// Formatting
// ---------------------------------------------------------------------------

export function fmtNum(v) {
  if (v == null) return '—';
  if (typeof v !== 'number') return String(v);
  if (!Number.isFinite(v)) return '—';
  const a = Math.abs(v);
  let digits;
  if (Number.isInteger(v) || a >= 1e5) digits = 0;
  else if (a >= 100) digits = 1;
  else if (a >= 10) digits = 2;
  else if (a >= 1) digits = 3;
  else if (a === 0) digits = 0;
  else return Number(v.toPrecision(3)).toString();
  return v.toLocaleString(undefined, { maximumFractionDigits: digits, minimumFractionDigits: 0 });
}

/** Axis tick label with just enough decimals for the tick step. */
export function fmtTick(v, step) {
  if (v == null) return '';
  const digits = step > 0 && step < 1 ? Math.min(6, Math.ceil(-Math.log10(step) - 1e-9)) : 0;
  return v.toLocaleString(undefined, { minimumFractionDigits: digits, maximumFractionDigits: digits });
}

/** Compact duration: 42s, 3m 10s, 12m, 2h 5m, 3d 4h. */
export function fmtDur(seconds) {
  if (seconds == null || !Number.isFinite(seconds)) return '—';
  let s = Math.max(0, Math.round(seconds));
  if (s < 60) return `${s}s`;
  const m = Math.floor(s / 60);
  if (m < 10) return `${m}m ${s % 60}s`;
  if (m < 60) return `${m}m`;
  const hrs = Math.floor(m / 60);
  if (hrs < 24) return `${hrs}h ${m % 60}m`;
  const d = Math.floor(hrs / 24);
  return `${d}d ${hrs % 24}h`;
}

export function fmtAgo(seconds) {
  if (seconds == null || !Number.isFinite(seconds)) return 'never';
  return seconds < 2 ? 'just now' : `${fmtDur(seconds)} ago`;
}

/** Human span for retention settings: 6 hours, 3 days, 2 weeks. */
export function fmtSpan(seconds) {
  const plural = (n, unit) => `${n} ${unit}${n === 1 ? '' : 's'}`;
  if (seconds % 604800 === 0 && seconds >= 1209600) return plural(seconds / 604800, 'week');
  if (seconds % 86400 === 0) return plural(seconds / 86400, 'day');
  if (seconds % 3600 === 0) return plural(seconds / 3600, 'hour');
  if (seconds >= 86400) return `${(seconds / 86400).toFixed(1)} days`;
  if (seconds >= 3600) return `${(seconds / 3600).toFixed(1)} hours`;
  return plural(Math.round(seconds / 60), 'minute');
}

export function fmtBytes(n) {
  if (n == null) return '—';
  if (n < 1024) return `${n} B`;
  const units = ['KB', 'MB', 'GB'];
  let v = n / 1024;
  let i = 0;
  while (v >= 1024 && i < units.length - 1) {
    v /= 1024;
    i++;
  }
  return `${v < 10 ? v.toFixed(1) : Math.round(v)} ${units[i]}`;
}

export function fmtClock(ts) {
  return new Date(ts * 1000).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit', second: '2-digit' });
}

export function fmtDateTime(ts, withSeconds = true) {
  const d = new Date(ts * 1000);
  const now = new Date();
  const sameDay = d.toDateString() === now.toDateString();
  const time = d.toLocaleTimeString([], {
    hour: '2-digit',
    minute: '2-digit',
    ...(withSeconds ? { second: '2-digit' } : {}),
  });
  if (sameDay) return time;
  const date = d.toLocaleDateString([], { weekday: 'short', month: 'short', day: 'numeric' });
  return `${date}, ${time}`;
}

const BW_KHZ = { 0: 125, 1: 250, 2: 500 };
export const bwLabel = (code) => (code in BW_KHZ ? `${BW_KHZ[code]} kHz` : `bw ${code}`);

// ---------------------------------------------------------------------------
// Theme: auto -> light -> dark
// ---------------------------------------------------------------------------

const THEMES = ['auto', 'light', 'dark'];

export function initTheme() {
  const btn = $('#theme-btn');
  let mode = prefs.get('theme', 'auto');
  if (!THEMES.includes(mode)) mode = 'auto';

  const apply = (announce) => {
    const root = document.documentElement;
    if (mode === 'auto') delete root.dataset.theme;
    else root.dataset.theme = mode;
    if (btn) {
      btn.replaceChildren(icon(mode === 'auto' ? 'contrast' : mode === 'light' ? 'sun' : 'moon'));
      btn.title = `Theme: ${mode} (click to change)`;
      btn.setAttribute('aria-label', btn.title);
    }
    window.dispatchEvent(new Event('themechange'));
    if (announce) toast(`Theme: ${mode === 'auto' ? 'match system' : mode}`);
  };

  btn?.addEventListener('click', () => {
    mode = THEMES[(THEMES.indexOf(mode) + 1) % THEMES.length];
    prefs.set('theme', mode);
    apply(true);
  });
  window.matchMedia('(prefers-color-scheme: dark)').addEventListener('change', () => {
    if (mode === 'auto') window.dispatchEvent(new Event('themechange'));
  });
  apply(false);
}

export const cssVar = (name) => getComputedStyle(document.documentElement).getPropertyValue(name).trim();

// ---------------------------------------------------------------------------
// Polling: pauses while the tab is hidden, backs off on errors
// ---------------------------------------------------------------------------

export class Poller {
  constructor(fn, intervalMs) {
    this.fn = fn;
    this.interval = intervalMs;
    this.timer = null;
    this.running = false;
    this.busy = false;
    this.failures = 0;
    document.addEventListener('visibilitychange', () => {
      if (document.hidden) this._clear();
      else if (this.running) this.now();
    });
  }

  start() {
    this.running = true;
    this.now();
  }

  stop() {
    this.running = false;
    this._clear();
  }

  async now() {
    this._clear();
    if (!this.running || document.hidden || this.busy) return;
    this.busy = true;
    try {
      await this.fn();
      this.failures = 0;
    } catch (e) {
      this.failures++;
      if (!(e instanceof ApiError) || e.status) console.warn(e);
    } finally {
      this.busy = false;
    }
    this._schedule();
  }

  _schedule() {
    this._clear();
    if (!this.running || document.hidden) return;
    const delay = this.failures
      ? Math.min(30000, this.interval * 2 ** Math.min(this.failures, 4))
      : this.interval;
    this.timer = setTimeout(() => this.now(), delay);
  }

  _clear() {
    clearTimeout(this.timer);
    this.timer = null;
  }
}

// ---------------------------------------------------------------------------
// Toasts and menus
// ---------------------------------------------------------------------------

export function toast(message, kind = 'ok', ms = 3200) {
  let host = $('#toasts');
  if (!host) {
    host = h('div', { class: 'toasts', id: 'toasts', 'aria-live': 'polite' });
    document.body.append(host);
  }
  const el = h(
    'div',
    { class: `toast${kind === 'error' ? ' error' : ''}`, role: kind === 'error' ? 'alert' : 'status' },
    icon(kind === 'error' ? 'alert' : 'check'),
    h('span', null, message),
  );
  host.append(el);
  setTimeout(() => {
    el.classList.add('leaving');
    setTimeout(() => el.remove(), 250);
  }, ms);
}

let openMenu = null;

export function closeMenu() {
  if (openMenu) {
    openMenu.remove();
    openMenu = null;
  }
}

/** A menu row with a checkbox: toggles in place and leaves the menu open. */
function menuCheck(it) {
  const btn = h(
    'button',
    {
      type: 'button',
      role: 'menuitemcheckbox',
      class: 'menu-check',
      title: it.title,
      'aria-checked': String(!!it.checked),
      onclick: () => {
        const on = btn.getAttribute('aria-checked') !== 'true';
        btn.setAttribute('aria-checked', String(on));
        it.onToggle(on);
      },
    },
    it.icon ? icon(it.icon) : null,
    h('span', { class: 'menu-label' }, it.label),
    h('span', { class: 'checkbox', 'aria-hidden': 'true' }, icon('check')),
  );
  return btn;
}

/**
 * Show a popover menu under `anchor`. items: 'sep' |
 * {label, icon, onClick, danger, disabled} | {label, icon, checked, onToggle, title}
 */
export function showMenu(anchor, items) {
  const wasOpen = openMenu && openMenu.parentElement === anchor.parentElement;
  closeMenu();
  if (wasOpen) return;
  const menu = h(
    'div',
    { class: 'menu', role: 'menu' },
    items.map((it) =>
      it === 'sep'
        ? h('hr')
        : it.checked != null
          ? menuCheck(it)
          : h(
            'button',
            {
              type: 'button',
              role: 'menuitem',
              class: it.danger ? 'danger' : null,
              disabled: !!it.disabled,
              onclick: () => {
                closeMenu();
                it.onClick();
              },
            },
            it.icon ? icon(it.icon) : null,
            it.label,
          ),
    ),
  );
  anchor.parentElement.append(menu);
  openMenu = menu;
}

document.addEventListener('click', (e) => {
  if (openMenu && !openMenu.parentElement.contains(e.target)) closeMenu();
});
document.addEventListener('keydown', (e) => {
  if (e.key === 'Escape') closeMenu();
});

// ---------------------------------------------------------------------------
// Scroll-edge fades
//
// Content dissolves at an edge only while there is more to scroll that way.
// CSS does the masking ([data-fade]); this keeps .fade-start/.fade-end, the
// scrollbar gutter (--sb, left unmasked) and the sticky-header offset
// (--fade-offset) current. Updates are coalesced to one per frame.
// ---------------------------------------------------------------------------

const fadeQueue = new Set();
let fadeRaf = 0;

function queueFade(el) {
  fadeQueue.add(el);
  if (!fadeRaf) {
    fadeRaf = requestAnimationFrame(() => {
      fadeRaf = 0;
      for (const target of fadeQueue) updateFade(target);
      fadeQueue.clear();
    });
  }
}

function updateFade(el) {
  const f = el._fade;
  if (!f || !el.isConnected) return;
  let pos;
  let max;
  let gutter;
  if (f.axis === 'x') {
    pos = Math.abs(el.scrollLeft);
    max = el.scrollWidth - el.clientWidth;
    gutter = el.offsetHeight - el.clientHeight - f.borders[0] - f.borders[2];
  } else {
    pos = el.scrollTop;
    max = el.scrollHeight - el.clientHeight;
    gutter = el.offsetWidth - el.clientWidth - f.borders[1] - f.borders[3];
  }
  el.classList.toggle('fade-start', pos > 1);
  el.classList.toggle('fade-end', max - pos > 1);
  el.style.setProperty('--sb', `${Math.max(0, gutter)}px`);
  if (f.sticky) {
    const s = el.querySelector(f.sticky);
    el.style.setProperty('--fade-offset', `${s ? s.offsetHeight : 0}px`);
  }
}

/**
 * Give a scroll container edge fades.
 * axis: 'y' (default) or 'x'. sticky: selector of a sticky header the top
 * fade should start below. Both can also come from data-fade-axis /
 * data-fade-sticky attributes.
 */
export function scrollFade(el, { axis, sticky } = {}) {
  if (!el || el._fade) return;
  const cs = getComputedStyle(el);
  el._fade = {
    axis: axis || el.dataset.fadeAxis || 'y',
    sticky: sticky || el.dataset.fadeSticky || null,
    borders: ['Top', 'Right', 'Bottom', 'Left'].map((s) => parseFloat(cs[`border${s}Width`]) || 0),
  };
  el.dataset.fade = el._fade.axis;
  const queue = () => queueFade(el);
  el.addEventListener('scroll', queue, { passive: true });
  const ro = new ResizeObserver(queue);
  ro.observe(el);
  for (const child of el.children) ro.observe(child);
  // Content changes (rows added, filtered, a dialog opening) move scrollHeight
  new MutationObserver((records) => {
    for (const r of records) for (const n of r.addedNodes) if (n.parentElement === el && n.nodeType === 1) ro.observe(n);
    queue();
  }).observe(el, { childList: true, subtree: true, attributes: true, attributeFilter: ['hidden'] });
  queue();
}

/** Fade every scroll region on the page that's present at startup. */
export function initScrollFades(root = document) {
  for (const el of root.querySelectorAll('.scroll, .seg:not(.seg-wrap), [data-fade-axis]')) {
    scrollFade(el, el.classList.contains('seg') ? { axis: 'x' } : {});
  }
}

// ---------------------------------------------------------------------------
// Drag to reorder
//
// Pointer Events, so mouse, touch and pen behave the same. Dragging starts
// from a handle only (charts use drag-to-zoom and touch needs page scroll);
// the real element is lifted (a chart's canvas can't be cloned), a placeholder
// holds its slot, and siblings slide into place. Arrow keys on a focused
// handle move the item too. Escape cancels.
//
// A gesture must always end: its listeners live on window (not the handle,
// which can lose pointer capture), a move with no button down ends a release
// we never saw, losing focus cancels, and cleanup runs even if the settle
// animation never finishes.
//
// Rows mode (rows: N): items flow N to a row and an item flagged
// data-new-row starts a new one; an item alone in its row gets .wide (CSS
// spans it). Dropping in the band above/below a row gives the item a row of
// its own; dropping beside a card joins that row (a full row wraps its last
// card onto a row of its own). Rows below never reflow.
// ---------------------------------------------------------------------------

const reducedMotion = () => window.matchMedia('(prefers-reduced-motion: reduce)').matches;
// Rows mode: scales the up/down move that gives a card its own row (1 = from
// mid-row to within 56 px of the row's edge; 0.75 is a quarter less)
const NEW_ROW_TRAVEL = 0.75;

function setNewRow(el, on) {
  if (on) el.dataset.newRow = '1';
  else delete el.dataset.newRow;
}

/** Group items into rows: a row starts at an item flagged data-new-row, or when the previous row is full. */
export function flowRows(els, perRow) {
  const rows = [];
  let row = null;
  for (const el of els) {
    if (!row || row.length >= perRow || el.dataset.newRow === '1') rows.push((row = []));
    row.push(el);
  }
  return rows;
}

/** Mark items that are alone in their row .wide. */
export function applyFlow(els, perRow) {
  for (const row of flowRows(els, perRow)) for (const el of row) el.classList.toggle('wide', row.length === 1);
}

/**
 * @param {HTMLElement} container  parent of the sortable items
 * @param {{item: string, handle: string, rows?: number,
 *          onDrop: (ids: string[], newRows?: string[]) => void}} opts
 *        items need data-id; onDrop gets the new id order (only if the layout
 *        changed) and, in rows mode, the ids that start a row
 * @returns {{readonly active: boolean, move: (el: HTMLElement, delta: number) => void}}
 */
export function sortable(container, { item, handle, onDrop, rows: perRow = 0 }) {
  let drag = null; // the lifted item, while one is lifted (or settling)
  let gesture = null; // the press on a handle (lifts after a small move)

  const items = () => [...container.children].filter((c) => c.matches(item));
  // Layout order: while dragging, the placeholder stands in for the lifted item
  const flow = () =>
    drag ? [...container.children].filter((c) => c === drag.ph || (c !== drag.el && c.matches(item))) : items();
  const relayout = () => {
    if (perRow) applyFlow(flow(), perRow);
  };
  const layoutKey = () => {
    const els = items();
    if (!perRow) return els.map((c) => c.dataset.id).join('\n');
    return flowRows(els, perRow).map((r) => r.map((c) => c.dataset.id).join(',')).join('\n');
  };
  const report = () => {
    const els = items();
    if (perRow) onDrop(els.map((c) => c.dataset.id), flowRows(els, perRow).map((r) => r[0].dataset.id));
    else onDrop(els.map((c) => c.dataset.id));
  };
  const columns = () => getComputedStyle(container).gridTemplateColumns.split(' ').filter(Boolean).length;

  function flip(mutate) {
    const sibs = [...container.children].filter((c) => !drag || c !== drag.el);
    const before = new Map(sibs.map((c) => [c, c.getBoundingClientRect()]));
    mutate();
    if (reducedMotion()) return;
    for (const c of sibs) {
      const a = before.get(c);
      const b = c.getBoundingClientRect();
      const dx = a.left - b.left;
      const dy = a.top - b.top;
      if (dx || dy) {
        c.animate([{ transform: `translate(${dx}px, ${dy}px)` }, { transform: 'none' }], {
          duration: 220,
          easing: 'cubic-bezier(0.2, 0.8, 0.2, 1)',
        });
      }
    }
  }

  // --- Rows mode: moves of m (the placeholder, or an item moved by keyboard)

  /** Make every current row start explicit, so later rows never reflow. */
  function freeze() {
    for (const row of flowRows(flow(), perRow)) setNewRow(row[0], true);
  }
  const nextOf = (m) => {
    const els = flow();
    return els[els.indexOf(m) + 1] || null;
  };
  /** m leaves its slot: whatever followed keeps m's row start. */
  function leave(m) {
    const n = nextOf(m);
    if (m.dataset.newRow === '1' && n) setNewRow(n, true);
  }
  function placeBeside(m, e, after) {
    freeze();
    leave(m);
    if (after) {
      e.after(m);
      setNewRow(m, false);
    } else {
      e.before(m);
      setNewRow(m, e.dataset.newRow === '1');
      setNewRow(e, false);
    }
  }
  function placeOwnRow(m, edge, where) {
    freeze();
    if (edge !== m) {
      leave(m);
      if (where === 'above') edge.before(m);
      else edge.after(m);
    }
    setNewRow(m, true);
    const n = nextOf(m);
    if (n) setNewRow(n, true);
  }

  /**
   * Where the placeholder should go, or null to stay. (x, y) is the center of
   * the dragged card, not the pointer: the grip sits at the card's top edge,
   * so a sideways drag would otherwise always be in a row's "own row" band.
   * From the center, a new row takes a clear move up or down (~100 px for a
   * full chart) and anything else reorders within the row.
   */
  function rowTarget(x, y) {
    const ph = drag.ph;
    const rows = flowRows(flow(), perRow).map((els) => {
      const rects = els.map((c) => c.getBoundingClientRect());
      return { els, rects, top: Math.min(...rects.map((r) => r.top)), bottom: Math.max(...rects.map((r) => r.bottom)) };
    });
    if (!rows.length) return null;
    // The row under the card's center; a gap belongs to the nearer row
    let i = 0;
    while (i < rows.length - 1 && y > (rows[i].bottom + rows[i + 1].top) / 2) i++;
    const row = rows[i];
    // How far the card's center must move up or down from mid-row to get a
    // row of its own: ~100 px for a full chart, proportionally less if short
    const half = (row.bottom - row.top) / 2;
    const band = half - NEW_ROW_TRAVEL * (half - Math.min(56, half / 2));
    if (y < row.top + band) {
      const edge = row.els[0];
      return { key: `above:${edge === ph ? '' : edge.dataset.id}`, apply: () => placeOwnRow(ph, edge, 'above') };
    }
    if (y > row.bottom - band) {
      const edge = row.els[row.els.length - 1];
      return { key: `below:${edge === ph ? '' : edge.dataset.id}`, apply: () => placeOwnRow(ph, edge, 'below') };
    }
    // Beside the nearest card in the row (stacked when the grid is one column)
    const stacked = columns() < 2;
    let k = 0;
    let best = Infinity;
    row.rects.forEach((r, j) => {
      const d = stacked ? Math.max(r.top - y, 0, y - r.bottom) : Math.max(r.left - x, 0, x - r.right);
      if (d < best) {
        best = d;
        k = j;
      }
    });
    const e = row.els[k];
    if (e === ph) return null;
    const r = row.rects[k];
    const after = stacked ? y > (r.top + r.bottom) / 2 : x > (r.left + r.right) / 2;
    return { key: `${after ? 'after' : 'before'}:${e.dataset.id}`, apply: () => placeBeside(ph, e, after) };
  }

  /** Flat mode: the item under the pointer; the placeholder takes its slot. */
  function flatTarget(x, y) {
    for (const c of container.children) {
      if (c === drag.el || c === drag.ph || !c.matches(item)) continue;
      const r = c.getBoundingClientRect();
      if (x >= r.left && x <= r.right && y >= r.top && y <= r.bottom) {
        return {
          key: c.dataset.id,
          apply: () => (drag.ph.compareDocumentPosition(c) & Node.DOCUMENT_POSITION_FOLLOWING ? c.after(drag.ph) : c.before(drag.ph)),
        };
      }
    }
    return null;
  }

  // --- Lift, follow, settle

  function begin(el, x0, y0) {
    const start = layoutKey();
    const rect = el.getBoundingClientRect();
    const ph = document.createElement('div');
    ph.className = 'drag-placeholder';
    ph.style.height = `${rect.height}px`;
    ph.style.borderRadius = getComputedStyle(el).borderRadius;
    setNewRow(ph, el.dataset.newRow === '1');
    ph.classList.toggle('wide', el.classList.contains('wide'));
    drag = {
      el,
      ph,
      rect,
      width: rect.width,
      grabX: x0 - rect.left, // pointer offset within the card
      grabY: y0 - rect.top,
      start,
      startX: x0,
      startY: y0,
      x: x0,
      y: y0,
      moved: true,
      last: null,
      raf: 0,
      anim: null,
      origNext: el.nextElementSibling,
      flags: perRow ? new Map(items().map((c) => [c, c.dataset.newRow === '1'])) : null,
      savedStyle: el.getAttribute('style'),
    };
    el.after(ph);
    Object.assign(el.style, {
      position: 'fixed',
      left: `${rect.left}px`,
      top: `${rect.top}px`,
      width: `${rect.width}px`,
      // min-height: if it narrows to fit a slot, its contents may need more
      minHeight: `${rect.height}px`,
      margin: '0',
      // Scale and resize around the grab point, so the grip stays under the pointer
      transformOrigin: `${x0 - rect.left}px ${y0 - rect.top}px`,
    });
    el.classList.add('drag-lifted');
    document.body.classList.add('is-dragging');
    position();
    drag.raf = requestAnimationFrame(tick);
  }

  function position() {
    drag.el.style.transform = `translate(${drag.x - drag.startX}px, ${drag.y - drag.startY}px) scale(1.015)`;
  }

  /** The lifted item previews its destination's width (wide row vs. shared). */
  function fitLifted() {
    const w = drag.ph.getBoundingClientRect().width;
    if (w > 0 && Math.abs(w - drag.width) > 0.5) {
      drag.width = w;
      drag.el.style.width = `${w}px`;
    }
  }

  function tick() {
    if (!drag || drag.finishing) return;
    try {
      // Auto-scroll near the top/bottom of the viewport
      const edge = 72;
      let vy = 0;
      if (drag.y < edge) vy = -Math.ceil((edge - drag.y) / 5);
      else if (drag.y > window.innerHeight - edge) vy = Math.ceil((drag.y - (window.innerHeight - edge)) / 5);
      if (vy) {
        window.scrollBy(0, vy);
        drag.moved = true;
      }
      // Re-evaluate only after the pointer (or page) moved, so a reflow that
      // slides another item under a still pointer can't ping-pong
      if (drag.moved) {
        drag.moved = false;
        // Rows: the card's center (target width, so a resize in flight can't
        // shift it); flat lists: the pointer
        const t = perRow
          ? rowTarget(drag.x - drag.grabX + drag.width / 2, drag.y - drag.grabY + drag.rect.height / 2)
          : flatTarget(drag.x, drag.y);
        if (t && t.key !== drag.last) {
          flip(() => {
            t.apply();
            relayout();
          });
          fitLifted();
        }
        drag.last = t ? t.key : null;
      }
    } catch (err) {
      console.error('sortable', err);
    }
    drag.raf = requestAnimationFrame(tick);
  }

  function restore(d) {
    const next = d.origNext && d.origNext !== d.el && d.origNext.parentNode === container ? d.origNext : null;
    container.insertBefore(d.ph, next);
    if (d.flags) for (const [c, on] of d.flags) setNewRow(c, on);
    if (d.flags) setNewRow(d.ph, d.flags.get(d.el));
    relayout();
  }

  function finish(commit) {
    const d = drag;
    if (!d || d.finishing) return;
    d.finishing = true;
    cancelAnimationFrame(d.raf);
    window.removeEventListener('keydown', onKey, true);

    let done = false;
    const cleanup = () => {
      if (done) return;
      done = true;
      try {
        d.anim?.cancel();
        if (d.savedStyle == null) d.el.removeAttribute('style');
        else d.el.setAttribute('style', d.savedStyle);
        d.el.classList.remove('drag-lifted');
        if (perRow) setNewRow(d.el, d.ph.dataset.newRow === '1');
        if (d.ph.isConnected) d.ph.replaceWith(d.el);
        else if (!d.el.isConnected) container.append(d.el);
      } finally {
        d.ph.remove();
        document.body.classList.remove('is-dragging');
        drag = null;
      }
      relayout();
      if (commit && layoutKey() !== d.start) report();
    };

    try {
      if (!commit) {
        flip(() => restore(d));
        fitLifted(); // back to its original width
      }
      const to = d.ph.getBoundingClientRect();
      const dx = to.left - d.rect.left;
      const dy = to.top - d.rect.top;
      if (reducedMotion()) {
        cleanup();
        return;
      }
      d.anim = d.el.animate(
        [{ transform: d.el.style.transform }, { transform: `translate(${dx}px, ${dy}px) scale(1)` }],
        { duration: 180, easing: 'cubic-bezier(0.2, 0.8, 0.2, 1)', fill: 'forwards' },
      );
      d.anim.finished.then(cleanup, cleanup);
      setTimeout(cleanup, 600); // never left lifted if the animation can't finish
    } catch (err) {
      console.error('sortable', err);
      cleanup();
    }
  }

  // --- The pointer gesture

  function endGesture(commit) {
    const g = gesture;
    if (!g) return;
    gesture = null;
    window.removeEventListener('pointermove', onMove, true);
    window.removeEventListener('pointerup', onUp, true);
    window.removeEventListener('pointercancel', onCancel, true);
    window.removeEventListener('blur', onBlur);
    document.removeEventListener('visibilitychange', onHidden);
    try {
      if (g.handle.hasPointerCapture?.(g.id)) g.handle.releasePointerCapture(g.id);
    } catch {
      /* already released */
    }
    if (drag && !drag.finishing) finish(commit);
  }

  function onMove(ev) {
    const g = gesture;
    if (!g || ev.pointerId !== g.id) return;
    if (ev.pointerType === 'mouse' && ev.buttons === 0) {
      endGesture(true); // released where we couldn't see it (e.g. outside the window)
      return;
    }
    if (!g.lifted) {
      if (Math.hypot(ev.clientX - g.x0, ev.clientY - g.y0) < 4) return;
      g.lifted = true;
      begin(g.el, g.x0, g.y0);
      window.addEventListener('keydown', onKey, true);
    }
    if (drag && !drag.finishing) {
      drag.x = ev.clientX;
      drag.y = ev.clientY;
      drag.moved = true;
      position();
    }
  }
  const onUp = (ev) => {
    if (gesture && ev.pointerId === gesture.id) endGesture(true);
  };
  const onCancel = (ev) => {
    if (gesture && ev.pointerId === gesture.id) endGesture(false);
  };
  const onBlur = () => endGesture(false);
  const onHidden = () => {
    if (document.hidden) endGesture(false);
  };
  function onKey(e) {
    if (e.key === 'Escape' && drag) {
      e.preventDefault();
      endGesture(false);
    }
  }

  container.addEventListener('pointerdown', (e) => {
    if (e.pointerType === 'mouse' && e.button !== 0) return;
    const handleEl = e.target.closest(handle);
    if (!handleEl || !container.contains(handleEl)) return;
    const el = handleEl.closest(item);
    if (!el || el.parentElement !== container) return;
    if (gesture) endGesture(true); // a new press means we missed the last release
    if (drag) return; // still settling into place
    e.preventDefault();
    try {
      handleEl.setPointerCapture(e.pointerId);
    } catch {
      /* window listeners cover it */
    }
    // Lift only after a small movement so a click on the handle is a no-op
    gesture = { id: e.pointerId, el, handle: handleEl, x0: e.clientX, y0: e.clientY, lifted: false };
    window.addEventListener('pointermove', onMove, true);
    window.addEventListener('pointerup', onUp, true);
    window.addEventListener('pointercancel', onCancel, true);
    window.addEventListener('blur', onBlur);
    document.addEventListener('visibilitychange', onHidden);
  });

  /** Trade places with the previous (delta < 0) or next item, and save. In
   *  rows mode the two swap slots: row starts stay where they are. */
  function move(el, delta) {
    if (drag || gesture) return;
    const list = items();
    const i = list.indexOf(el);
    const j = i + Math.sign(delta);
    if (i < 0 || j < 0 || j >= list.length) return;
    const other = list[j];
    const start = layoutKey();
    flip(() => {
      if (j < i) other.before(el);
      else other.after(el);
      if (perRow) {
        const mine = el.dataset.newRow === '1';
        setNewRow(el, other.dataset.newRow === '1');
        setNewRow(other, mine);
      }
      relayout();
    });
    if (layoutKey() !== start) report();
  }

  // Keyboard: arrows on a focused handle move the item one place
  container.addEventListener('keydown', (e) => {
    const handleEl = e.target.closest?.(handle);
    if (!handleEl || drag) return;
    const el = handleEl.closest(item);
    if (!el || el.parentElement !== container) return;
    let delta = 0;
    if (e.key === 'ArrowUp' || e.key === 'ArrowLeft') delta = -1;
    else if (e.key === 'ArrowDown' || e.key === 'ArrowRight') delta = 1;
    else return;
    e.preventDefault();
    move(el, delta);
    handleEl.focus();
  });

  return {
    get active() {
      return drag != null || gesture != null;
    },
    move,
  };
}

/**
 * Collapsible cards: <details class="card" data-collapse-key="..."> with a
 * <summary> holding a .chev slot. Open/closed state is remembered per browser.
 * onToggle(open) runs on every change.
 */
export function initCollapsibles(onToggle = () => {}) {
  for (const card of document.querySelectorAll('details[data-collapse-key]')) {
    const key = `card.${card.dataset.collapseKey}.open`;
    card.querySelector('.chev')?.append(icon('chevron'));
    card.open = prefs.get(key, true);
    card.addEventListener('toggle', () => {
      prefs.set(key, card.open);
      onToggle(card, card.open);
    });
  }
}

/** A drag handle button for sortable items. */
export function dragHandle(label = 'Drag to reorder') {
  return h('button', { type: 'button', class: 'drag-handle', title: `${label} (or focus and use arrow keys)`, 'aria-label': label }, icon('grip'));
}

// ---------------------------------------------------------------------------
// Top bar (shared by every page)
// ---------------------------------------------------------------------------

export function renderTopbar(status) {
  const sub = $('#host');
  if (sub && status) {
    const parts = [status.hostname];
    if (status.node_id && status.node_id !== status.hostname) parts.push(status.node_id);
    if (status.uptime_seconds != null) parts.push(`up ${fmtDur(status.uptime_seconds)}`);
    sub.textContent = parts.join(' · ');
    document.title = `${status.node_id || status.hostname} · ${document.body.dataset.title || 'Gateway'}`;
  }
  const rec = $('#rec-pill');
  if (rec) rec.hidden = !status?.logger?.recording;
}

/** Ticks every second; elements with data-age-base/data-age-at show a live age. */
export function startAgeTicker() {
  const tick = () => {
    const now = performance.now();
    for (const el of $$('[data-age-base]')) {
      const age = Number(el.dataset.ageBase) + (now - Number(el.dataset.ageAt)) / 1000;
      el.textContent = (el.dataset.agePrefix || '') + fmtAgo(age);
    }
  };
  setInterval(tick, 1000);
}

export function setAge(el, ageSeconds, prefix = '') {
  el.dataset.ageBase = String(ageSeconds ?? NaN);
  el.dataset.ageAt = String(performance.now());
  el.dataset.agePrefix = prefix;
  el.textContent = prefix + fmtAgo(ageSeconds);
}

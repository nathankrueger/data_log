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

/** Show a popover menu under `anchor`. items: {label, icon, onClick, danger, disabled} | 'sep' */
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
// ---------------------------------------------------------------------------

const reducedMotion = () => window.matchMedia('(prefers-reduced-motion: reduce)').matches;

/**
 * @param {HTMLElement} container  parent of the sortable items
 * @param {{item: string, handle: string, onDrop: (ids: string[]) => void}} opts
 *        items need data-id; onDrop gets the new id order (only if it changed)
 * @returns {{readonly active: boolean}}
 */
export function sortable(container, { item, handle, onDrop }) {
  let drag = null;
  let pending = null;

  const items = () => [...container.children].filter((c) => c.matches(item));
  const order = () => items().map((c) => c.dataset.id);

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

  function begin(el, handleEl, e) {
    const rect = el.getBoundingClientRect();
    const ph = document.createElement('div');
    ph.className = 'drag-placeholder';
    ph.style.height = `${rect.height}px`;
    ph.style.borderRadius = getComputedStyle(el).borderRadius;
    drag = {
      el,
      handle: handleEl,
      ph,
      rect,
      startX: e.clientX,
      startY: e.clientY,
      x: e.clientX,
      y: e.clientY,
      moved: true,
      last: null,
      raf: 0,
      origNext: el.nextElementSibling,
      startOrder: order().join('\n'),
      savedStyle: el.getAttribute('style'),
    };
    el.after(ph);
    Object.assign(el.style, {
      position: 'fixed',
      left: `${rect.left}px`,
      top: `${rect.top}px`,
      width: `${rect.width}px`,
      height: `${rect.height}px`,
      margin: '0',
    });
    el.classList.add('drag-lifted');
    document.body.classList.add('is-dragging');
    position();
    drag.raf = requestAnimationFrame(tick);
  }

  function position() {
    drag.el.style.transform = `translate(${drag.x - drag.startX}px, ${drag.y - drag.startY}px) scale(1.015)`;
  }

  function targetAt(x, y) {
    for (const c of container.children) {
      if (c === drag.el || c === drag.ph || !c.matches(item)) continue;
      const r = c.getBoundingClientRect();
      if (x >= r.left && x <= r.right && y >= r.top && y <= r.bottom) return c;
    }
    return null;
  }

  function tick() {
    if (!drag) return;
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
      const t = targetAt(drag.x, drag.y);
      if (t && t !== drag.last) {
        const phBefore = drag.ph.compareDocumentPosition(t) & Node.DOCUMENT_POSITION_FOLLOWING;
        flip(() => (phBefore ? t.after(drag.ph) : t.before(drag.ph)));
      }
      drag.last = t;
    }
    drag.raf = requestAnimationFrame(tick);
  }

  function finish(commit) {
    const d = drag;
    if (!d || d.finishing) return;
    d.finishing = true;
    cancelAnimationFrame(d.raf);
    window.removeEventListener('keydown', onKey, true);
    if (!commit) flip(() => container.insertBefore(d.ph, d.origNext && d.origNext !== d.el ? d.origNext : null));

    const cleanup = () => {
      if (d.savedStyle == null) d.el.removeAttribute('style');
      else d.el.setAttribute('style', d.savedStyle);
      d.el.classList.remove('drag-lifted');
      d.ph.replaceWith(d.el);
      document.body.classList.remove('is-dragging');
      drag = null;
      const now = order();
      if (commit && now.join('\n') !== d.startOrder) onDrop(now);
    };

    const to = d.ph.getBoundingClientRect();
    const dx = to.left - d.rect.left;
    const dy = to.top - d.rect.top;
    if (reducedMotion()) {
      cleanup();
      return;
    }
    const anim = d.el.animate(
      [{ transform: d.el.style.transform }, { transform: `translate(${dx}px, ${dy}px) scale(1)` }],
      { duration: 180, easing: 'cubic-bezier(0.2, 0.8, 0.2, 1)', fill: 'forwards' },
    );
    anim.onfinish = () => {
      anim.cancel();
      cleanup();
    };
  }

  function onKey(e) {
    if (e.key === 'Escape' && drag) {
      e.preventDefault();
      finish(false);
    }
  }

  container.addEventListener('pointerdown', (e) => {
    if (drag || pending || (e.pointerType === 'mouse' && e.button !== 0)) return;
    const handleEl = e.target.closest(handle);
    if (!handleEl || !container.contains(handleEl)) return;
    const el = handleEl.closest(item);
    if (!el || el.parentElement !== container) return;
    e.preventDefault();
    handleEl.setPointerCapture(e.pointerId);
    // One gesture. Lift only after a small movement so a click on the handle
    // is a no-op; once lifted (or cancelled with Escape) it never re-lifts.
    const gesture = { id: e.pointerId, lifted: false };
    pending = gesture;

    const move = (ev) => {
      if (ev.pointerId !== gesture.id) return;
      if (!gesture.lifted) {
        if (Math.hypot(ev.clientX - e.clientX, ev.clientY - e.clientY) < 4) return;
        gesture.lifted = true;
        begin(el, handleEl, e);
        window.addEventListener('keydown', onKey, true);
      }
      if (drag && !drag.finishing) {
        drag.x = ev.clientX;
        drag.y = ev.clientY;
        drag.moved = true;
        position();
      }
    };
    const up = (ev) => {
      if (ev.pointerId !== gesture.id) return;
      handleEl.removeEventListener('pointermove', move);
      handleEl.removeEventListener('pointerup', up);
      handleEl.removeEventListener('pointercancel', up);
      pending = null;
      if (drag && !drag.finishing) finish(ev.type === 'pointerup');
    };
    handleEl.addEventListener('pointermove', move);
    handleEl.addEventListener('pointerup', up);
    handleEl.addEventListener('pointercancel', up);
  });

  // Keyboard: arrows on a focused handle move the item one place
  container.addEventListener('keydown', (e) => {
    const handleEl = e.target.closest?.(handle);
    if (!handleEl || drag) return;
    const el = handleEl.closest(item);
    if (!el || el.parentElement !== container) return;
    const list = items();
    const i = list.indexOf(el);
    let j = i;
    if (e.key === 'ArrowUp' || e.key === 'ArrowLeft') j = i - 1;
    else if (e.key === 'ArrowDown' || e.key === 'ArrowRight') j = i + 1;
    else return;
    e.preventDefault();
    if (j < 0 || j >= list.length) return;
    flip(() => (j < i ? list[j].before(el) : list[j].after(el)));
    handleEl.focus();
    onDrop(order());
  });

  return {
    get active() {
      return drag != null;
    },
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

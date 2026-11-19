// Live page: summary tiles, nodes, sensor feed, gateway parameters.

import {
  $,
  api,
  bwLabel,
  dragHandle,
  fmtClock,
  fmtDur,
  fmtBytes,
  fmtNum,
  h,
  icon,
  initCollapsibles,
  initScrollFades,
  initTheme,
  Poller,
  prefs,
  renderTopbar,
  setAge,
  sortable,
  startAgeTicker,
  toast,
} from './common.js';

const FEED_STOPS = [50, 100, 200, 500, 1000, 2000, 5000];
const STATUS_MS = 4000;
const FEED_MS = 2000;

const state = {
  status: null,
  feed: {
    epoch: null,
    seq: 0,
    size: 200,
    rows: [], // newest first: {r, tr}
    paused: false,
    unseen: 0, // rows added while scrolled away from the top
    filter: prefs.get('feed.filter', ''),
    nodes: new Set(prefs.get('feed.nodes', [])),
    sizeEditing: false,
    held: null, // readings the gateway holds (from /api/status), shown while collapsed
  },
  nodeCards: new Map(),
  nodeOrder: [], // user's drag order (ids), saved on the gateway
  nodeOrderSaving: false,
  paramRows: new Map(),
};

let nodeSort = null; // sortable controller for the nodes grid

// ---------------------------------------------------------------------------
// Freshness: compare a node's silence with its own report interval
// ---------------------------------------------------------------------------

function freshness(node) {
  const expected = node.interval_s || (node.local ? 10 : 60);
  const age = node.age_s;
  if (age <= Math.max(expected * 2.5, expected + 10)) return ['good', 'Live'];
  if (age <= expected * 6 + 30) return ['warn', 'Late'];
  return ['crit', 'Stale'];
}

function signalBars(rssi) {
  if (rssi == null) return 0;
  if (rssi >= -70) return 4;
  if (rssi >= -85) return 3;
  if (rssi >= -100) return 2;
  return 1;
}

// ---------------------------------------------------------------------------
// Status poll: tiles, nodes, params
// ---------------------------------------------------------------------------

async function pollStatus() {
  const s = await api('/api/status');
  state.status = s;
  if (!state.nodeOrderSaving && Array.isArray(s.node_order)) state.nodeOrder = s.node_order;
  renderTopbar(s);
  renderTiles(s);
  renderNodes(s.nodes || []);
  renderParams(s.params);
  renderChips(s.nodes || []);
  if (s.feed && !state.feed.sizeEditing && s.feed.size !== state.feed.size) {
    state.feed.size = s.feed.size;
    syncSizeSlider();
    trimFeed();
  }
  if (s.feed) {
    // A collapsed feed isn't polled; its badge shows what the gateway holds
    state.feed.held = s.feed.count;
    if (!$('#feed-card').open) updateFeedChrome();
  }
}

function renderTiles(s) {
  const nodes = s.nodes || [];
  const remote = nodes.filter((n) => !n.local);
  const counts = { good: 0, warn: 0, crit: 0 };
  for (const n of nodes) counts[freshness(n)[0]]++;

  $('#t-nodes').textContent = nodes.length ? `${counts.good} / ${nodes.length}` : '0';
  $('#t-nodes-sub').textContent = nodes.length
    ? [`${counts.good} live`, counts.warn && `${counts.warn} late`, counts.crit && `${counts.crit} stale`]
        .filter(Boolean)
        .join(' · ')
    : 'Waiting for the first packet';

  const last = remote.reduce((best, n) => (best == null || n.age_s < best.age_s ? n : best), null);
  if (last) {
    setAge($('#t-last'), last.age_s);
    $('#t-last-sub').textContent = `from ${last.id}${last.rssi != null ? ` · ${last.rssi} dBm` : ''}`;
  } else {
    const el = $('#t-last');
    delete el.dataset.ageBase;
    el.textContent = '—';
    $('#t-last-sub').textContent = s.lora ? 'No LoRa packets yet' : 'LoRa radio not running';
  }

  const p = s.params || {};
  const val = (k) => p[k]?.value;
  if (!s.lora) {
    $('#t-radio').textContent = 'Offline';
    $('#t-radio-sub').textContent = 'Transceiver failed to start';
  } else if (val('sf') != null) {
    $('#t-radio').textContent = `SF${val('sf')} · ${bwLabel(val('bw'))}`;
    const freqs = [val('n2g_freq'), val('g2n_freq')].filter((f) => f != null).map((f) => f.toFixed(1));
    $('#t-radio-sub').textContent = `TX ${val('txpwr')} dBm · ${freqs.join(' / ')} MHz`;
  }

  const lg = s.logger;
  if (lg) {
    $('#t-hist').textContent = lg.recording ? (lg.error ? 'Write errors' : 'Recording') : 'Off';
    if (lg.error) $('#t-hist').title = lg.error;
    $('#t-hist-sub').textContent = lg.series
      ? `${lg.series} series · ${fmtBytes(lg.bytes)} stored`
      : lg.charts
        ? `${lg.charts} charts · ${fmtBytes(lg.bytes)}`
        : 'Set up charts to log history';
  }
}

/** Nodes in the user's saved order; unknown/new ones keep server order after them. */
function orderedNodes(nodes) {
  const rank = new Map(state.nodeOrder.map((id, i) => [id, i]));
  return nodes
    .map((n, i) => [rank.has(n.id) ? rank.get(n.id) : state.nodeOrder.length + i, n])
    .sort((a, b) => a[0] - b[0])
    .map((x) => x[1]);
}

function renderNodes(nodes) {
  const host = $('#nodes');
  $('#nodes-empty').hidden = nodes.length > 0;
  nodes = orderedNodes(nodes);
  const dragging = nodeSort?.active;
  $('#nodes-sub').textContent = nodes.length
    ? `${nodes.reduce((a, n) => a + n.packets, 0).toLocaleString()} packets since start`
    : '';

  const seen = new Set();
  nodes.forEach((n, i) => {
    seen.add(n.id);
    let card = state.nodeCards.get(n.id);
    if (!card) {
      card = buildNodeCard(n);
      state.nodeCards.set(n.id, card);
    }
    // Never shuffle the grid under a drag in progress
    if (!dragging && host.children[i] !== card.el) host.insertBefore(card.el, host.children[i] || null);
    updateNodeCard(card, n);
  });
  for (const [id, card] of state.nodeCards) {
    if (!seen.has(id) && !dragging) {
      card.el.remove();
      state.nodeCards.delete(id);
    }
  }
}

function buildNodeCard(n) {
  const card = {
    packets: n.packets,
    status: h('span', { class: 'status' }),
    signal: h('span', { class: 'signal', 'data-bars': '0' }, h('i'), h('i'), h('i'), h('i')),
    rssi: h('span', { class: 'num' }),
    heard: h('span'),
    interval: h('span'),
    count: h('span', { class: 'num' }),
    readings: h('div', { class: 'node-readings' }),
  };
  card.el = h(
    'article',
    { class: 'node', 'data-id': n.id },
    dragHandle(`Drag to reorder ${n.id}`),
    h(
      'div',
      { class: 'node-head' },
      h('span', { class: 'node-name', title: n.id }, n.id),
      n.local ? h('span', { class: 'badge badge-outline' }, 'gateway') : null,
      card.status,
    ),
    h(
      'div',
      { class: 'node-meta' },
      n.local ? null : h('span', null, card.signal, card.rssi),
      card.heard,
      card.interval,
      card.count,
    ),
    card.readings,
  );
  return card;
}

function updateNodeCard(card, n) {
  const [level, label] = freshness(n);
  card.status.className = `status status-${level}`;
  card.status.textContent = label;
  card.el.classList.toggle('stale', level === 'crit');
  if (n.packets > card.packets) {
    card.el.classList.remove('pulse');
    void card.el.offsetWidth; // restart the animation
    card.el.classList.add('pulse');
  }
  card.packets = n.packets;

  card.signal.dataset.bars = String(signalBars(n.rssi));
  card.rssi.textContent = n.rssi != null ? `${n.rssi} dBm` : '— dBm';
  setAge(card.heard, n.age_s, 'heard ');
  card.interval.textContent = n.interval_s ? `every ~${fmtDur(n.interval_s)}` : '';
  card.count.textContent = `${n.packets.toLocaleString()} ${n.local ? 'reads' : 'pkts'}`;

  const rows = [];
  for (const r of n.readings) {
    rows.push(h('span', { class: 'k', title: r.id }, r.name));
    rows.push(h('span', { class: 'v' }, fmtNum(r.value), r.units ? h('small', null, r.units) : null));
  }
  card.readings.replaceChildren(...rows);
}

// ---------------------------------------------------------------------------
// Feed
// ---------------------------------------------------------------------------

const feedScroll = () => $('#feed-scroll');

async function pollFeed() {
  const f = state.feed;
  if (f.paused || !$('#feed-card').open) return; // collapsed: catch up on reopen
  const q = new URLSearchParams({ after: String(f.seq) });
  if (f.epoch != null) q.set('epoch', String(f.epoch));
  const data = await api(`/api/readings?${q}`);

  const readings = data.readings || [];
  const gap = readings.length && f.seq && readings[0].seq > f.seq + 1;
  if (data.reset || gap || data.epoch !== f.epoch) clearFeed();
  f.epoch = data.epoch;
  f.seq = data.seq;
  if (!state.feed.sizeEditing && data.size !== f.size) {
    f.size = data.size;
    syncSizeSlider();
  }
  if (readings.length) addFeedRows(readings);
}

function clearFeed() {
  state.feed.rows = [];
  state.feed.unseen = 0;
  $('#feed-body').replaceChildren();
  updateFeedChrome();
}

function feedRow(r, fresh) {
  return h(
    'tr',
    { class: fresh ? 'fresh' : null },
    h('td', { class: 'time', title: new Date(r.t * 1000).toLocaleString() }, fmtClock(r.t)),
    h('td', { class: 'feed-node' }, r.node),
    h('td', { title: r.id }, r.name),
    h('td', { class: 'value' }, fmtNum(r.value), r.units ? h('small', null, r.units) : null),
    h('td', { class: 'hide-sm' }, h('span', { class: `src ${r.src}` }, r.src === 'lora' ? 'LoRa' : 'local')),
    h('td', { class: 'rssi hide-sm' }, r.rssi != null ? `${r.rssi}` : '—'),
  );
}

function addFeedRows(readings) {
  const f = state.feed;
  const tbody = $('#feed-body');
  const scroller = feedScroll();
  const firstLoad = f.rows.length === 0;
  const atTop = scroller.scrollTop < 4;
  const before = scroller.scrollHeight;

  const frag = document.createDocumentFragment();
  const added = [];
  for (let i = readings.length - 1; i >= 0; i--) {
    const r = readings[i];
    const tr = feedRow(r, !firstLoad);
    tr.hidden = !matchesFilter(r);
    frag.append(tr);
    added.push({ r, tr });
  }
  tbody.insertBefore(frag, tbody.firstChild);
  // Height added above the viewport (measured before trimming the bottom)
  const grew = scroller.scrollHeight - before;
  f.rows = added.concat(f.rows);
  trimFeed();

  // Keep the reader's place when they've scrolled down into history
  if (!atTop && !firstLoad) {
    scroller.scrollTop += grew;
    f.unseen += added.filter((x) => !x.tr.hidden).length;
  }
  updateFeedChrome();
}

function trimFeed() {
  const f = state.feed;
  while (f.rows.length > f.size) f.rows.pop().tr.remove();
}

function matchesFilter(r) {
  const f = state.feed;
  if (f.nodes.size && !f.nodes.has(r.node)) return false;
  if (!f.filter) return true;
  const q = f.filter.toLowerCase();
  return r.node.toLowerCase().includes(q) || r.name.toLowerCase().includes(q) || r.id.includes(q);
}

function applyFilter() {
  for (const row of state.feed.rows) row.tr.hidden = !matchesFilter(row.r);
  updateFeedChrome();
}

function updateFeedChrome() {
  const f = state.feed;
  const visible = f.nodes.size || f.filter ? f.rows.filter((x) => !x.tr.hidden).length : f.rows.length;
  if (!$('#feed-card').open && f.held != null) {
    $('#feed-count').textContent = `${f.held} / ${f.size}`;
  } else {
    $('#feed-count').textContent = f.nodes.size || f.filter
      ? `${visible} of ${f.rows.length}`
      : `${f.rows.length} / ${f.size}`;
  }
  const empty = $('#feed-empty');
  empty.hidden = visible > 0;
  empty.textContent = f.rows.length ? 'No readings match the filter' : 'Waiting for readings…';

  const pill = $('#feed-new');
  pill.hidden = f.unseen === 0;
  pill.replaceChildren(icon('up'), `${f.unseen} new`);

  const pause = $('#feed-pause');
  pause.setAttribute('aria-pressed', String(f.paused));
  pause.replaceChildren(icon(f.paused ? 'play' : 'pause'), f.paused ? 'Resume' : 'Pause');
}

function renderChips(nodes) {
  const host = $('#feed-chips');
  const ids = nodes.map((n) => n.id);
  const key = ids.join('\n');
  if (host.dataset.key === key) {
    for (const chip of host.children) chip.setAttribute('aria-pressed', String(state.feed.nodes.has(chip.dataset.node)));
    return;
  }
  host.dataset.key = key;
  host.replaceChildren(
    ...ids.map((id) =>
      h(
        'button',
        {
          type: 'button',
          class: 'chip',
          'data-node': id,
          'aria-pressed': state.feed.nodes.has(id),
          onclick: () => {
            const set = state.feed.nodes;
            if (set.has(id)) set.delete(id);
            else set.add(id);
            prefs.set('feed.nodes', [...set]);
            renderChips(state.status?.nodes || []);
            applyFilter();
          },
        },
        id,
      ),
    ),
  );
  host.hidden = ids.length < 2;
}

function syncSizeSlider() {
  const slider = $('#feed-size');
  let idx = FEED_STOPS.findIndex((v) => v >= state.feed.size);
  if (idx < 0) idx = FEED_STOPS.length - 1;
  slider.value = String(idx);
  paintSlider(slider);
  $('#feed-size-out').textContent = state.feed.size.toLocaleString();
}

function paintSlider(slider) {
  const pct = (Number(slider.value) / Number(slider.max)) * 100;
  slider.style.setProperty('--pct', `${pct}%`);
}

function initFeedControls() {
  const filter = $('#feed-filter');
  $('#feed-search-wrap').prepend(icon('search'));
  filter.value = state.feed.filter;
  let t;
  filter.addEventListener('input', () => {
    clearTimeout(t);
    t = setTimeout(() => {
      state.feed.filter = filter.value.trim();
      prefs.set('feed.filter', state.feed.filter);
      applyFilter();
    }, 120);
  });

  $('#feed-pause').addEventListener('click', () => {
    state.feed.paused = !state.feed.paused;
    updateFeedChrome();
    if (!state.feed.paused) feedPoller.now();
  });

  $('#feed-new').addEventListener('click', () => {
    feedScroll().scrollTo({ top: 0, behavior: 'smooth' });
  });
  feedScroll().addEventListener(
    'scroll',
    () => {
      if (feedScroll().scrollTop < 4 && state.feed.unseen) {
        state.feed.unseen = 0;
        updateFeedChrome();
      }
    },
    { passive: true },
  );

  const slider = $('#feed-size');
  slider.addEventListener('input', () => {
    state.feed.sizeEditing = true;
    paintSlider(slider);
    $('#feed-size-out').textContent = FEED_STOPS[Number(slider.value)].toLocaleString();
  });
  slider.addEventListener('change', async () => {
    const size = FEED_STOPS[Number(slider.value)];
    try {
      const res = await api('/api/feed', { method: 'PUT', body: { size } });
      state.feed.size = res.size;
      trimFeed();
      updateFeedChrome();
      if (state.feed.rows.length < res.size) {
        // Grew: fetch the older readings the gateway retained
        state.feed.seq = 0;
        await feedPoller.now();
      }
    } catch (e) {
      toast(`Could not resize feed: ${e.message}`, 'error');
      syncSizeSlider();
    } finally {
      state.feed.sizeEditing = false;
    }
  });
  syncSizeSlider();
  updateFeedChrome();
}

// ---------------------------------------------------------------------------
// Gateway parameters
// ---------------------------------------------------------------------------

function renderParams(params) {
  const tbody = $('#params-body');
  if (!params) {
    if (!tbody.dataset.empty) {
      tbody.dataset.empty = '1';
      tbody.replaceChildren(h('tr', null, h('td', { colspan: '5', class: 'feed-empty' }, 'Parameters unavailable')));
    }
    return;
  }
  if (tbody.dataset.empty) {
    delete tbody.dataset.empty;
    tbody.replaceChildren();
  }
  const names = Object.keys(params);
  $('#params-sub').textContent = `${names.length} parameters`;
  for (const name of names) {
    let row = state.paramRows.get(name);
    if (!row) {
      row = buildParamRow(name, params[name]);
      state.paramRows.set(name, row);
      tbody.append(row.tr);
    }
    updateParamRow(row, params[name]);
  }
}

function buildParamRow(name, info) {
  const row = { name, info };
  if (info.writable) {
    row.input = h('input', {
      class: 'input',
      type: info.type === 'str' ? 'text' : 'number',
      step: info.type === 'float' ? 'any' : '1',
      min: info.min ?? null,
      max: info.max ?? null,
      'aria-label': name,
    });
    row.apply = h('button', { class: 'btn btn-sm', type: 'button', disabled: true }, 'Apply');
    row.input.addEventListener('input', () => markDirty(row));
    row.input.addEventListener('keydown', (e) => {
      if (e.key === 'Enter') applyParam(row);
      if (e.key === 'Escape') {
        row.input.value = String(row.info.value);
        markDirty(row);
      }
    });
    row.apply.addEventListener('click', () => applyParam(row));
  }
  const range = info.min != null && info.max != null ? `${info.min} – ${info.max}` : '—';
  const mode = info.writable ? (info.staged ? 'staged' : 'immediate') : 'read-only';
  const badge = info.writable ? (info.staged ? 'badge badge-warn' : 'badge badge-accent') : 'badge';
  row.ro = h('span', { class: 'ro' });
  row.tr = h(
    'tr',
    null,
    h('td', null, name),
    h('td', null, info.writable ? row.input : row.ro),
    h('td', { class: 'num dim' }, range),
    h('td', null, h('span', { class: badge }, mode)),
    h('td', { class: 'r' }, info.writable ? row.apply : null),
  );
  return row;
}

function updateParamRow(row, info) {
  row.info = info;
  if (!info.writable) {
    row.ro.textContent = String(info.value);
    return;
  }
  const editing = document.activeElement === row.input || row.input.classList.contains('dirty');
  if (!editing) row.input.value = String(info.value);
}

function markDirty(row) {
  const dirty = row.input.value !== String(row.info.value);
  row.input.classList.toggle('dirty', dirty);
  row.apply.disabled = !dirty;
}

async function applyParam(row) {
  const { name, info } = row;
  const raw = row.input.value.trim();
  let value = raw;
  if (info.type !== 'str') {
    value = Number(raw);
    if (raw === '' || !Number.isFinite(value)) {
      toast(`${name}: enter a number`, 'error');
      return;
    }
  }
  row.apply.disabled = true;
  try {
    await api(`/gateway/param/${encodeURIComponent(name)}`, { method: 'PUT', body: { value } });
    if (info.staged) {
      const r = await api('/gateway/rcfg_radio', { method: 'POST', body: {} });
      if (r?.error) throw new Error(r.message || r.error);
    }
    await api('/gateway/savecfg', { method: 'POST', body: {} });
    row.input.classList.remove('dirty');
    row.input.blur();
    toast(`${name} set to ${raw} and saved`);
    statusPoller.now();
  } catch (e) {
    toast(`${name}: ${e.message}`, 'error');
    row.apply.disabled = false;
  }
}

async function saveNodeOrder(visible) {
  // Keep the saved position of nodes that aren't on screen right now
  const ids = visible.concat(state.nodeOrder.filter((id) => !visible.includes(id)));
  state.nodeOrder = ids;
  state.nodeOrderSaving = true;
  try {
    await api('/api/nodes/order', { method: 'PUT', body: { ids } });
  } catch (e) {
    toast(`Could not save node order: ${e.message}`, 'error');
  } finally {
    state.nodeOrderSaving = false;
  }
}

// ---------------------------------------------------------------------------

const statusPoller = new Poller(pollStatus, STATUS_MS);
const feedPoller = new Poller(pollFeed, FEED_MS);

initTheme();
$('#nodes-empty-icon').append(icon('antenna'));
initFeedControls();
initCollapsibles((card, open) => {
  if (card.id !== 'feed-card') return;
  updateFeedChrome();
  if (open) feedPoller.now();
});
initScrollFades();
nodeSort = sortable($('#nodes'), { item: '.node', handle: '.drag-handle', onDrop: saveNodeOrder });
startAgeTicker();
statusPoller.start();
feedPoller.start();

// Review UI. Design rules from .claude/skills/apple-design:
//   - respond on pointer-down, never only at the end
//   - springs animate from the *current* value, so they can be interrupted
//   - reduced motion gets a gentler equivalent, not nothing

const reduceMotion = matchMedia('(prefers-reduced-motion: reduce)').matches;

/** Critically damped spring (damping 1.0, response 0.35) — Apple's default for
 *  non-gestural UI. Animates from the element's live value so an interrupting
 *  call continues from where it is rather than jumping to a start pose. */
function spring(el, from, to, { response = 0.35, onFrame } = {}) {
  if (reduceMotion) { onFrame?.(to); return () => {}; }
  const stiffness = (2 * Math.PI / response) ** 2;
  const damping = 2 * (2 * Math.PI / response); // ratio 1.0
  let value = from, velocity = 0, raf = 0, last = performance.now();
  const step = (now) => {
    const dt = Math.min((now - last) / 1000, 1 / 30); last = now;
    const accel = stiffness * (to - value) - damping * velocity;
    velocity += accel * dt;
    value += velocity * dt;
    onFrame?.(value);
    if (Math.abs(to - value) > 0.002 || Math.abs(velocity) > 0.002) raf = requestAnimationFrame(step);
    else onFrame?.(to);
  };
  raf = requestAnimationFrame(step);
  return () => cancelAnimationFrame(raf);
}

const players = {};

/** One persistent <audio> per mode.
 *
 *  Re-rendering a pane used to build a fresh <audio> each time. The discarded
 *  element keeps its connection open until it is collected, so a handful of
 *  renders exhausts Chrome's six-connections-per-host budget and every later
 *  request stalls — with the server perfectly healthy, which is what makes it
 *  so hard to read. Moving one element into place preserves both the
 *  connection and the playback position. */
function player(key) {
  let el = players[key];
  if (!el) {
    el = document.createElement('audio');
    el.controls = true;
    el.preload = 'metadata';
    players[key] = el;
  }
  return el;
}

/** Mount the mode's player in `slotId`, pointing at `src`. */
function mountPlayer(key, slotId, src) {
  const el = player(key);
  const slot = $(slotId);
  if (!slot) return;
  if (el.getAttribute('src') !== src) {
    el.pause();
    el.setAttribute('src', src);
    el.load();
  }
  slot.appendChild(el);
}


const api = {
  async tracks(status, query) {
    const p = new URLSearchParams();
    if (status) p.set('status', status);
    if (query) p.set('q', query);
    const qs = p.toString();
    return (await fetch(`/api/tracks${qs ? `?${qs}` : ''}`)).json();
  },
  async track(id) { return (await fetch(`/api/track/${id}`)).json(); },
  async setField(id, field, value) {
    return (await fetch(`/api/track/${id}/field`, {
      method: 'POST', headers: { 'content-type': 'application/json' },
      body: JSON.stringify({ field, value }),
    })).json();
  },
  async accept(id) { return (await fetch(`/api/track/${id}/accept`, { method: 'POST' })).json(); },
  async ingest(url) {
    const r = await fetch('/api/ingest', {
      method: 'POST', headers: { 'content-type': 'application/json' },
      body: JSON.stringify({ url }),
    });
    if (!r.ok) throw new Error((await r.json()).detail);
    return r.json();
  },
  async ingestStatus() { return (await fetch('/api/ingest/status')).json(); },
  async ingestLog(since) {
    return (await fetch(`/api/ingest/log?since=${since}`)).json();
  },
  async elicitNext() { return (await fetch('/api/elicit/next')).json(); },
  async elicitChoice(option) {
    return (await fetch('/api/elicit/choice', {
      method: 'POST', headers: { 'content-type': 'application/json' },
      body: JSON.stringify({ option }),
    })).json();
  },
  async elicitProgress() { return (await fetch('/api/elicit/progress')).json(); },
  async elicitApply() { return (await fetch('/api/elicit/apply', { method: 'POST' })).json(); },
  async override(id, url) {
    const r = await fetch(`/api/track/${id}/url`, {
      method: 'POST', headers: { 'content-type': 'application/json' },
      body: JSON.stringify({ url }),
    });
    if (!r.ok) throw new Error((await r.json()).detail);
    return r.json();
  },
};

const state = { list: [], counts: {}, index: 0, selectedId: null, detail: null,
  field: null, filter: 'review', mode: 'review', question: null, query: '' };
const $ = (id) => document.getElementById(id);
const esc = (s) => String(s ?? '').replace(/[&<>"]/g, (c) =>
  ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c]));

/** Refresh the queue.
 *
 *  `keepDetail` is for the ingest poll. The list is ordered newest first, so
 *  every arriving track shifts every index down by one — an index-based
 *  selection therefore lands on a different song each second, and re-rendering
 *  the detail pane throws away whatever was being typed into it. Selection
 *  follows the track id, and a poll never touches the open track. */
async function loadQueue({ keepDetail = false } = {}) {
  // counts come from the unfiltered set, so the tabs keep saying how much
  // exists rather than how much the current search happens to show
  const [all, matching] = await Promise.all([
    api.tracks(),
    state.query ? api.tracks(null, state.query) : null,
  ]);
  const visible = matching ?? all;
  state.counts = all.reduce((acc, t) => (acc[t.status] = (acc[t.status] || 0) + 1, acc), {});
  state.counts.all = all.length;
  state.list = state.filter === 'all'
    ? visible : visible.filter((t) => t.status === state.filter);

  const found = state.list.findIndex((t) => t.id === state.selectedId);
  if (found >= 0) state.index = found;
  else state.index = Math.min(state.index, Math.max(state.list.length - 1, 0));

  renderTabs();
  renderQueue();
  if (!state.list.length) {
    state.selectedId = null;
    $('track').innerHTML = '<p class="empty">Queue is clear.</p>';
    $('side').innerHTML = '';
    return;
  }
  if (found >= 0 && keepDetail) return;
  await selectIndex(state.index);
}

function renderTabs() {
  const c = state.counts;
  // wayfinding: the counts answer "what's here", the tabs "where can I go"
  $('queue-count').innerHTML = ['review', 'published', 'all']
    .map((f) => `<button class="tab" data-f="${f}" aria-pressed="${state.filter === f}">
      ${f} <span class="n">${c[f] || 0}</span></button>`)
    .join('');
  for (const el of document.querySelectorAll('.tab')) {
    el.addEventListener('click', () => {
      state.filter = el.dataset.f;
      state.index = 0;
      loadQueue();
    });
  }
}

function renderQueue() {
  $('queue').innerHTML = state.list.map((t, i) => {
    const pills = [
      t.published_path ? '<span class="pill done">published</span>' : '',
      t.reason ? `<span class="pill review">${esc(t.reason.replace('_', ' '))}</span>` : '',
      t.is_video_rip ? '<span class="pill rip">video rip</span>' : '',
      t.identity_confidence != null ? `<span class="pill">${t.identity_confidence.toFixed(2)}</span>` : '',
    ].join(' ');
    return `<div class="item" role="option" data-i="${i}" aria-selected="${i === state.index}">
      <div class="t">${esc(t.title || t.norm_title || `track ${t.id}`)}</div>
      <div class="s">${esc(t.artist || t.norm_artist || t.channel || '')}</div>
      <div style="margin-top:.25rem">${pills}</div>
    </div>`;
  }).join('') || `<p class="empty">${state.query
    ? `Nothing matches "${esc(state.query)}".` : 'Nothing to review.'}</p>`;

  for (const el of document.querySelectorAll('.item')) {
    // commit on click, but the :active transform already fired on pointer-down
    el.addEventListener('click', () => selectIndex(Number(el.dataset.i)));
  }
}

async function selectIndex(i) {
  state.index = i;
  const row = state.list[i];
  if (!row) return;
  state.selectedId = row.id;
  renderQueue();
  const el = document.querySelector(`.item[data-i="${i}"]`);
  el?.scrollIntoView({ block: 'nearest', behavior: reduceMotion ? 'auto' : 'smooth' });
  state.detail = await api.track(row.id);
  state.field = null;
  renderTrack();
}

function renderTrack() {
  const d = state.detail;
  if (!d) return;
  const t = d.track;
  const resolved = Object.fromEntries(d.resolved.map((r) => [r.field, r]));
  // what Accept will choose, so the button is predictable
  const preview = Object.fromEntries((d.preview || []).map((r) => [r.field, r]));
  $('track-title').textContent = resolved.title?.value || t.norm_title || `track ${t.id}`;
  $('track-meta').textContent =
    `${t.genre_family || 'unrouted'} · confidence ${t.identity_confidence ?? '—'}`;

  const order = ['title', 'artist', 'album', 'album_artist', 'genre', 'label',
    'track_number', 'disc_number', 'year', 'release_date', 'isrc',
    'mix_name', 'remixer', 'composer', 'lyricist', 'key', 'bpm', 'artwork_url'];
  const fields = order.filter(
    (f) => resolved[f] || preview[f] || d.candidates.some((c) => c.field === f));

  const artUrls = [...new Set(d.candidates
    .filter((c) => c.field === 'artwork_url' && c.value).map((c) => c.value))];
  const chosenArt = resolved.artwork_url?.value || preview.artwork_url?.value;
  const embedded = t.published_path ? `/api/artwork/${t.id}` : null;

  $('track').innerHTML = `
    <div class="art-row">
      ${embedded
        ? `<img class="art" src="${embedded}" alt="cover art"
             onerror="this.replaceWith(Object.assign(document.createElement('div'),
               {className:'art empty-art',textContent:'no embedded art'}))">`
        : chosenArt
          ? `<img class="art" src="${esc(chosenArt)}" alt="cover art">`
          : '<div class="art empty-art">no art</div>'}
      <div>
        <div class="label" style="margin-bottom:.35rem">Cover art</div>
        <div class="art-choices">
          ${artUrls.map((u) => `<img class="art-choice ${u === chosenArt ? 'chosen' : ''}"
              src="${esc(u)}" data-art="${esc(u)}" alt="candidate cover"
              title="use this cover">`).join('') || '<span class="label">none offered</span>'}
        </div>
      </div>
    </div>
    <!-- metadata, not none: the duration appears without pressing play, so a
         working player never looks like a broken one. the files are local. -->
    <div class="player" id="track-player"></div>
    ${t.published_path
      ? `<p class="path">Published to ${esc(t.published_path)}</p>`
      : ''}
    <div style="display:flex;gap:.5rem;margin:.5rem 0 1rem">
      <button class="primary" id="accept">${t.published_path ? 'Re-tag' : 'Accept'} <kbd>↵</kbd></button>
      <input class="url" id="url" type="text" placeholder="Paste a Spotify / MusicBrainz / Discogs link…">
    </div>
    ${fields.map((f) => {
      const r = resolved[f];
      const by = r?.decided_by === 'manual' ? 'manual'
        : r?.decided_by === 'fallback' ? 'fallback' : '';
      const opts = d.candidates.filter((c) => c.field === f);
      const p = preview[f];
      // an unresolved track still has candidates — a low-confidence track is
      // never arbitrated, so without this the whole review screen reads "unset"
      const hint = p
        ? `would pick ${p.source}`
        : opts.length ? `${opts.length} sources — pick one` : 'no source offered this';
      return `<div class="field" data-field="${f}" data-focused="${state.field === f}">
        <span class="label">${f.replace(/_/g, ' ')}</span>
        <input value="${esc(r?.value ?? '')}" data-empty="${!r?.value}"
               placeholder="${esc(p?.value ?? opts[0]?.value ?? '')}" data-field="${f}">
        <span class="src ${by}">${esc(r?.source ?? hint)}${by ? ` · ${by}` : ''}</span>
      </div>`;
    }).join('')}
    <p class="label" style="margin-top:1rem">
      <kbd>j</kbd>/<kbd>k</kbd> move · <kbd>space</kbd> play · <kbd>tab</kbd> field ·
      <kbd>↵</kbd> accept · <kbd>esc</kbd> leave a field
    </p>`;

  mountPlayer('review', 'track-player', `/api/audio/${t.id}`);
  for (const el of document.querySelectorAll('.field')) {
    el.addEventListener('click', () => focusField(el.dataset.field));
  }
  for (const input of document.querySelectorAll('.field input')) {
    input.addEventListener('focus', () => focusField(input.dataset.field));
    input.addEventListener('change', async () => {
      await api.setField(t.id, input.dataset.field, input.value);
      state.detail = await api.track(t.id);
      renderTrack();
    });
    // an input keeps focus until something takes it away, and while it has
    // focus every navigation key is dead — `j` types a `j` into the title
    // rather than moving down. Enter commits and leaves; Escape abandons and
    // leaves. Without a way out, the keyboard flow works exactly once.
    input.addEventListener('keydown', (e) => {
      if (e.key === 'Enter') { e.preventDefault(); input.blur(); }
      else if (e.key === 'Escape') {
        e.preventDefault();
        input.value = resolved[input.dataset.field]?.value ?? '';
        input.blur();
      }
    });
  }
  for (const img of document.querySelectorAll('.art-choice')) {
    img.addEventListener('click', async () => {
      await api.setField(t.id, 'artwork_url', img.dataset.art);
      state.detail = await api.track(t.id);
      renderTrack();
    });
  }
  $('accept').addEventListener('click', acceptCurrent);
  $('url').addEventListener('change', async (e) => {
    try {
      const r = await api.override(t.id, e.target.value);
      e.target.value = `${r.provider} ${r.kind} ${r.id}`;
    } catch (err) { e.target.value = ''; e.target.placeholder = err.message; }
  });
  const first = state.field && fields.includes(state.field) ? state.field : fields[0];
  if (first) focusField(first);
  else $('side').innerHTML = '<p class="empty">No fields yet.</p>';
}

function focusField(field) {
  state.field = field;
  for (const el of document.querySelectorAll('.field')) {
    el.dataset.focused = el.dataset.field === field;
  }
  renderCandidates(field);
}

function renderCandidates(field) {
  const d = state.detail;
  const resolved = d.resolved.find((r) => r.field === field);
  const options = d.candidates.filter((c) => c.field === field);
  $('side-title').textContent = 'Sources';
  $('side-field').textContent = field.replace(/_/g, ' ');
  $('side').innerHTML = options.length
    ? options.map((c) => {
        const art = field === 'artwork_url'
          ? `<img class="thumb" src="${esc(c.value)}" alt="">` : '';
        const shown = field === 'artwork_url' ? '' : esc(c.value);
        return `<div class="cand ${c.value === resolved?.value ? 'chosen' : ''}"
          data-value="${esc(c.value)}">
          <span>${art}${shown}</span><span class="label">${esc(c.source)}</span></div>`;
      }).join('')
    : '<p class="empty">No source offered this field.</p>';
  for (const el of document.querySelectorAll('.cand')) {
    el.addEventListener('click', async () => {
      await api.setField(d.track.id, field, el.dataset.value);
      state.detail = await api.track(d.track.id);
      renderTrack();
    });
  }
}

async function acceptCurrent() {
  const row = state.list[state.index];
  if (!row) return;
  const el = document.querySelector(`.item[data-i="${state.index}"]`);
  // spatial consistency: it leaves the way the queue moves — upward, out
  if (el) spring(el, 0, -24, { onFrame: (v) => {
    el.style.transform = `translateY(${v}px)`;
    el.style.opacity = String(Math.max(0, 1 + v / 24));
  } });
  await api.accept(row.id);
  await loadQueue();
}

// --- acquisition ----------------------------------------------------------

let ingestTimer = 0;
let logCursor = -1;

const STAGES = [
  ['downloaded', 'downloaded'],
  ['analysed', 'analysed'],
  ['resolved', 'resolved'],
  ['published', 'published'],
  ['queued', 'to review'],
  ['skipped', 'skipped'],
  ['failed', 'failed'],
];

function renderCounters(s) {
  const pct = s.total ? Math.round((100 * s.done) / s.total) : 0;
  $('counters').innerHTML = `
    <div class="count-head">
      <strong>${s.done}/${s.total || '?'}</strong>
      <span class="label">${esc(s.stage || (s.running ? 'working' : 'idle'))}</span>
      <span class="count-now">${esc(s.current || '')}</span>
    </div>
    <div class="bar"><div class="bar-fill" style="width:${pct}%"></div></div>
    <div class="counts">
      ${STAGES.map(([k, label]) => `
        <div class="count ${s[k] ? 'has' : ''}" data-k="${k}">
          <span class="n">${s[k] || 0}</span>
          <span class="label">${label}</span>
        </div>`).join('')}
    </div>`;
}

/** Append new lines and keep the view pinned to the bottom, unless the reader
 *  has scrolled up — yanking them back down mid-read is the thing that makes a
 *  log window unusable. */
async function pumpLog(progressLine) {
  const box = $('log');
  const { lines, cursor } = await api.ingestLog(logCursor);
  const atBottom = box.scrollHeight - box.scrollTop - box.clientHeight < 40;

  if (lines.length) {
    logCursor = cursor;
    const frag = document.createDocumentFragment();
    for (const l of lines) {
      const el = document.createElement('div');
      el.className = `ln ${l.level}`;
      el.textContent = l.text;
      frag.appendChild(el);
    }
    box.querySelector('.ln.live')?.remove();
    box.appendChild(frag);
  }
  // the live percentage is rewritten in place, the way a terminal does it
  let live = box.querySelector('.ln.live');
  if (progressLine) {
    if (!live) {
      live = document.createElement('div');
      live.className = 'ln live';
      box.appendChild(live);
    }
    live.textContent = progressLine;
  } else {
    live?.remove();
  }
  if (atBottom) box.scrollTop = box.scrollHeight;
}

// the console is a working surface during a run and clutter after it; the
// preference is remembered so it does not have to be re-collapsed every poll
let logOpen = true;

function setLogOpen(open) {
  logOpen = open;
  $('log').hidden = !open;
  const btn = $('console-toggle');
  btn.textContent = open ? 'Hide output' : 'Show output';
  btn.setAttribute('aria-expanded', String(open));
}

// single flight: belt and braces, so any future caller cannot reintroduce the
// overlapping-poll duplication above
let polling = false;

async function pollIngest() {
  if (polling) return true;
  polling = true;
  try {
    return await _pollIngest();
  } finally {
    polling = false;
  }
}

async function _pollIngest() {
  const s = await api.ingestStatus();
  if (!s.url) { $('console').hidden = true; $('add-status').innerHTML = ''; return false; }

  $('console').hidden = false;
  renderCounters(s);
  if (logOpen) await pumpLog(s.progress_line);

  $('add-status').innerHTML = s.error
    ? `<p class="add-line bad">${esc(s.error)}</p>`
    : s.running
      ? `<p class="add-line">${s.done}/${s.total} · ${esc(s.current || 'starting…')}</p>`
      : `<p class="add-line done">Finished — ${s.published} published,
           ${s.queued} to review</p>`;
  return Boolean(s.running);
}

/** Poll until the run ends, rescheduling only once a tick has finished.
 *
 *  This was a `setInterval`, which fires whether or not the previous tick has
 *  returned. Each tick makes three or four requests, so ticks overlapped
 *  routinely — and because `pumpLog` reads `logCursor`, awaits, and only then
 *  writes it back, two overlapping polls both read the stale cursor and both
 *  appended the same batch. Every log line could appear twice. */
async function watchIngest() {
  clearTimeout(ingestTimer);
  const tick = async () => {
    const running = await pollIngest();
    // the queue grows as tracks land, so refresh it alongside the progress —
    // but never disturb the track being edited
    await loadQueue({ keepDetail: true });
    if (running) ingestTimer = setTimeout(tick, 1000);
  };
  await tick();
}

// debounced: a keystroke per request would hammer sqlite on a 2,000-row library
let searchTimer = 0;
$('search').addEventListener('input', (e) => {
  clearTimeout(searchTimer);
  const value = e.target.value.trim();
  searchTimer = setTimeout(() => {
    state.query = value;
    state.index = 0;
    loadQueue();
  }, 180);
});
$('search').addEventListener('keydown', (e) => {
  // escape clears and hands the keyboard back to the queue
  if (e.key === 'Escape') { e.target.value = ''; state.query = ''; e.target.blur(); loadQueue(); }
});

$('console-toggle').addEventListener('click', () => setLogOpen(!logOpen));

// a textarea grows with the paste; Enter submits, shift+Enter adds a line
$('add-url').addEventListener('input', (e) => {
  e.target.style.height = 'auto';
  e.target.style.height = `${Math.min(e.target.scrollHeight, 120)}px`;
});
$('add-url').addEventListener('keydown', (e) => {
  if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); $('add').requestSubmit(); }
});

$('add').addEventListener('submit', async (e) => {
  e.preventDefault();
  const input = $('add-url');
  const url = input.value.trim();
  if (!url) return;
  try {
    await api.ingest(url);
    input.value = '';
    input.style.height = 'auto';
    watchIngest();
  } catch (err) {
    $('add-status').innerHTML = `<p class="add-line bad">${esc(err.message)}</p>`;
  }
});

// --- calibration ----------------------------------------------------------
//
// Blind comparison (SPEC.md §9). The server sends values and never source
// names, so nothing here can leak the ranking — not to the eye, and not to
// view-source. Answers are not revealed per question either: seeing "you
// picked Discogs" would anchor the next fifty answers toward consistency
// rather than judgement.

function setMode(mode) {
  state.mode = mode;
  $('app').hidden = mode !== 'review';
  $('elicit').hidden = mode !== 'elicit';
  for (const el of document.querySelectorAll('.mode')) {
    el.setAttribute('aria-pressed', String(el.dataset.mode === mode));
  }
  if (mode === 'elicit') nextQuestion();
}

function fieldLabel(f) { return f.replace(/_/g, ' '); }

async function nextQuestion() {
  const q = await api.elicitNext();
  state.question = q.done ? null : q;
  renderQuestion(q);
}

function renderQuestion(q) {
  const p = q.progress || {};
  // `remaining` is an upper bound: a pair still collapses to one option when
  // the sources turn out to agree, and is skipped rather than asked. Showing
  // "5 left" beside "complete" reads as a bug, so the count stops at the end.
  $('elicit-progress').textContent = q.done
    ? `${p.answered || 0} answered · ${p.cells_started || 0} cells`
    : `${p.answered || 0} answered · ${p.remaining || 0} left · ${p.cells_done || 0} cells done`;

  if (q.done) {
    $('elicit-field').textContent = 'Calibration complete';
    $('elicit-body').innerHTML = `
      <div class="elicit-done">
        <p>Every reachable cell has enough answers, or has run out of tracks
           that disagree.</p>
        <button class="primary" id="apply">Write precedence table</button>
        <div id="apply-result"></div>
      </div>`;
    $('apply').addEventListener('click', applyRanking);
    return;
  }

  $('elicit-field').textContent = fieldLabel(q.field);
  const ctx = [q.context.artist, q.context.title].filter(Boolean).join(' — ');
  const isArt = q.field === 'artwork_url';

  $('elicit-body').innerHTML = `
    <p class="elicit-ctx">${esc(ctx || `track ${q.track_id}`)}
      <span class="pill">${esc(q.family)}</span></p>
    <div class="player" id="elicit-player"></div>
    <p class="elicit-ask">Which ${esc(fieldLabel(q.field))} is better?</p>
    <div class="choices">
      ${q.options.map((o, i) => `
        <button class="choice" data-i="${i}">
          <span class="key">${i + 1}</span>
          ${isArt
            ? `<img class="choice-art" src="${esc(o.value)}" alt="cover option ${i + 1}">`
            : `<span class="choice-value">${esc(o.value)}</span>`}
          ${o.extra.length
            ? `<span class="choice-extra">${o.extra
                .map(([k, v]) => `${esc(k)} ${esc(v)}`).join(' · ')}</span>`
            : ''}
        </button>`).join('')}
    </div>
    <button class="skip" id="no-diff"><span class="key">0</span> No real difference</button>
    <p class="label elicit-help">
      <kbd>1</kbd>–<kbd>${q.options.length}</kbd> choose · <kbd>0</kbd> no difference ·
      <kbd>space</kbd> play
    </p>`;

  mountPlayer('elicit', 'elicit-player', `/api/audio/${q.track_id}`);
  for (const el of document.querySelectorAll('.choice')) {
    el.addEventListener('click', () => answer(Number(el.dataset.i)));
  }
  $('no-diff').addEventListener('click', () => answer(-1));
}

async function answer(option) {
  if (!state.question) return;
  const el = option >= 0
    ? document.querySelector(`.choice[data-i="${option}"]`) : $('no-diff');
  // feedback lands on the chosen card before the next question replaces it
  if (el) spring(el, 0, 1, { response: 0.25, onFrame: (v) => {
    el.style.transform = `scale(${1 - 0.03 * Math.max(0, 1 - v)})`;
  } });
  state.question = null;
  await api.elicitChoice(option);
  nextQuestion();
}

async function applyRanking() {
  const r = await api.elicitApply();
  const detail = await api.elicitProgress();
  const rows = detail.cells.filter((c) => c.ranking.length);
  const thin = detail.cells.filter((c) => !c.ranking.length);

  // "0 cells written" on its own reads as a failure. It is the guard doing its
  // job: a ranking derived from one or two answers is worse than the built-in
  // default, so a thin cell is deliberately left alone.
  $('apply-result').innerHTML = rows.length
    ? `<p class="label" style="margin-top:1rem">${r.cells} cells written</p>
       <table class="cells">
         <tr><th>family</th><th>field</th><th>answers</th><th>ranking</th></tr>
         ${rows.map((c) => `<tr>
           <td>${esc(c.family)}</td><td>${esc(fieldLabel(c.field))}</td>
           <td>${c.answers}</td>
           <td class="mono">${esc(c.ranking.join(' → '))}</td></tr>`).join('')}
       </table>
       <p class="label">Run <span class="mono">music retag</span> to re-emit
         tags with the calibrated table.</p>`
    : `<p class="add-line" style="margin-top:1rem">Nothing written yet — every
         cell still has too few answers to beat the built-in ranking.
         ${thin.length ? `${thin.length} cell${thin.length === 1 ? '' : 's'}
         started; each needs at least 5 answers.` : ''}
         Add more tracks and calibrate again.</p>`;
}

for (const el of document.querySelectorAll('.mode')) {
  el.addEventListener('click', () => setMode(el.dataset.mode));
}

const TYPING = new Set(['INPUT', 'TEXTAREA']);

addEventListener('keydown', (e) => {
  if (TYPING.has(e.target.tagName) && e.key !== 'Enter') return;
  const audio = players[state.mode === 'elicit' ? 'elicit' : 'review'];
  if (state.mode === 'elicit') {
    if (e.key === ' ') { e.preventDefault(); audio && (audio.paused ? audio.play() : audio.pause()); }
    else if (/^[0-9]$/.test(e.key) && state.question) {
      e.preventDefault();
      const n = Number(e.key);
      if (n === 0) answer(-1);
      else if (n <= state.question.options.length) answer(n - 1);
    }
    return;
  }
  if (e.key === 'j' || e.key === 'ArrowDown') { e.preventDefault(); selectIndex(Math.min(state.index + 1, state.list.length - 1)); }
  else if (e.key === 'k' || e.key === 'ArrowUp') { e.preventDefault(); selectIndex(Math.max(state.index - 1, 0)); }
  else if (e.key === ' ') { e.preventDefault(); audio && (audio.paused ? audio.play() : audio.pause()); }
  else if (e.key === 'Enter' && !TYPING.has(e.target.tagName)) { e.preventDefault(); acceptCurrent(); }
});

await loadQueue();
// one poll on load; it keeps going only if a run is already in progress
watchIngest();

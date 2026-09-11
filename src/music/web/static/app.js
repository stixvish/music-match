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

const api = {
  async tracks(status) {
    const q = status ? `?status=${encodeURIComponent(status)}` : '';
    return (await fetch(`/api/tracks${q}`)).json();
  },
  async track(id) { return (await fetch(`/api/track/${id}`)).json(); },
  async setField(id, field, value) {
    return (await fetch(`/api/track/${id}/field`, {
      method: 'POST', headers: { 'content-type': 'application/json' },
      body: JSON.stringify({ field, value }),
    })).json();
  },
  async accept(id) { return (await fetch(`/api/track/${id}/accept`, { method: 'POST' })).json(); },
  async override(id, url) {
    const r = await fetch(`/api/track/${id}/url`, {
      method: 'POST', headers: { 'content-type': 'application/json' },
      body: JSON.stringify({ url }),
    });
    if (!r.ok) throw new Error((await r.json()).detail);
    return r.json();
  },
};

const state = { list: [], counts: {}, index: 0, detail: null, field: null, filter: 'review' };
const $ = (id) => document.getElementById(id);
const esc = (s) => String(s ?? '').replace(/[&<>"]/g, (c) =>
  ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c]));

async function loadQueue() {
  const all = await api.tracks();
  state.counts = all.reduce((acc, t) => (acc[t.status] = (acc[t.status] || 0) + 1, acc), {});
  state.counts.all = all.length;
  state.list = state.filter === 'all' ? all : all.filter((t) => t.status === state.filter);
  renderTabs();
  renderQueue();
  if (state.list.length) selectIndex(Math.min(state.index, state.list.length - 1));
  else { $('track').innerHTML = '<p class="empty">Queue is clear.</p>'; $('side').innerHTML = ''; }
}

function renderTabs() {
  const c = state.counts;
  // wayfinding: the counts answer "what's here", the tabs "where can I go"
  $('queue-count').innerHTML = ['review', 'published', 'all']
    .map((f) => `<button class="tab" data-f="${f}" aria-pressed="${state.filter === f}">
      ${f} ${c[f] || 0}</button>`)
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
      t.reason ? `<span class="pill review">${esc(t.reason.replace('_', ' '))}</span>` : '',
      t.is_video_rip ? '<span class="pill rip">video rip</span>' : '',
      t.identity_confidence != null ? `<span class="pill">${t.identity_confidence.toFixed(2)}</span>` : '',
    ].join(' ');
    return `<div class="item" role="option" data-i="${i}" aria-selected="${i === state.index}">
      <div class="t">${esc(t.title || t.norm_title || `track ${t.id}`)}</div>
      <div class="s">${esc(t.artist || t.norm_artist || t.channel || '')}</div>
      <div style="margin-top:.25rem">${pills}</div>
    </div>`;
  }).join('') || '<p class="empty">Nothing to review.</p>';

  for (const el of document.querySelectorAll('.item')) {
    // commit on click, but the :active transform already fired on pointer-down
    el.addEventListener('click', () => selectIndex(Number(el.dataset.i)));
  }
}

async function selectIndex(i) {
  state.index = i;
  const row = state.list[i];
  if (!row) return;
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
    'mix_name', 'remixer', 'composer', 'lyricist', 'key', 'bpm'];
  const fields = order.filter(
    (f) => resolved[f] || preview[f] || d.candidates.some((c) => c.field === f));

  $('track').innerHTML = `
    <audio id="audio" controls preload="none" src="/api/audio/${t.id}"></audio>
    <div style="display:flex;gap:.5rem;margin:.5rem 0 1rem">
      <button class="primary" id="accept">Accept <kbd>↵</kbd></button>
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
      <kbd>↵</kbd> accept
    </p>`;

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
    ? options.map((c) => `<div class="cand ${c.value === resolved?.value ? 'chosen' : ''}"
         data-value="${esc(c.value)}">
         <span>${esc(c.value)}</span><span class="label">${esc(c.source)}</span></div>`).join('')
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

addEventListener('keydown', (e) => {
  if (e.target.tagName === 'INPUT' && e.key !== 'Enter') return;
  const audio = $('audio');
  if (e.key === 'j' || e.key === 'ArrowDown') { e.preventDefault(); selectIndex(Math.min(state.index + 1, state.list.length - 1)); }
  else if (e.key === 'k' || e.key === 'ArrowUp') { e.preventDefault(); selectIndex(Math.max(state.index - 1, 0)); }
  else if (e.key === ' ') { e.preventDefault(); audio && (audio.paused ? audio.play() : audio.pause()); }
  else if (e.key === 'Enter' && e.target.tagName !== 'INPUT') { e.preventDefault(); acceptCurrent(); }
});

loadQueue();

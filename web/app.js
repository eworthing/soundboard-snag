/**
 * app.js — Soundboard Snag web UI
 *
 * Vanilla JS, no build step, no dependencies.
 * Talks to the app's local server:
 *   GET  /api/search?q=&max=&min_views=&min_sounds=&sort=&include_dates=&recent_days=
 *          → SSE stream
 *   GET  /api/board/<board>
 *          → JSON {board, has_downloads, total_count, sounds:[{id,title}], error|null}
 *   POST /api/fetch-sound   body: {board, sound_id, title}
 *          → {token}; the app opens the sound's download window
 *   GET  /api/fetch-sound/<token>
 *          → {status: waiting|saving|saved|exists|cancelled|failed|expired, name?, kb?, error?}
 *
 * When no server is running, the UI renders the empty-state gracefully
 * and fetch errors surface as toasts.
 */

'use strict';

/* ═══════════════════════════════════════════════════════════════
   STATE
   ═══════════════════════════════════════════════════════════════ */

/** AbortController for the currently active search SSE stream. */
let searchAbort = null;

/** How many boards have been scanned in the current search. */
let boardsScanned = 0;

/** Map of board-identifier → pad DOM element for reconciliation. */
const padMap = new Map();

/** Last rendered result set + query, so the sort control can re-order in place. */
let lastResults = [];
let lastQuery   = '';

const SOUND_POLL_MS = 1000;
let saveHintShown = false;

/**
 * Save one sound. The app opens soundboard.com's download page in its own
 * window (the person passes the site's "verify you are human" check there if
 * it asks), the file lands in the board's folder under a clean name, and the
 * window closes. Poll until it's done, then cue the next sound.
 */
async function fetchSound(chip, board, soundId, title) {
  if (!/^\d+$/.test(soundId) || chip.dataset.busy) return;
  chip.dataset.busy = '1';
  const soundsEl = chip.parentElement;
  chip.classList.remove('is-error', 'is-next');
  chip.classList.add('is-downloading');
  const finish = (cls) => {
    delete chip.dataset.busy;
    chip.classList.remove('is-downloading');
    if (cls) chip.classList.add(cls);
  };

  let token = null;
  try {
    const resp = await fetch('/api/fetch-sound', {
      method:  'POST',
      headers: { 'Content-Type': 'application/json' },
      body:    JSON.stringify({ board, sound_id: soundId, title }),
    });
    const data = await resp.json().catch(() => ({}));
    token = data.token || null;
    if (!token) {
      finish('is-error');
      showToast(`Couldn't start that sound — ${data.error || `error ${resp.status}`}`, 'error');
      return;
    }
  } catch (_) {
    finish('is-error');
    showToast('Couldn\'t reach the app — try again.', 'error');
    return;
  }

  for (;;) {
    await new Promise(r => setTimeout(r, SOUND_POLL_MS));
    if (!chip.isConnected) {
      // The chip list was re-rendered (full list loaded): find this sound again.
      chip = soundsEl.querySelector(`.sound-chip[data-id="${soundId}"]`);
      if (!chip) return;
    }
    let st;
    try {
      st = await (await fetch(`/api/fetch-sound/${encodeURIComponent(token)}`)).json();
    } catch (_) {
      finish('is-error');
      return;
    }
    if (st.status === 'waiting' || st.status === 'saving') continue;
    if (st.status === 'saved' || st.status === 'exists') {
      finish('is-saved');
      showToast(st.status === 'exists' ? `Already saved: ${st.name}` : `Saved ${st.name}`, 'success');
      cueNextChip(chip);
    } else if (st.status === 'failed') {
      finish('is-error');
      showToast(`Couldn't save "${title}"${st.error ? ' — ' + st.error : ''}.`, 'error');
    } else if (st.status === 'cancelled') {
      finish();
      showToast(`Download window closed — click "${title}" to try again.`, '');
    } else {
      finish();
      showToast(`Gave up waiting for "${title}" — click it to try again.`, 'error');
    }
    return;
  }
}

/** Highlight and focus the next sound that isn't saved yet (Enter saves it). */
function cueNextChip(fromChip) {
  const chips = [...fromChip.parentElement.querySelectorAll('.sound-chip[data-id]')];
  chips.forEach(c => c.classList.remove('is-next'));
  const start = chips.indexOf(fromChip);
  const next = chips.slice(start + 1).concat(chips.slice(0, start))
    .find(c => !c.classList.contains('is-saved') && !c.classList.contains('is-downloading'));
  if (next) {
    next.classList.add('is-next');
    next.focus({ preventScroll: false });
  } else {
    showToast('All sounds on this board are saved.', 'success');
  }
}

/* ═══════════════════════════════════════════════════════════════
   DOM REFERENCES
   Gathered once at module load. All IDs match index.html.
   ═══════════════════════════════════════════════════════════════ */

const $ = (id) => document.getElementById(id);

const searchForm      = $('search-form');
const searchInput     = $('search-input');
const searchBtn       = $('search-btn');
const minViewsInput   = $('min-views');
const minSoundsInput  = $('min-sounds');
const maxInput        = $('max-results');
const sortSelect      = $('sort-order');
const includeDates    = $('include-dates');
const padGrid         = $('pad-grid');
const rackEmpty       = $('rack-empty');
const rackCount       = $('rack-count');
const rackStatus      = $('rack-status');
const rackEl          = $('rack');
const toastContainer  = $('toast-container');

/* ═══════════════════════════════════════════════════════════════
   SSE HELPER
   ═══════════════════════════════════════════════════════════════ */

/**
 * Consume a Server-Sent Events stream via fetch() + ReadableStream.
 *
 * Uses fetch() instead of native EventSource because:
 * 1. EventSource is GET-only — it can't carry a POST body for /api/download.
 * 2. We need a shared AbortController so navigating away / starting a new
 *    search cancels the in-flight request (which signals the server to stop).
 *
 * Frame format parsed: `event: <type>\ndata: <json>\n\n`
 * Heartbeat lines (`: keepalive`) are silently ignored.
 *
 * @param {string} url
 * @param {object} opts
 * @param {AbortSignal} opts.signal        required — connect to AbortController
 * @param {function} opts.onEvent         called as onEvent(type, dataObject)
 * @returns {Promise<void>}  resolves when stream ends; rejects on network error
 */
async function streamSSE(url, { signal, onEvent }) {
  const resp = await fetch(url, { headers: { Accept: 'text/event-stream' }, signal });

  if (!resp.ok) {
    // Non-2xx — attempt to extract server error message
    let msg = `HTTP ${resp.status}`;
    try {
      const errBody = await resp.json();
      if (errBody && errBody.error) msg = errBody.error;
    } catch (_) { /* ignore — body might not be JSON */ }
    throw new Error(msg);
  }

  const reader  = resp.body.getReader();
  const decoder = new TextDecoder();
  let   buffer  = '';

  // eslint-disable-next-line no-constant-condition
  while (true) {
    const { value, done } = await reader.read();
    if (done) break;

    buffer += decoder.decode(value, { stream: true });

    // SSE frames are separated by double newlines.
    // Split on \n\n; keep the last (potentially incomplete) chunk in buffer.
    const frames = buffer.split('\n\n');
    buffer = frames.pop(); // last element may be incomplete

    for (const frame of frames) {
      if (!frame.trim()) continue;

      let evType = '';
      let evData = '';

      for (const line of frame.split('\n')) {
        if (line.startsWith('event:')) {
          evType = line.slice(6).trim();
        } else if (line.startsWith('data:')) {
          evData = line.slice(5).trim();
        }
        // Lines starting with ':' are comments/keepalives — ignore.
      }

      if (!evType || !evData) continue;

      let parsed;
      try {
        parsed = JSON.parse(evData);
      } catch (e) {
        console.warn('[SSE] Failed to parse frame data:', evData, e);
        continue;
      }

      onEvent(evType, parsed);
    }
  }
}

/* ═══════════════════════════════════════════════════════════════
   SKELETON PADS
   Show 8 placeholder pads immediately when a search starts so
   there is no blank/empty flash during the SSE wait.
   ═══════════════════════════════════════════════════════════════ */

function createSkeletonPad() {
  const el = document.createElement('div');
  el.className = 'pad pad--skeleton';
  el.setAttribute('role', 'listitem');
  el.setAttribute('aria-hidden', 'true');
  el.innerHTML = `
    <div class="skeleton-line skeleton-line--title"></div>
    <div class="skeleton-line skeleton-line--stat"></div>
    <div class="skeleton-line skeleton-line--stat-2"></div>
    <div class="skeleton-line skeleton-line--btn"></div>
  `;
  return el;
}

/** Clear the grid and fill it with n skeleton pads. */
function showSkeletons(n) {
  padGrid.innerHTML = '';
  padMap.clear();
  rackEmpty.hidden = true;
  for (let i = 0; i < n; i++) {
    padGrid.appendChild(createSkeletonPad());
  }
}

/* ═══════════════════════════════════════════════════════════════
   REAL PAD — creation
   ═══════════════════════════════════════════════════════════════ */

/**
 * Escape a string for safe insertion into HTML innerHTML.
 */
function escHtml(s) {
  return String(s ?? '')
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;')
    .replace(/'/g, '&#39;');
}

/**
 * Format an ISO-8601 date string → short display like "Jun 2025".
 * Falls back to the first 7 characters ("2025-06") if Date parsing fails.
 */
function formatDate(iso) {
  if (!iso) return '';
  try {
    return new Date(iso).toLocaleDateString('en-US', { month: 'short', year: 'numeric' });
  } catch (_) {
    return iso.slice(0, 7);
  }
}


/**
 * Create a real soundboard pad element from an API board object.
 *
 * Board object shape (from /api/search `results` event):
 *   { board, name, has_downloads, sounds, total_count,
 *     description, category, views, views_int, tags,
 *     approx_updated, approx_source }
 *
 * @param {object} board
 * @param {number} [animDelay=0]  CSS animation-delay in ms (for stagger)
 * @returns {HTMLElement}
 */
function createPad(board, animDelay = 0) {
  const el = document.createElement('div');
  el.className = 'pad pad--entering';
  el.setAttribute('role', 'listitem');
  el.setAttribute('tabindex', '0');
  el.setAttribute('aria-expanded', 'false');
  el.setAttribute(
    'aria-label',
    `${board.name} — ${board.total_count} sounds, ${board.views} views`
  );

  if (animDelay > 0) {
    el.style.animationDelay = `${animDelay}ms`;
  }

  // Tags — limit to 4 so pads don't overflow
  const tagsHtml = (board.tags || []).slice(0, 4)
    .map(t => `<span class="pad__tag">${escHtml(t)}</span>`)
    .join('');

  // Approximate date — only shown when present
  const dateHtml = board.approx_updated
    ? `<span class="pad__stat"><span class="pad__stat-value">${escHtml(formatDate(board.approx_updated))}</span> updated</span>`
    : '';

  // GRAB button (only for downloadable boards)
  const grabHtml = board.has_downloads
    ? `<button
         type="button"
         class="btn btn--primary pad__grab"
         aria-label="Save the next sound from ${escHtml(board.name)}"
       >
         <span class="grab-label">Get next sound</span>
       </button>`
    : `<span class="pad__stat" style="color:var(--red);font-size:0.62rem;">play-only</span>`;

  // Cover art — board icon scraped from soundboard.com. Lazy-loaded; on error
  // (404 / blocked) it collapses to the styled waveform placeholder.
  const mediaHtml = `
    <div class="pad__media${board.image ? '' : ' pad__media--empty'}">
      ${board.image
        ? `<img class="pad__art" src="${escHtml(board.image)}" alt="" loading="lazy" decoding="async"
                onerror="this.closest('.pad__media').classList.add('pad__media--empty');this.remove();">`
        : ''}
    </div>`;

  el.innerHTML = `
    ${mediaHtml}
    <div class="pad__name">${escHtml(board.name)}</div>
    <div class="pad__stats">
      <span class="pad__stat">
        <span class="pad__stat-value">${escHtml(String(board.total_count))}</span> sounds
      </span>
      <span class="pad__stat">
        <span class="pad__stat-value">${escHtml(board.views || '—')}</span> views
      </span>
      ${dateHtml}
    </div>
    ${tagsHtml ? `<div class="pad__tags">${tagsHtml}</div>` : ''}
    <div class="pad__footer">
      ${grabHtml}
    </div>
    <!-- Sound chips — filled from the search result, or on expand -->
    <div class="pad__sounds" hidden></div>
  `;

  // Search results carry the board's first sounds: show them straight away so
  // a sound is one click from the results, with no expand step.
  if (board.has_downloads && (board.sounds || []).length) {
    const soundsEl = el.querySelector('.pad__sounds');
    renderChips(el, board, board.sounds, board.total_count);
    soundsEl.hidden = false;
    el.classList.add('pad--active');
    el.setAttribute('aria-expanded', 'true');
  }

  // ── Expand sounds on click / keyboard (but not when clicking GRAB) ──
  const expand = () => toggleSounds(el, board);

  el.addEventListener('click', (e) => {
    if (e.target.closest('.pad__grab')) return;
    expand();
  });

  el.addEventListener('keydown', (e) => {
    if (e.key === 'Enter' || e.key === ' ') {
      if (e.target.closest('.pad__grab')) return;
      e.preventDefault();
      expand();
    }
  });

  // ── GRAB download ──
  const grabBtn = el.querySelector('.pad__grab');
  if (grabBtn) {
    grabBtn.addEventListener('click', (e) => {
      e.stopPropagation();
      getNextSound(el, board);
    });
  }

  return el;
}

/* ═══════════════════════════════════════════════════════════════
   SOUND EXPAND — fetch /api/board/<board> and show sound chips
   ═══════════════════════════════════════════════════════════════ */

/**
 * Fill a pad's chip area. If the board has more sounds than we were given
 * (search results carry only the first few), add a "+N more" chip that loads
 * the full list.
 */
function renderChips(padEl, board, sounds, totalCount) {
  const soundsEl = padEl.querySelector('.pad__sounds');
  // Keep per-chip state (saved / waiting) when the full list replaces a partial one.
  const state = new Map([...soundsEl.querySelectorAll('.sound-chip[data-id]')]
    .map(c => [c.dataset.id, c.className]));
  soundsEl.innerHTML = sounds
    .map(s => `<button type="button" class="sound-chip" data-id="${escHtml(s.id)}" data-title="${escHtml(s.title)}" title="Download — ${escHtml(s.title)}">${escHtml(s.title)}</button>`)
    .join('');
  soundsEl.querySelectorAll('.sound-chip').forEach(chip => {
    if (state.has(chip.dataset.id)) chip.className = state.get(chip.dataset.id);
    chip.addEventListener('click', (e) => {
      e.stopPropagation();  // don't collapse the pad
      fetchSound(chip, board.board, chip.dataset.id, chip.dataset.title);
    });
  });
  const missing = (Number(totalCount) || 0) - sounds.length;
  if (missing > 0) {
    const more = document.createElement('button');
    more.type = 'button';
    more.className = 'sound-chip sound-chip--more';
    more.textContent = `+${missing} more`;
    more.title = 'Show all sounds on this board';
    more.addEventListener('click', (e) => {
      e.stopPropagation();
      more.disabled = true;
      more.textContent = 'Loading…';
      loadAllSounds(padEl, board);
    });
    soundsEl.appendChild(more);
  } else {
    padEl.dataset.soundsLoaded = '1';
  }
}

async function toggleSounds(padEl, board) {
  const soundsEl = padEl.querySelector('.pad__sounds');
  const isOpen   = !soundsEl.hidden;

  if (isOpen) {
    // Collapse
    soundsEl.hidden = true;
    padEl.classList.remove('pad--active');
    padEl.setAttribute('aria-expanded', 'false');
    return;
  }

  // Expand
  padEl.classList.add('pad--active');
  soundsEl.hidden = false;
  padEl.setAttribute('aria-expanded', 'true');

  // Already showing sounds (full list, or the first few from the search) — don't re-fetch
  if (padEl.dataset.soundsLoaded || soundsEl.querySelector('.sound-chip')) return;

  soundsEl.innerHTML = `<span class="pad__sounds-msg">Loading…</span>`;
  await loadAllSounds(padEl, board);
}

/** Fetch /api/board/<board> and render the board's full sound list. */
async function loadAllSounds(padEl, board) {
  const soundsEl = padEl.querySelector('.pad__sounds');

  try {
    // encodeURIComponent handles spaces, %, & in the board identifier
    const resp = await fetch(`/api/board/${encodeURIComponent(board.board)}`);

    if (!resp.ok) {
      const errData = await resp.json().catch(() => ({}));
      const msg     = resp.status === 503
        ? 'Server busy — try again.'
        : escHtml(errData.error || `Error ${resp.status}`);
      soundsEl.innerHTML = `<span class="pad__sounds-msg is-error">${msg}</span>`;
      return;
    }

    const data   = await resp.json();
    const sounds = data.sounds || [];

    if (!sounds.length) {
      soundsEl.innerHTML = `<span class="pad__sounds-msg">No sounds found.</span>`;
    } else {
      // Each chip is a button — click it to download just that one sound.
      renderChips(padEl, board, sounds, sounds.length);
    }

    padEl.dataset.soundsLoaded = '1';
  } catch (err) {
    if (err.name === 'AbortError') return;
    soundsEl.innerHTML = `<span class="pad__sounds-msg is-error">Couldn't reach soundboard.com — check your connection and try again.</span>`;
  }
}

/* ═══════════════════════════════════════════════════════════════
   GET NEXT SOUND — the pad button
   ═══════════════════════════════════════════════════════════════ */

/**
 * Whole boards can't be grabbed in one go (every sound needs its own human
 * check), so the pad button saves the next sound that isn't saved yet.
 */
async function getNextSound(padEl, board) {
  if (padEl.querySelector('.sound-chip.is-downloading')) {
    showToast('Still saving a sound from this board. Finish it first.', '');
    return;
  }
  const pick = () => padEl.querySelector('.sound-chip[data-id]:not(.is-saved)');
  let next = pick();
  if (!next && !padEl.dataset.soundsLoaded) {
    if (padEl.getAttribute('aria-expanded') !== 'true') await toggleSounds(padEl, board);
    else await loadAllSounds(padEl, board);
    next = pick();
  }
  if (next) {
    padEl.querySelector('.pad__sounds').hidden = false;
    padEl.classList.add('pad--active');
    padEl.setAttribute('aria-expanded', 'true');
    fetchSound(next, board.board, next.dataset.id, next.dataset.title);
  } else {
    showToast('All sounds on this board are saved.', 'success');
  }
}

/* ═══════════════════════════════════════════════════════════════
   SEARCH — GET /api/search  →  SSE events
   ═══════════════════════════════════════════════════════════════ */

async function startSearch(query) {
  // Cancel any in-flight search stream (triggers server-side cancel via disconnect)
  if (searchAbort) {
    searchAbort.abort();
  }
  searchAbort  = new AbortController();
  boardsScanned = 0;
  lastQuery = query;

  const maxBoards = parseInt(maxInput.value, 10) || 20;

  // Enter searching UI state
  searchBtn.classList.add('is-loading');
  searchBtn.disabled = true;
  rackEl.classList.add('is-searching');
  rackStatus.textContent  = '';
  rackCount.textContent   = `0 scanned`;

  // Show skeleton pads immediately — no blank flash
  showSkeletons(Math.min(8, maxBoards));

  // Build query string from controls
  const params = new URLSearchParams({ q: query });
  const minV   = minViewsInput.value.trim();
  const minS   = minSoundsInput.value.trim();
  if (minV)              params.set('min_views', minV);
  if (minS)              params.set('min_sounds', minS);
  params.set('max', String(maxBoards));
  params.set('sort', sortSelect.value);
  if (includeDates.checked)              params.set('include_dates', '1');
  // No hard recent-days cutoff — "recently updated" sort shows the newest found.

  try {
    await streamSSE(`/api/search?${params}`, {
      signal: searchAbort.signal,
      onEvent(type, data) {
        switch (type) {

          // Board analysis started — the SINGLE source for the scanned counter.
          // (--max is "max boards to check", and the engine scans more boards than
          // it ultimately returns, so scanned legitimately exceeds max — no /max
          // denominator, no cap.)
          case 'board_analyze_start':
            boardsScanned++;
            rackCount.textContent = `${boardsScanned} scanned`;
            break;

          // A downloadable board was found — render its pad immediately (full
          // dict), replacing a skeleton, so results stream in during a long scan.
          case 'board_result':
            if (data && data.board) renderProgressive(data.board);
            break;

          // Progress-only events. These do NOT carry full board objects (no name /
          // total_count / views) and fire for every analyzed board incl. play-only
          // and filtered ones, so they neither render pads nor count (counting here
          // double-incremented the scanned tally). Pads come solely from `results`.
          // Server hit its time budget (dated searches can be slow) — results
          // that follow may be incomplete.
          case 'search_partial':
            if (data && data.message) showToast(data.message, 'error');
            break;

          case 'board_parsed':
          case 'board_filter_result':
          case 'search_page_parsed':
          case 'board_date_scan_result':
          case 'track_last_modified':
            break;

          // ── FINAL RESULTS ──
          // Authoritative array of all passing boards. Reconcile the grid:
          // replace any remaining skeletons, add any boards not already shown.
          case 'results': {
            // Filter play-only boards ONCE here so the rendered pads and the
            // "N loaded" count agree (the server may include non-downloadable
            // boards that pass the view/sound filters).
            const boards = (Array.isArray(data) ? data : []).filter(b => b && b.has_downloads);
            reconcileResults(boards);
            finishSearch(boards.length);
            break;
          }

          // Stream-level error
          case 'error':
            showToast(
              (data && data.message)
                ? data.message
                : 'Couldn\'t reach soundboard.com — check your connection and try again.',
              'error'
            );
            finishSearch(0);
            break;

          // Server at concurrency cap
          case 'busy':
            showToast('Server is busy — try again in a moment.', 'error');
            finishSearch(0);
            break;
        }
      },
    });
  } catch (err) {
    if (err.name === 'AbortError') {
      // Normal cancellation (new search or navigation) — no toast
    } else {
      showToast('Couldn\'t reach soundboard.com — check your connection and try again.', 'error');
      finishSearch(0);
    }
  }
}

/**
 * Insert a board as a real pad in place of the next skeleton.
 * Used when progress events carry full board data.
 */
/** Render one board's pad as soon as it's found (progressive streaming). */
function renderProgressive(board) {
  if (!board || !board.has_downloads || padMap.has(board.board)) return;
  rackEmpty.hidden = true;
  const skeleton = padGrid.querySelector('.pad--skeleton');
  const pad = createPad(board, 0);
  if (skeleton) padGrid.replaceChild(pad, skeleton);
  else padGrid.appendChild(pad);
  padMap.set(board.board, pad);
}

function reconcileResults(boards) {
  // Remove leftover skeletons
  padGrid.querySelectorAll('.pad--skeleton').forEach(s => s.remove());

  // This tool downloads sounds — play-only boards aren't useful here. The server
  // already returns downloadable boards only; filter defensively just in case.
  boards = boards.filter(b => b && b.has_downloads);

  // Remember the set so the sort control can re-order it without re-searching.
  lastResults = boards;

  if (!boards.length) {
    rackEmpty.hidden = false;
    rackEmpty.querySelector('p').textContent =
      'No downloadable boards found. Try a different search.';
    return;
  }

  rackEmpty.hidden = true;
  // Pads were mostly rendered progressively as they streamed in; renderBoards
  // reuses those existing nodes and reorders them into the final sorted order
  // (creating a pad only for any board not already shown).
  renderBoards(boards);
}

/** Return a new array sorted by the given key (does not mutate input). */
function sortBoards(boards, key) {
  const arr = boards.slice();
  if (key === 'recent') {
    // Newest approximate-updated first; boards with no date sink to the bottom.
    arr.sort((a, b) => {
      const ta = a.approx_updated ? Date.parse(a.approx_updated) : -Infinity;
      const tb = b.approx_updated ? Date.parse(b.approx_updated) : -Infinity;
      return tb - ta;
    });
  } else {
    arr.sort((a, b) => (b.views_int || 0) - (a.views_int || 0));
  }
  return arr;
}

/** Clear the grid and render the given boards in order (used for re-sort). */
function renderBoards(boards) {
  // Reorder in place, REUSING existing pad nodes — appendChild moves an existing
  // child to its new position, so an in-flight download's closure/progress stays
  // attached (recreating the node would detach it and let a duplicate download
  // start). Only build a pad for a board not already on screen.
  boards.forEach((board, i) => {
    let pad = padMap.get(board.board);
    if (!pad) {
      pad = createPad(board, Math.min(i * 30, 400));
      padMap.set(board.board, pad);
    }
    padGrid.appendChild(pad);
  });
}

/**
 * Re-order the currently displayed results when the sort control changes —
 * no re-search needed. Exception: switching to "recent" when the current
 * results carry no dates (a views search doesn't fetch them) re-runs the
 * search so the server fetches Last-Modified dates and sorts authoritatively.
 */
function applySortChange() {
  if (!lastResults.length) return;            // nothing on screen yet
  const key = sortSelect.value;
  if (key === 'recent' && !lastResults.some(b => b.approx_updated)) {
    if (lastQuery) startSearch(lastQuery);    // need dates → re-search with sort=recent
    return;
  }
  lastResults = sortBoards(lastResults, key);
  renderBoards(lastResults);
}

/** Exit the searching UI state. */
function finishSearch(resultCount) {
  searchBtn.classList.remove('is-loading');
  searchBtn.disabled = false;
  rackEl.classList.remove('is-searching');

  if (resultCount > 0) {
    rackCount.textContent  = `${resultCount} board${resultCount !== 1 ? 's' : ''} loaded`;
    rackStatus.textContent = '';
    // Cue the first sound, and explain the browser hand-off once per page.
    const firstChip = padGrid.querySelector('.sound-chip[data-id]:not(.is-saved)');
    if (firstChip && !padGrid.querySelector('.sound-chip.is-next')) firstChip.classList.add('is-next');
    if (firstChip && !saveHintShown) {
      saveHintShown = true;
      showToast('Click a sound to save it. soundboard.com may ask you to verify you are human in a small window first.', '');
    }
  } else if (padGrid.children.length === 0) {
    // Nothing to show
    rackCount.textContent  = '';
    rackStatus.textContent = 'No results found.';
    rackEmpty.hidden = false;
  }

  searchAbort = null;
}

/* ═══════════════════════════════════════════════════════════════
   TOAST NOTIFICATIONS
   aria-live="polite" on the container ensures screen readers
   announce toasts without stealing focus.
   ═══════════════════════════════════════════════════════════════ */

/**
 * Show a brief toast notification.
 * @param {string} msg
 * @param {'success'|'error'|''} [type='']
 */
function showToast(msg, type = '') {
  const toast = document.createElement('div');
  toast.className = `toast${type ? ` toast--${type}` : ''}`;
  toast.textContent = msg;
  toastContainer.appendChild(toast);

  // Dismiss after 4 seconds
  setTimeout(() => {
    toast.classList.add('is-dismissing');
    // Remove from DOM after the CSS transition completes (300ms)
    toast.addEventListener('transitionend', () => toast.remove(), { once: true });
    // Hard fallback in case transitionend doesn't fire
    setTimeout(() => toast.remove(), 500);
  }, 4000);
}

/* ═══════════════════════════════════════════════════════════════
   EVENT WIRING
   ═══════════════════════════════════════════════════════════════ */

// Search form submit
searchForm.addEventListener('submit', (e) => {
  e.preventDefault();
  const q = searchInput.value.trim();
  if (!q) {
    searchInput.focus();
    return;
  }
  startSearch(q);
});

// Sort control → re-order results already on screen (no re-search for views).
sortSelect.addEventListener('change', applySortChange);

// Cancel the active search stream when the user navigates away.
// This signals the server (via broken connection) to stop the worker.
window.addEventListener('beforeunload', () => {
  if (searchAbort) searchAbort.abort();
});

/* ═══════════════════════════════════════════════════════════════
   INITIALISATION
   ═══════════════════════════════════════════════════════════════ */


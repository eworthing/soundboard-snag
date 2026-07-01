/**
 * app.js — Soundboard Snag web UI
 *
 * Vanilla JS, no build step, no dependencies.
 * Assumes the server exposes:
 *   GET  /api/search?q=&max=&min_views=&min_sounds=&sort=&include_dates=&recent_days=
 *          → SSE stream
 *   POST /api/download   body: {board, download_root?}
 *          → SSE stream
 *   GET  /api/board/<board>
 *          → JSON {board, has_downloads, total_count, sounds:[{id,title}], error|null}
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

/** In-flight downloads (board id → AbortController) so Stop can cancel them. */
const activeDownloads = new Map();

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
const cliCommandEl    = $('cli-command');
const copyBtn         = $('copy-cmd');
const cliPasteInput   = $('cli-paste');
const padGrid         = $('pad-grid');
const rackEmpty       = $('rack-empty');
const rackCount       = $('rack-count');
const rackStatus      = $('rack-status');
const rackEl          = $('rack');
const toastContainer  = $('toast-container');
const powerLed        = $('power-led');
const commandBar      = $('command-bar');
const serverAddress   = $('server-address');

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
 * @param {string}   [opts.method='GET']
 * @param {string}   [opts.body=null]     JSON string for POST
 * @param {AbortSignal} opts.signal        required — connect to AbortController
 * @param {function} opts.onEvent         called as onEvent(type, dataObject)
 * @returns {Promise<void>}  resolves when stream ends; rejects on network error
 */
async function streamSSE(url, { method = 'GET', body = null, signal, onEvent }) {
  const init = {
    method,
    headers: { Accept: 'text/event-stream' },
    signal,
  };
  if (body != null) {
    init.headers['Content-Type'] = 'application/json';
    init.body = body;
  }

  const resp = await fetch(url, init);

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
   CLI MIRROR — build and parse
   ═══════════════════════════════════════════════════════════════ */

/**
 * Quote a string for insertion into a shell command.
 * If the string contains spaces or shell-special characters, wraps it
 * in double quotes and escapes any internal double quotes.
 */
function shellQuote(s) {
  if (/[\s"'\\&|;<>()$`!#*?{}[\]~]/.test(s)) {
    return `"${s.replace(/\\/g, '\\\\').replace(/"/g, '\\"')}"`;
  }
  return s;
}

/**
 * Build the exact runnable CLI command from the current control state.
 * Flags are omitted when they equal their default values (cleaner output).
 * Matches the behaviour of main() in soundboard-snag.py.
 */
function buildCliCommand() {
  const q      = searchInput.value.trim();
  const minV   = minViewsInput.value.trim();
  const minS   = minSoundsInput.value.trim();
  const max    = maxInput.value.trim();
  const sort   = sortSelect.value;
  const dates  = includeDates.checked;

  const parts = ['soundboard-snag.py'];

  if (q)                      parts.push(`--search ${shellQuote(q)}`);
  if (minV && minV !== '0')   parts.push(`--min-views ${minV}`);
  if (minS && minS !== '0')   parts.push(`--min-sounds ${minS}`);
  if (max  && max  !== '20')  parts.push(`--max ${max}`);
  if (sort && sort !== 'views') parts.push(`--sort ${sort}`);

  // --sort recent implies --include-dates on the CLI side; only add it
  // standalone when sorting by views (avoids redundancy).
  if (dates && sort !== 'recent') parts.push('--include-dates');

  return parts.join(' ');
}

/** Update the CLI mirror display element. */
function refreshCliMirror() {
  cliCommandEl.textContent = buildCliCommand();
}

/**
 * Minimal shell tokeniser that handles single and double quotes.
 * Returns an array of token strings (flags and their values).
 * Used by parseAndApplyCommand for the reverse-mode paste.
 */
function tokenizeShell(input) {
  const tokens = [];
  let   i      = 0;

  while (i < input.length) {
    // Skip whitespace between tokens
    while (i < input.length && /\s/.test(input[i])) i++;
    if (i >= input.length) break;

    let token = '';
    const q = input[i];

    if (q === '"' || q === "'") {
      i++; // skip opening quote
      while (i < input.length && input[i] !== q) {
        // Handle backslash escapes inside double-quoted strings
        if (input[i] === '\\' && q === '"' && i + 1 < input.length) {
          i++;
          token += input[i++] ?? '';
        } else {
          token += input[i++];
        }
      }
      i++; // skip closing quote (or end-of-string if unmatched)
    } else {
      // Unquoted: consume until whitespace
      while (i < input.length && !/\s/.test(input[i])) {
        token += input[i++];
      }
    }

    if (token.length) tokens.push(token);
  }

  return tokens;
}

/**
 * Parse a `soundboard-snag.py …` command string and apply its flags
 * to the UI controls. Best-effort: unknown flags and args are skipped.
 *
 * Both `python3 soundboard-snag.py …` and bare `soundboard-snag.py …`
 * are accepted.
 */
function parseAndApplyCommand(rawCmd) {
  const cmd = rawCmd.trim();
  if (!cmd) return;

  // Strip the interpreter + script name prefix
  const stripped = cmd.replace(/^(python3?\s+)?soundboard-snag\.py\s*/, '').trim();
  const tokens   = tokenizeShell(stripped);

  // Reset controls to defaults before applying parsed values
  searchInput.value   = '';
  minViewsInput.value = '';
  minSoundsInput.value = '';
  maxInput.value      = '20';
  sortSelect.value    = 'views';
  includeDates.checked = false;

  let i = 0;
  while (i < tokens.length) {
    const tok = tokens[i];

    switch (tok) {
      case '--search':
      case '-s':
        searchInput.value = tokens[++i] ?? '';
        break;
      case '--min-views':
        minViewsInput.value = tokens[++i] ?? '';
        break;
      case '--min-sounds':
        minSoundsInput.value = tokens[++i] ?? '';
        break;
      case '--max':
        maxInput.value = tokens[++i] ?? '20';
        break;
      case '--sort':
        sortSelect.value = tokens[++i] ?? 'views';
        break;
      case '--include-dates':
        includeDates.checked = true;
        break;
      case '--recent-days':
        // Legacy hard cutoff is gone — map it to "sort by recently updated".
        i++;  // consume the day count
        sortSelect.value = 'recent';
        break;
      default:
        // Unknown flag: if the next token looks like a value (not a flag),
        // consume it so we stay in sync.
        if (tok.startsWith('-') && i + 1 < tokens.length && !tokens[i + 1].startsWith('-')) {
          i++;
        }
    }
    i++;
  }

  refreshCliMirror();
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
         aria-label="Grab sounds from ${escHtml(board.name)}"
       >
         <span class="grab-label">Grab</span>
         <svg class="grab-spinner" width="13" height="13" viewBox="0 0 13 13" aria-hidden="true" focusable="false">
           <circle cx="6.5" cy="6.5" r="4.5" stroke="currentColor" stroke-width="2.2" fill="none" stroke-dasharray="20" stroke-dashoffset="7"/>
         </svg>
       </button>`
    : `<span class="pad__stat" style="color:var(--rec);font-size:0.62rem;">play-only</span>`;

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
      <span class="pad__progress" aria-live="polite" aria-atomic="true"></span>
    </div>
    <!-- Sound chips — populated on expand -->
    <div class="pad__sounds" hidden></div>
  `;

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
      startDownload(el, board);
    });
  }

  return el;
}

/* ═══════════════════════════════════════════════════════════════
   SOUND EXPAND — fetch /api/board/<board> and show sound chips
   ═══════════════════════════════════════════════════════════════ */

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

  // Already loaded — don't re-fetch
  if (padEl.dataset.soundsLoaded) return;

  soundsEl.innerHTML = `<span class="pad__sounds-msg">Loading…</span>`;

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
      soundsEl.innerHTML = sounds
        .map(s => `<button type="button" class="sound-chip" data-id="${escHtml(s.id)}" data-title="${escHtml(s.title)}" title="Download — ${escHtml(s.title)}">${escHtml(s.title)}</button>`)
        .join('');
      soundsEl.querySelectorAll('.sound-chip').forEach(chip => {
        chip.addEventListener('click', (e) => {
          e.stopPropagation();  // don't collapse the pad
          downloadSound(chip, board.board, chip.dataset.id, chip.dataset.title);
        });
      });
    }

    padEl.dataset.soundsLoaded = '1';
  } catch (err) {
    if (err.name === 'AbortError') return;
    soundsEl.innerHTML = `<span class="pad__sounds-msg is-error">Couldn't reach soundboard.com — check your connection and try again.</span>`;
  }
}

/**
 * Download a single sound (one chip click). POST /api/download-sound returns a
 * small JSON result; reflect it on the chip + a toast.
 */
async function downloadSound(chip, board, soundId, title) {
  if (chip.dataset.busy) return;
  chip.dataset.busy = '1';
  chip.classList.add('is-downloading');
  try {
    const resp = await fetch('/api/download-sound', {
      method:  'POST',
      headers: { 'Content-Type': 'application/json' },
      body:    JSON.stringify({ board, sound_id: soundId, title }),
    });
    const data = await resp.json().catch(() => ({}));
    chip.classList.remove('is-downloading');
    if (resp.ok && (data.status === 'saved' || data.status === 'exists')) {
      chip.classList.add('is-saved');
      showToast(
        data.status === 'exists' ? `Already saved: ${data.name}` : `Saved ${data.name}`,
        'success'
      );
    } else {
      chip.classList.add('is-error');
      const why = data.error || (resp.status === 503 ? 'server busy' : `error ${resp.status}`);
      showToast(`Couldn't save that sound — ${why}`, 'error');
    }
  } catch (_) {
    chip.classList.remove('is-downloading');
    chip.classList.add('is-error');
    showToast('Couldn\'t reach soundboard.com — check your connection and try again.', 'error');
  } finally {
    delete chip.dataset.busy;
  }
}

/* ═══════════════════════════════════════════════════════════════
   DOWNLOAD — POST /api/download  →  SSE events
   ═══════════════════════════════════════════════════════════════ */

async function startDownload(padEl, board) {
  const grabBtn    = padEl.querySelector('.pad__grab');
  const progressEl = padEl.querySelector('.pad__progress');

  if (!grabBtn || grabBtn.disabled) return;

  grabBtn.classList.add('is-loading');
  grabBtn.disabled = true;
  progressEl.classList.add('is-visible');
  progressEl.textContent = 'Starting…';

  const dlAbort = new AbortController();
  activeDownloads.set(board.board, dlAbort);

  try {
    await streamSSE('/api/download', {
      method: 'POST',
      body:   JSON.stringify({ board: board.board }),
      signal: dlAbort.signal,
      onEvent(type, data) {
        switch (type) {

          // Download began; we know total sound count
          case 'download_start':
            progressEl.textContent = `0 / ${data.total}`;
            break;

          // Board HTML parsed (count confirmed)
          case 'board_parsed':
            if (data.count != null) {
              progressEl.textContent = `0 / ${data.count}`;
            }
            break;

          // Individual file events — show "i / n" in mono tabular style
          case 'file_start':
            progressEl.textContent = `file ${data.i} / ${data.n}`;
            break;

          case 'file_saved':
            progressEl.textContent = `${data.i} / ${data.n} · ${escHtml(data.name)}`;
            break;

          case 'file_skipped':
            progressEl.textContent = `${data.i} / ${data.n} · skipped`;
            break;

          case 'file_failed':
            progressEl.textContent = `${data.i} / ${data.n} · failed`;
            break;

          // Done — pulse the pad teal and toast
          case 'download_complete':
            handleDownloadComplete(padEl, grabBtn, progressEl, data);
            break;

          // Server cancelled the download mid-run
          case 'download_aborted':
            progressEl.textContent = `Aborted: ${data.reason || 'cancelled'}`;
            resetGrabBtn(grabBtn, progressEl, 3500);
            break;

          // Fatal download error
          case 'download_error':
            showToast(
              data.error
                ? `Download error: ${data.error}`
                : 'Couldn\'t reach soundboard.com — check your connection and try again.',
              'error'
            );
            resetGrabBtn(grabBtn, progressEl, 0);
            break;

          // Server at concurrency cap
          case 'busy':
            showToast('Server is busy — try again in a moment.', 'error');
            resetGrabBtn(grabBtn, progressEl, 0);
            break;
        }
      },
    });
  } catch (err) {
    if (err.name === 'AbortError') {
      // User navigated away / clicked Stop — silent
    } else {
      showToast('Couldn\'t reach soundboard.com — check your connection and try again.', 'error');
      resetGrabBtn(grabBtn, progressEl, 0);
    }
  } finally {
    activeDownloads.delete(board.board);
  }
}

/**
 * Handle a successful download_complete event.
 * Pulses the pad teal, toasts the result, resets the button.
 */
function handleDownloadComplete(padEl, grabBtn, progressEl, data) {
  const { snagged = 0, existing = 0, failed = 0 } = data;

  // Button: Grab → Grabbed (briefly), then back to Grab
  grabBtn.classList.remove('is-loading');
  grabBtn.disabled = false;
  const labelEl = grabBtn.querySelector('.grab-label');
  if (labelEl) labelEl.textContent = 'Grabbed';

  // Teal pulse on the pad
  padEl.classList.add('just-grabbed');
  padEl.addEventListener('animationend', () => {
    padEl.classList.remove('just-grabbed');
  }, { once: true });

  // Build toast message
  const soundWord = snagged === 1 ? 'sound' : 'sounds';
  let msg = `Grabbed ${snagged} ${soundWord}`;
  if (existing > 0) msg += ` (${existing} already existed)`;
  if (failed > 0)   msg += `, ${failed} failed`;
  if (data.path)    msg += ` → ${data.path}`;
  showToast(msg, 'success');

  // Reset after 4s
  setTimeout(() => {
    progressEl.textContent = '';
    progressEl.classList.remove('is-visible');
    if (labelEl) labelEl.textContent = 'Grab';
  }, 4000);
}

/** Reset a GRAB button + progress span back to idle state. */
function resetGrabBtn(grabBtn, progressEl, delayMs) {
  const reset = () => {
    grabBtn.classList.remove('is-loading');
    grabBtn.disabled = false;
    progressEl.textContent = '';
    progressEl.classList.remove('is-visible');
    const labelEl = grabBtn.querySelector('.grab-label');
    if (labelEl) labelEl.textContent = 'Grab';
  };
  if (delayMs > 0) {
    setTimeout(reset, delayMs);
  } else {
    reset();
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
function insertProgressivePad(board) {
  if (padMap.has(board.board)) return; // already shown

  const skeleton = padGrid.querySelector('.pad--skeleton');
  const delay    = padMap.size * 40; // gentle stagger
  const pad      = createPad(board, delay);

  if (skeleton) {
    padGrid.replaceChild(pad, skeleton);
  } else {
    padGrid.appendChild(pad);
  }

  padMap.set(board.board, pad);
}

/**
 * After the `results` event: clear remaining skeletons and add any
 * boards from the final array that aren't already in the grid.
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
   POWER-ON SEQUENCE
   A ~600ms one-shot visual moment on page load:
   1. Amber LED flicker (device-header__led)
   2. Command-bar backlight flash (slight delay)
   Both are CSS animations triggered by adding a class.
   Wrapped in a prefers-reduced-motion check:
   if the user has reduced motion enabled the classes still get
   added but the @media rule in styles.css cuts duration to ~0ms.
   ═══════════════════════════════════════════════════════════════ */

function runPowerOn() {
  powerLed.classList.add('power-on');
  powerLed.addEventListener('animationend', () => {
    powerLed.classList.remove('power-on');
  }, { once: true });

  // Command-bar flicker starts 100ms after the LED (see CSS animation-delay)
  commandBar.classList.add('power-on');
  commandBar.addEventListener('animationend', () => {
    commandBar.classList.remove('power-on');
  }, { once: true });
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

// All controls → refresh the CLI mirror on any change
[minViewsInput, minSoundsInput, maxInput, sortSelect, includeDates, searchInput]
  .forEach(el => {
    el.addEventListener('input',  refreshCliMirror);
    el.addEventListener('change', refreshCliMirror);
  });

// Sort control → re-order results already on screen (no re-search for views).
sortSelect.addEventListener('change', applySortChange);

// Stop button → shut down the local server (this is how you "quit" the app).
const stopServerBtn = document.getElementById('stop-server');
if (stopServerBtn) {
  stopServerBtn.addEventListener('click', async () => {
    stopServerBtn.disabled = true;
    if (searchAbort) searchAbort.abort();
    // Cancel any in-flight downloads so files stop landing after "quit".
    activeDownloads.forEach(c => c.abort());
    activeDownloads.clear();
    try { await fetch('/api/shutdown', { method: 'POST' }); } catch (_) { /* server going down */ }
    document.body.innerHTML =
      '<div class="stopped-screen"><strong>Server stopped.</strong>' +
      '<span>You can close this tab. Re-open Soundboard Snag to start again.</span></div>';
  });
}

// Copy CLI command to clipboard
copyBtn.addEventListener('click', async () => {
  const text = cliCommandEl.textContent;
  try {
    await navigator.clipboard.writeText(text);
    copyBtn.classList.add('is-copied');
    copyBtn.setAttribute('aria-label', 'Copied!');
    setTimeout(() => {
      copyBtn.classList.remove('is-copied');
      copyBtn.setAttribute('aria-label', 'Copy command to clipboard');
    }, 1500);
  } catch (_) {
    // Clipboard API unavailable — fall back to selecting the text
    const range = document.createRange();
    range.selectNode(cliCommandEl);
    const sel = window.getSelection();
    if (sel) {
      sel.removeAllRanges();
      sel.addRange(range);
    }
  }
});

// Paste CLI command → parse and set controls (reverse mode)
// Triggers as soon as the input contains a recognisable command.
cliPasteInput.addEventListener('input', () => {
  const val = cliPasteInput.value.trim();
  if (val.includes('soundboard-snag')) {
    parseAndApplyCommand(val);
    // Clear the paste field after a short delay so the user sees the controls update
    setTimeout(() => { cliPasteInput.value = ''; }, 400);
  }
});

// Cancel the active search stream when the user navigates away.
// This signals the server (via broken connection) to stop the worker.
window.addEventListener('beforeunload', () => {
  if (searchAbort) searchAbort.abort();
});

/* ═══════════════════════════════════════════════════════════════
   INITIALISATION
   ═══════════════════════════════════════════════════════════════ */

// Update the server-address badge to the actual host
serverAddress.textContent = `v · ${window.location.host || '127.0.0.1:8765'}`;

// Build the initial CLI mirror (shows bare `soundboard-snag.py`)
refreshCliMirror();

// Run the power-on animation
runPowerOn();

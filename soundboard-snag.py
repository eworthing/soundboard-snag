#!/usr/bin/env python3
"""
Soundboard Snag
A macOS desktop app for finding soundboard.com boards and saving their sounds
with clean, normalized filenames.

    python3 soundboard-snag.py            # open the app window
    python3 soundboard-snag.py --serve    # developer mode: headless web server

The app is a native window (pywebview) around a small local web UI. Search and
board lookups run in-process. soundboard.com puts a human check in front of
every download that this tool does not bypass: clicking a sound opens its
download page in a window, you pass the check, and the sound is saved into
``~/Downloads/Soundboard Snag/<board>/`` under a clean name.

Requirements: macOS, Python 3.9+, and ``pip install -r requirements.txt``.
The engine itself (search, parsing, the local server) uses only the standard
library, so ``--serve`` and the unit tests need no extra packages.

Approximate updated dates are inferred from HTTP Last-Modified headers on a few
tracks per board: best-effort, not authoritative.
"""

import argparse
import filecmp
import html
import os
import re
import threading
import time
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from typing import List, NamedTuple, Optional, Tuple
from urllib.parse import urlparse, quote, unquote
from urllib.request import Request, urlopen
from urllib.error import HTTPError, URLError


# Module-level constants
BASE_URL = "https://soundboard.com"  # canonical host; www. answers every request with a 308 redirect
USER_AGENT = 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36'
REQUEST_DELAY = 0.5  # Delay between requests in seconds (be respectful to server)
HTTP_TIMEOUT = 10  # Network timeout for requests (seconds)
HEADER_REQUEST_DELAY = 0.5  # Delay between track header checks (same pace as page requests)
MAX_DATE_PROBES_PER_BOARD = 10  # Hard cap on track header checks per board when inferring dates


def _site_says_stop(code):
    """True for HTTP statuses that mean "back off": forbidden, rate-limited, or server errors.

    On these the tool stops what it is doing instead of carrying on through
    the remaining boards at the same pace.
    """
    try:
        code = int(code)
    except (TypeError, ValueError):
        return False
    return code in (403, 429) or 500 <= code < 600
WINDOWS_RESERVED_NAMES = {'CON', 'PRN', 'AUX', 'NUL'}
WINDOWS_RESERVED_NAMES.update({f'COM{i}' for i in range(1, 10)})
WINDOWS_RESERVED_NAMES.update({f'LPT{i}' for i in range(1, 10)})


class BoardResult(NamedTuple):
    """A single soundboard search result.

    Replaces the former 11-field positional tuple returned by ``search_boards``
    so callers reference fields by name instead of fragile index positions
    (``r[8]``, ``x[9]``...). Being a ``NamedTuple`` it is still a plain tuple at
    runtime — indexing and unpacking continue to work — so introducing it is
    behavior-preserving.
    """
    board_name: str
    has_downloads: bool
    sounds_info: List[Tuple[str, str]]
    total_count: int
    board_desc: str
    category: str
    views: str
    tags: List[str]
    views_int: int
    approx_updated: Optional[datetime]
    approx_source: Optional[str]
    title: Optional[str] = None  # human-readable board title (best-effort; falls back to slug)
    image: Optional[str] = None  # board cover-art URL (best-effort; None when no custom icon)


def board_result_to_dict(board):
    """Serialize a BoardResult into a JSON-safe dict.

    Shared by the ``--json`` CLI mode and the web search API so both speak the
    same shape. ``board`` is the identifier a client echoes back to
    ``/api/download`` / ``/api/board`` (it is ``board_name``, the same value the
    CLI quotes via ``_quote_path_segment`` to build ``/sb/<…>`` URLs — there is no
    separate raw-slug field in the data model). ``approx_updated`` is ISO-8601
    UTC or ``None``.
    """
    return {
        "board": board.board_name,
        "name": board.title or board.board_name,
        "title": board.title,
        "image": board.image,
        "has_downloads": bool(board.has_downloads),
        "sounds": [{"id": sid, "title": title} for sid, title in board.sounds_info],
        "total_count": board.total_count,
        "description": board.board_desc,
        "category": board.category,
        "views": board.views,
        "views_int": board.views_int,
        "tags": list(board.tags),
        "approx_updated": board.approx_updated.isoformat() if board.approx_updated else None,
        "approx_source": board.approx_source,
    }


class ParsedBoard(NamedTuple):
    """Fields parsed from a single board page's HTML.

    Everything here is a pure function of the board-page markup, so it can be
    unit-tested against a saved HTML fixture with no network. Network-derived
    data (the approximate updated date) and filter/render decisions stay in
    ``search_boards``.
    """
    sound_matches: List[Tuple[str, str]]
    has_downloads: bool
    download_ids: List[str]
    board_desc: str
    category: str
    views: str
    views_int: int
    tags: List[str]
    sounds_info: List[Tuple[str, str]]
    sound_count: int


def _extract_board_title(board_html):
    """Best-effort human-readable board title from board-page HTML.

    Prefers the page ``<h1>`` (e.g. "Star wars Battle sounds"); falls back to
    ``<title>`` minus the " - Soundboard.com ..." site suffix. Returns a cleaned
    display string, or None when nothing usable is found (caller falls back to
    the slug). Pure/no I/O.
    """
    raw = None
    h1 = re.search(r'<h1[^>]*>(.*?)</h1>', board_html, re.DOTALL | re.IGNORECASE)
    if h1:
        raw = re.sub(r'<[^>]+>', '', h1.group(1))
    else:
        t = re.search(r'<title>(.*?)</title>', board_html, re.DOTALL | re.IGNORECASE)
        if t:
            raw = re.split(r'\s*-\s*Soundboard\.com', t.group(1), 1)[0]
    if raw is None:
        return None
    title = re.sub(r'\s+', ' ', html.unescape(raw)).strip()
    return title or None


def _extract_board_image(board_html):
    """Best-effort board cover-art URL from board-page HTML.

    soundboard.com renders the board icon as a ``page-bg`` background image,
    e.g. ``url(/boardicon/<slug>.jpg)``. Returns an absolute URL, or None when
    the board has no custom icon (the default ``/images/unknown.png`` path is
    not under ``/boardicon/`` so it is correctly ignored → caller/UI fall back
    to a placeholder). Pure/no I/O.
    """
    # Prefer the board's own page-bg icon; only then fall back to any /boardicon
    # reference (a related-boards sidebar can otherwise win and show wrong art).
    pb = re.search(r'class=["\']page-bg["\'][^>]*url\((/boardicon/[^\s"\')]+)\)', board_html, re.IGNORECASE)
    if pb:
        return BASE_URL + pb.group(1)
    m = re.search(r'/boardicon/[^\s"\')]+', board_html)
    return (BASE_URL + m.group(0)) if m else None


def _parse_board_html(board_html):
    """Parse one board page's HTML into a ParsedBoard (pure; no network)."""
    # Sound IDs and titles
    sound_matches = re.findall(r'data-src="(\d+)".*?<span>([^<]+)</span>', board_html, re.DOTALL)

    # Download buttons present?
    download_pattern = r'<a href="/sb/sound/\d+"[^>]*class="[^"]*btn-download-track'
    has_downloads = re.search(download_pattern, board_html) is not None

    # Downloadable sound IDs (more reliable for date checks than data-src), de-duplicated in order
    download_ids_raw = re.findall(r'<a href="/sb/sound/(\d+)"[^>]*class="[^"]*btn-download-track', board_html)
    download_ids = []
    seen_download_ids = set()
    for sid in download_ids_raw:
        if sid not in seen_download_ids:
            download_ids.append(sid)
            seen_download_ids.add(sid)

    # Description
    desc_match = re.search(r'<p class="item-desc[^"]*"[^>]*>([^<]*)</p>', board_html)
    board_desc = html.unescape(desc_match.group(1).strip()) if desc_match and desc_match.group(1).strip() else ""

    # Category
    cat_match = re.search(r'<strong>Category:\s*</strong>\s*<span class="text-muted">\s*([^<]+)</span>', board_html)
    category = html.unescape(cat_match.group(1).strip()) if cat_match else ""

    # Views
    views_match = re.search(r'<strong>Views:\s*</strong>\s*<span class="text-muted">\s*([^<]+)</span>', board_html)
    views = html.unescape(views_match.group(1).strip()) if views_match else ""

    # Tags
    tags = []
    tags_match = re.search(r'<strong>Tags:\s*</strong>(.*?)</div>', board_html, re.DOTALL)
    if tags_match:
        tags = [html.unescape(t.strip()) for t in re.findall(r'<a[^>]*>([^<]+)</a>', tags_match.group(1)) if t.strip()]

    # Preview filenames (first 10), titles cleaned
    sounds_info = [(sid, html.unescape(title.strip())) for sid, title in sound_matches[:10]]

    return ParsedBoard(
        sound_matches=sound_matches,
        has_downloads=has_downloads,
        download_ids=download_ids,
        board_desc=board_desc,
        category=category,
        views=views,
        views_int=_parse_views_count(views),
        tags=tags,
        sounds_info=sounds_info,
        sound_count=len(sound_matches),
    )


def _evaluate_filters(views_int, sound_count, approx_updated,
                      min_views, min_sounds, recent_threshold, recent_days):
    """Decide whether a board passes the active search filters (pure).

    Returns ``(meets, failures)`` where ``failures`` is a list of
    ``(bucket_key, reason)`` tuples in evaluation order. ``bucket_key`` matches
    the ``skipped_buckets`` keys so the caller can attribute skips; ``reason`` is
    the human-readable explanation. The date filter is only evaluated when the
    basic (views/sounds) filters pass, matching the original skip-accuracy rule.
    """
    failures = []
    if min_views > 0 and views_int < min_views:
        failures.append(("views", f"views ({views_int}) < min_views ({min_views})"))
    if min_sounds > 0 and sound_count < min_sounds:
        failures.append(("sounds", f"sounds ({sound_count}) < min_sounds ({min_sounds})"))
    if recent_threshold is not None and not failures:
        if not approx_updated:
            failures.append(("updated_unknown", "updated date unavailable"))
        elif approx_updated < recent_threshold:
            failures.append(("updated_too_old", f"updated ({_format_date(approx_updated)}) older than {recent_days} days"))
    return (not failures, failures)


def _http_get(url):
    """Fetch a URL and return its decoded text (the default page fetcher).

    This is the production adapter of the fetch seam: ``search_boards`` accepts a
    ``fetch`` callable so tests can inject an in-memory fake instead of hitting
    the network. It raises ``HTTPError`` / ``URLError`` exactly like the inline
    ``urlopen`` it replaced, so existing error handling is unchanged.
    """
    req = Request(url, headers={'User-Agent': USER_AGENT})
    with urlopen(req, timeout=HTTP_TIMEOUT) as response:
        return response.read().decode('utf-8')


def _quote_path_segment(value):
    """Quote a URL path segment without double-encoding existing percent escapes."""
    # Keep '%' safe so values like "PRINS%20JULIUS" aren't double-encoded.
    return quote(value, safe="%-_.~")


def _parse_views_count(text):
    """Parse a views string into an integer.

    Handles comma-separated integers and compact forms like "1.2K" / "10k" / "3M".
    Returns 0 if parsing fails.
    """
    if not text:
        return 0

    value = text.strip().replace(',', '')
    if not value:
        return 0

    match = re.match(r'^(\d+(?:\.\d+)?)\s*([kKmM])?$', value)
    if not match:
        try:
            return int(value)
        except ValueError:
            return 0

    number = float(match.group(1))
    suffix = match.group(2)
    multiplier = 1
    if suffix:
        if suffix.lower() == 'k':
            multiplier = 1_000
        elif suffix.lower() == 'm':
            multiplier = 1_000_000

    return int(number * multiplier)


def _extract_board_slugs_from_search_html(html_content):
    """Extract board slugs from search result HTML.

    This is intentionally permissive to handle URL-encoded spaces/unicode.
    Returns URL path segments (may be percent-encoded).
    """
    # Prefer href parsing to avoid matching unrelated /sb/ occurrences.
    # Links may be root-relative ("/sb/x") or absolute
    # ("https://soundboard.com/sb/x"); the site switched to absolute in 2026.
    slugs = re.findall(
        r"href\s*=\s*['\"]?(?:https?://(?:www\.)?soundboard\.com)?/sb/([^'\"\s>#?]+)",
        html_content,
        flags=re.IGNORECASE,
    )
    return [html.unescape(s).strip() for s in slugs if s and s.strip()]


def _parse_http_datetime(value):
    """Parse HTTP datetime header values to a timezone-aware datetime."""
    if not value:
        return None

    try:
        dt = parsedate_to_datetime(value)
    except (TypeError, ValueError):
        return None

    if dt is None:
        return None

    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)

    return dt.astimezone(timezone.utc)


def _fetch_last_modified_detailed(url):
    """Fetch Last-Modified header for a URL (best-effort) with diagnostics.

    Returns:
        Tuple[datetime|None, str]: (last_modified_dt_utc, diagnostic_string)
    """
    headers = {'User-Agent': USER_AGENT}

    try:
        req = Request(url, headers=headers, method='HEAD')
        with urlopen(req, timeout=HTTP_TIMEOUT) as response:
            lm = response.headers.get('Last-Modified')
            dt = _parse_http_datetime(lm)
            diag = f"HEAD {response.getcode()}" + (" last-modified" if dt else " no-last-modified")
            return dt, diag
    except HTTPError as e:
        # Some servers block HEAD.
        if e.code not in (405, 501):
            return None, f"HEAD http_{e.code}"
    except URLError as e:
        return None, f"HEAD urlerror: {getattr(e, 'reason', str(e))}"
    except Exception as e:
        return None, f"HEAD error: {type(e).__name__}: {e}"

    # Fallback: some servers block HEAD, so request a single byte
    try:
        headers = {'User-Agent': USER_AGENT, 'Range': 'bytes=0-0'}
        req = Request(url, headers=headers)
        with urlopen(req, timeout=HTTP_TIMEOUT) as response:
            lm = response.headers.get('Last-Modified')
            dt = _parse_http_datetime(lm)
            diag = f"RANGE {response.getcode()}" + (" last-modified" if dt else " no-last-modified")
            return dt, diag
    except HTTPError as e:
        return None, f"RANGE http_{e.code}"
    except URLError as e:
        return None, f"RANGE urlerror: {getattr(e, 'reason', str(e))}"
    except Exception as e:
        return None, f"RANGE error: {type(e).__name__}: {e}"


def _format_date(dt):
    """Format a datetime for display (YYYY-MM-DD)."""
    return dt.strftime('%Y-%m-%d')


def _format_datetime_utc(dt):
    """Format a datetime in UTC for verbose/log output."""
    if not dt:
        return "unknown"
    try:
        dt = dt.astimezone(timezone.utc)
    except Exception:
        pass
    return dt.strftime('%Y-%m-%d %H:%M:%S UTC')


class SoundboardSnag:
    """Snags and manages soundboard audio files."""

    def __init__(self, soundboard_url, download_root=None, fetcher=None):
        """Parse a soundboard.com board URL; fetch and parse its page on demand.

        Args:
            soundboard_url: A string URL pointing to a soundboard.com page.
                Expected format: https://soundboard.com/sb/boardname
            download_root: Root directory the board's folder is created under.
                If None, uses the current directory.
            fetcher: Optional callable(url) -> decoded page text. Defaults to
                _http_get (real network). Tests inject an in-memory fake.

        Raises:
            ValueError: If the URL format is invalid or board name cannot
                be extracted.
        """
        self.url = urlparse(soundboard_url)
        self.board_slug, self.board_name = self._extract_board_slug_and_name()
        self.base_url = BASE_URL
        self.download_root = download_root if download_root else os.getcwd()
        self.fetcher = fetcher if fetcher is not None else _http_get

    def _extract_board_slug_and_name(self):
        """Extract the board slug (URL-safe) and a display name from the URL path.

        Returns:
            Tuple[str, str]: (board_slug, board_name_display)

        Raises:
            ValueError: If URL path is empty or board name cannot be
                determined.
        """
        if not self.url.path or self.url.path == "/":
            raise ValueError("Invalid URL: No soundboard path found. Expected format: https://www.soundboard.com/sb/boardname")

        slug = self.url.path.replace("/sb/", "").replace("/", "").strip()
        if not slug:
            raise ValueError("Could not extract board name from URL path")

        display = unquote(slug).strip()
        if not display:
            display = slug

        return slug, display

    def _board_url(self):
        return f"{self.base_url}/sb/{_quote_path_segment(self.board_slug)}"

    def _board_output_dirname(self):
        # Sanitize board name for use as a directory name (cross-platform).
        name = re.sub(r'[\\/]+', '_', self.board_name).strip()
        if not name:
            name = re.sub(r'[\\/]+', '_', self.board_slug).strip() or 'soundboard'
        # Remove characters invalid on Windows/macOS/Linux filesystems
        name = re.sub(r'[<>:"|?*]', '-', name)
        name = re.sub(r'[\x00-\x1f\x7f]', '', name)
        name = name.rstrip('. ')
        return name or 'soundboard'

    def _fetch_page(self):
        """Fetch the soundboard page content.

        Returns:
            The page HTML content as a UTF-8 decoded string.

        Raises:
            RuntimeError: If the page cannot be retrieved (HTTP errors or
                network errors).
        """
        page_url = self._board_url()

        try:
            return self.fetcher(page_url)
        except HTTPError as e:
            raise RuntimeError(f"HTTP Error {e.code}: {e.reason}")
        except URLError as e:
            raise RuntimeError(f"Network error: {e.reason}")

    def _check_downloads_enabled(self, html_content):
        """Check if the board has download buttons enabled.

        Args:
            html_content: The HTML content of the soundboard page.

        Returns:
            tuple: (has_downloads, download_count) where has_downloads is bool
                   and download_count is the number of download buttons found.
        """
        download_pattern = r'<a href="/sb/sound/\d+"[^>]*class="[^"]*btn-download-track'
        download_buttons = re.findall(download_pattern, html_content)
        return (len(download_buttons) > 0, len(download_buttons))

    def _parse_sound_items(self, html_content):
        """Extract sound IDs and titles from the page HTML."""
        # Pattern matches: data-src="ID" ... <span>Title</span>
        pattern = r'<div class="item r"[^>]*data-src="(\d+)"[^>]*>.*?<div class="item-title text-ellipsis">\s*<span>(.*?)</span>'
        matches = re.findall(pattern, html_content, re.DOTALL)

        if matches:
            return matches

        # Fallback: extract IDs only if pattern doesn't match
        id_pattern = r'<a href="/sb/sound/(\d+)"[^>]*class="btn-download-track"'
        sound_ids = re.findall(id_pattern, html_content)
        return [(sid, '') for sid in sound_ids]

    def _sanitize_filename(self, raw_filename, sound_id, page_title):
        """Clean and normalize a filename."""
        cleaned = raw_filename

        # Decode HTML entities (e.g., &#039; to ')
        cleaned = html.unescape(cleaned)

        # Remove UUID patterns (e.g., 227896-abc123-...)
        cleaned = re.sub(
            r'\d{6}-[a-f0-9]{8}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{12}',
            '',
            cleaned,
            flags=re.IGNORECASE
        )

        # Use page title if filename is empty after UUID removal
        if not cleaned.strip() or cleaned.strip().startswith('.'):
            if page_title and page_title.strip():
                cleaned = f"{page_title.strip()}.mp3"
            else:
                cleaned = f"audio_{sound_id}.mp3"

        # Normalize spacing and punctuation
        cleaned = cleaned.replace('_', ' ')
        cleaned = cleaned.replace('--', '-')
        cleaned = re.sub(r'\s+', ' ', cleaned)  # Multiple spaces to single
        cleaned = re.sub(r'\s*-\s*', ' - ', cleaned)  # Normalize hyphens
        cleaned = re.sub(r'\s+(\.[^.]+)$', r'\1', cleaned)  # Remove space before extension
        cleaned = cleaned.strip()

        # Sanitize invalid characters (cross-platform)
        cleaned = re.sub(r'[<>:"/\\|?*]', '-', cleaned)

        # Remove control characters (Windows compatibility)
        cleaned = re.sub(r'[\x00-\x1f\x7f]', '', cleaned)

        # Get name and extension separately for proper handling
        name, ext = os.path.splitext(cleaned)

        # Strip trailing dots and spaces from name (Windows requirement)
        name = name.rstrip('. ')

        # Title case if all lowercase (improves readability).
        # Skip all-uppercase names to preserve acronyms (e.g., "NASA", "HTML").
        if name and name.islower():
            name = name.title()

        # Handle Windows reserved names (CON, PRN, AUX, NUL, COM1-9, LPT1-9)
        name_upper = name.upper()
        if name_upper in WINDOWS_RESERVED_NAMES:
            name = f'_{name}'  # Prefix with underscore to make it safe

        # Truncate to stay within filesystem limits (255 bytes max on most OS)
        max_name_len = 255 - len(ext.encode('utf-8'))
        if len(name.encode('utf-8')) > max_name_len:
            while len(name.encode('utf-8')) > max_name_len and name:
                name = name[:-1]
            name = name.rstrip('. ')

        # Reconstruct filename
        cleaned = name + ext

        # Final safety check - ensure filename isn't empty
        if not cleaned or cleaned == '.mp3':
            cleaned = f'audio_{sound_id}.mp3'

        return cleaned


def search_boards(
    query,
    max_results=20,
    min_views=0,
    min_sounds=0,
    include_dates=False,
    recent_days=None,
    sort_by="views",
    date_sample_size=0,
    logger=None,
    fetch=None,
    cancel_event=None,
    time_budget=None,
):
    """Search for soundboards with detailed information including filenames, category, and tags.

    By default, filters are applied to improve search quality:
    - Minimum 10 views (filters out brand new/untested boards)
    - Minimum 3 sounds (filters out incomplete/test boards)
    Set min_views=0 and min_sounds=0 to disable filtering.

    Args:
        query: Search term(s)
        max_results: Maximum number of boards to check (will fetch multiple pages if needed)
        min_views: Minimum number of views required (default set by CLI, 0 = no filter)
        min_sounds: Minimum number of sounds required (default set by CLI, 0 = no filter)
        include_dates: If True, fetch approximate Last-Modified dates (extra requests)
        recent_days: If set, only include boards updated within the last N days (approx)
        sort_by: Sort results by "views" or "recent" (approx update date)
        logger: Optional sink with an ``event(name, **fields)`` method that receives progress events
        fetch: Optional callable(url) -> decoded page text. Defaults to _http_get
            (real network). Tests inject an in-memory fake to exercise the
            orchestration (pagination, dedup, early-stop, near-misses) offline.
    Returns:
        List[BoardResult]: one named record per board (fields accessed by name).
    """
    if fetch is None:
        fetch = _http_get
    encoded_query = quote(query)

    if logger:
        logger.event(
            "search_start",
            query=query,
            max_results=max_results,
            min_views=min_views,
            min_sounds=min_sounds,
            include_dates=bool(include_dates),
            recent_days=recent_days,
            sort_by=sort_by,
            date_sample_size=date_sample_size,
        )

    recent_threshold = None
    if recent_days is not None:
        recent_threshold = datetime.now(timezone.utc) - timedelta(days=recent_days)
        if logger:
            logger.event("recent_threshold", recent_days=recent_days, threshold_utc=_format_datetime_utc(recent_threshold))

    results = []
    downloadable_count = 0
    target_downloadable = max_results
    # Cache: url -> (datetime|None, diag_string)
    last_modified_cache = {}

    boards_analyzed_total = 0

    # When using --recent-days, keep track of the newest boards that would have
    # passed the basic filters (downloads/views/sounds) but missed the date window.
    # This lets us provide a helpful suggestion when the date filter yields no results.

    def fetch_last_modified_cached(url, with_diag=False):
        if url in last_modified_cache:
            dt, diag = last_modified_cache[url]
            return (dt, diag) if with_diag else dt

        dt, diag = _fetch_last_modified_detailed(url)
        last_modified_cache[url] = (dt, diag)
        return (dt, diag) if with_diag else dt

    # Fetch boards page by page, analyzing as we go
    seen = set()
    page = 1
    max_pages = 10  # Safety limit to prevent infinite loops
    keep_searching = True
    # Traffic safeguards (see _site_says_stop / MAX_DATE_PROBES_PER_BOARD):
    site_stop_code = None        # set when the site answers 403/429/5xx -> stop the search
    date_probes_blocked = False  # set when a track header check gets such an answer
    # Boards that fail the filters still cost a request each, so bound the total.
    max_boards_checked = max(30, int(max_results) * 3)
    boards_cap_hit = False

    # Optional wall-clock budget (seconds). Date inference (recent/--include-dates)
    # probes HTTP headers per track and can run for minutes on a popular term; the
    # web path passes a budget so the search returns partial results instead of
    # appearing to hang. None = unlimited (the CLI default).
    search_deadline = (time.monotonic() + time_budget) if time_budget else None
    time_budget_hit = False

    def _over_budget():
        return search_deadline is not None and time.monotonic() > search_deadline

    while keep_searching and page <= max_pages:
        # Cooperative cancellation (web client disconnect) — stop between pages.
        if cancel_event is not None and cancel_event.is_set():
            break
        if _over_budget():
            time_budget_hit = True
            break
        search_url = f"{BASE_URL}/search/{encoded_query}?page={page}" if page > 1 else f"{BASE_URL}/search/{encoded_query}"

        if logger:
            logger.event("search_page_fetch_start", page=page, url=search_url)

        try:
            html_content = fetch(search_url)
            if logger:
                logger.event("search_page_fetch_ok", page=page, url=search_url, bytes=len(html_content))
        except (HTTPError, URLError) as e:
            if logger:
                logger.event("search_page_fetch_error", page=page, url=search_url, error=str(e))
            if _site_says_stop(getattr(e, 'code', None)):
                site_stop_code = e.code
            break

        # Extract board slugs from this page (permissive; handles URL-encoded names)
        boards = _extract_board_slugs_from_search_html(html_content)
        if logger:
            logger.event("search_page_parsed", page=page, raw_board_slug_count=len(boards))

        page_boards = []
        for board in boards:
            board = html.unescape(board)
            board = board.strip().strip('/')
            if not board or '/' in board:
                continue

            board_display = unquote(board).strip() or board
            key = board_display.lower()
            if key in ['search', 'popular', 'new']:
                continue

            if key not in seen:
                seen.add(key)
                page_boards.append(board_display)

        # If no new boards found on this page, we've reached the end
        if not page_boards:
            if logger:
                logger.event("search_end_no_more_boards", page=page)
            break

        # Analyze boards from this page
        for board_index, board_name in enumerate(page_boards, 1):
            # Cooperative cancellation — stop between boards on client disconnect.
            if cancel_event is not None and cancel_event.is_set():
                keep_searching = False
                break
            # Wall-clock budget — return what we have rather than hang on a big
            # date-inference scan.
            if _over_budget():
                time_budget_hit = True
                keep_searching = False
                break
            # If we have enough downloadable boards, we can stop
            if downloadable_count >= target_downloadable:
                keep_searching = False
                break

            if boards_analyzed_total >= max_boards_checked:
                boards_cap_hit = True
                keep_searching = False
                break

            boards_analyzed_total += 1

            if logger:
                logger.event(
                    "board_analyze_start",
                    page=page,
                    page_index=board_index,
                    page_total=len(page_boards),
                    board=board_name,
                )

            try:
                board_url = f"{BASE_URL}/sb/{_quote_path_segment(board_name)}"
                board_html = fetch(board_url)
                if logger:
                    logger.event("board_fetch_ok", board=board_name, url=board_url, bytes=len(board_html))

                # Parse all board-page fields (pure; unit-tested via _parse_board_html)
                parsed = _parse_board_html(board_html)
                board_title = _extract_board_title(board_html)
                board_image = _extract_board_image(board_html)
                sound_matches = parsed.sound_matches
                has_downloads = parsed.has_downloads
                download_ids_deduped = parsed.download_ids
                board_desc = parsed.board_desc
                category = parsed.category
                views = parsed.views
                tags = parsed.tags
                sounds_info = parsed.sounds_info
                sound_count = parsed.sound_count
                views_int = parsed.views_int


                fails_basic_filters = (
                    (min_views > 0 and views_int < min_views)
                    or (min_sounds > 0 and sound_count < min_sounds)
                )

                if logger:
                    logger.event(
                        "board_parsed",
                        board=board_name,
                        has_downloads=bool(has_downloads),
                        sound_count=sound_count,
                        views_raw=views,
                        views_int=views_int,
                        download_button_count=len(download_ids_deduped),
                    )

                # Approximate last-updated date (best-effort)
                approx_updated = None
                approx_source = None
                track_headers_ok = None
                track_headers_total = None
                # Date inference hits per-track download URLs, so skip it for play-only
                # boards and for boards that already fail the views/sounds filters.
                need_date_scan = include_dates or recent_threshold is not None or sort_by == "recent"
                if need_date_scan and has_downloads and not fails_basic_filters:
                    # Heuristic:
                    # - Use the max Last-Modified across track download URLs
                    track_date = None

                    candidate_ids = []
                    if download_ids_deduped:
                        candidate_ids = download_ids_deduped
                    elif sound_matches:
                        # Fallback to IDs from data-src if download IDs aren't found
                        candidate_ids = [sid for sid, _ in sound_matches if str(sid).isdigit()]

                    if date_probes_blocked:
                        candidate_ids = []

                    if candidate_ids:
                        # date_sample_size=0 => scan as many as allowed. Otherwise, scan only the last N as displayed.
                        if date_sample_size and int(date_sample_size) > 0:
                            candidate_ids = candidate_ids[-int(date_sample_size):]
                        # Never more than the hard cap, whatever was asked for.
                        candidate_ids = candidate_ids[-MAX_DATE_PROBES_PER_BOARD:]

                        if logger:
                            logger.event(
                                "board_date_scan_start",
                                board=board_name,
                                candidate_track_count=len(candidate_ids),
                                sampled=bool(date_sample_size and int(date_sample_size) > 0),
                            )

                        track_headers_total = len(candidate_ids)
                        track_headers_ok = 0

                        max_dt = None
                        for sid in candidate_ids:
                            track_url = f"{BASE_URL}/track/download/{sid}"
                            dt, diag = fetch_last_modified_cached(track_url, with_diag=True)
                            if logger:
                                logger.event(
                                    "track_last_modified",
                                    board=board_name,
                                    track_id=sid,
                                    url=track_url,
                                    last_modified_utc=_format_datetime_utc(dt) if dt else None,
                                    diagnostic=diag,
                                    ok=bool(dt),
                                )
                            if dt:
                                track_headers_ok += 1
                            if dt and (max_dt is None or dt > max_dt):
                                max_dt = dt
                            m_stop = re.search(r'http_(\d{3})', diag or '')
                            if m_stop and _site_says_stop(m_stop.group(1)):
                                # The site refused a header check: make no more
                                # of them for the rest of this search.
                                date_probes_blocked = True
                                break
                            if len(candidate_ids) > 1:
                                time.sleep(HEADER_REQUEST_DELAY)

                        track_date = max_dt

                    if track_date:
                        approx_updated = track_date
                        approx_source = "track"

                    if logger:
                        logger.event(
                            "board_date_scan_result",
                            board=board_name,
                            board_last_modified_utc=_format_datetime_utc(approx_updated) if approx_updated else None,
                            track_headers_ok=(track_headers_ok or 0),
                            track_headers_total=track_headers_total,
                        )


                # Apply filters (pure decision; bucket attribution stays here).
                # Date filter is only evaluated when the basic filters pass, which
                # keeps skip reasons accurate — see _evaluate_filters.
                meets_filters, filter_failures = _evaluate_filters(
                    views_int, sound_count, approx_updated,
                    min_views, min_sounds, recent_threshold, recent_days,
                )
                filter_reasons = [reason for _, reason in filter_failures]

                if logger:
                    logger.event(
                        "board_filter_result",
                        board=board_name,
                        has_downloads=bool(has_downloads),
                        meets_filters=bool(meets_filters),
                        filter_reasons=filter_reasons,
                        approx_updated_utc=_format_datetime_utc(approx_updated) if approx_updated else None,
                    )

                # Only add to results if it meets filters
                if meets_filters:
                    new_board = BoardResult(
                        board_name=board_name,
                        has_downloads=has_downloads,
                        sounds_info=sounds_info,
                        total_count=sound_count,
                        board_desc=board_desc,
                        category=category,
                        views=views,
                        tags=tags,
                        views_int=views_int,
                        approx_updated=approx_updated,
                        approx_source=approx_source,
                        title=board_title,
                        image=board_image,
                    )
                    results.append(new_board)

                    # Stream each downloadable board to the web client as it's found
                    # (full dict) so pads render progressively instead of appearing
                    # all at once when the whole scan finishes.
                    if logger and has_downloads:
                        logger.event("board_result", board=board_result_to_dict(new_board))

                    # Count downloadable boards that meet filters
                    if has_downloads and meets_filters:
                        downloadable_count += 1

                # Add delay between board checks to be respectful to server
                time.sleep(REQUEST_DELAY)

            except Exception as e:
                if logger:
                    logger.event("board_analyze_error", board=board_name, error=str(e))
                # Don't increment downloadable_count on error
                if isinstance(e, HTTPError) and _site_says_stop(e.code):
                    # Forbidden / rate-limited / server error: stop here rather
                    # than keep requesting the remaining boards.
                    site_stop_code = e.code
                    keep_searching = False
                    break
                continue

        # Move to next page if we haven't found enough boards yet
        if downloadable_count >= target_downloadable or not keep_searching:
            break

        page += 1
        time.sleep(REQUEST_DELAY)  # Delay between pages


    # Filter results to only show downloadable boards (already done in meets_filters logic)
    results = [r for r in results if r.has_downloads]

    if site_stop_code is not None:
        stop_msg = (f"soundboard.com answered HTTP {site_stop_code}, so the search stopped early "
                    f"to avoid sending more requests. Wait a while before trying again.")
        if logger:
            logger.event("search_partial", message=stop_msg)
    elif boards_cap_hit:
        cap_msg = (f"Checked {boards_analyzed_total} boards, the limit for one search. "
                   f"Results may be incomplete; loosen the filters or narrow the search.")
        if logger:
            logger.event("search_partial", message=cap_msg)

    # Tell the caller (web UI) we stopped early on the time budget, so it can note
    # that results may be incomplete rather than looking like a silent truncation.
    if time_budget_hit and logger:
        logger.event("search_partial",
                      message=f"Reached the {int(time_budget)}s limit while checking dates "
                              f"(recent filtering probes each board one by one, so it's slow). "
                              f"Showing what was found — results may be incomplete.",
                      scanned=boards_analyzed_total)

    # Sort results
    if sort_by == "recent":
        min_date = datetime.min.replace(tzinfo=timezone.utc)
        results.sort(key=lambda x: x.approx_updated or min_date, reverse=True)
    else:
        # Sort by views (highest first)
        results.sort(key=lambda x: x.views_int, reverse=True)

    return results


class DownloadCoordinator:
    """Tracks sounds being fetched through the app's download windows.

    soundboard.com puts a human check in front of every download, which this
    tool does not bypass: the desktop shell opens the sound's download page in a
    real browser window, the person passes the check there, and the window's
    download hook asks this coordinator where to save the file. One job per
    sound; the web UI polls ``poll(token)`` until it reaches a final status.

    Statuses: waiting (window open) -> saving -> saved | exists | cancelled |
    failed | expired. Pure bookkeeping: the window is opened by the
    ``open_window(token, url)`` callable the shell supplies (None when running
    headless, where downloads aren't available) and closed through
    ``close_window(token)`` as soon as a job ends without the person doing it.
    """

    AUDIO_EXTS = ('.mp3', '.wav', '.ogg', '.m4a', '.aac', '.flac')
    FINAL = ('saved', 'exists', 'cancelled', 'failed', 'expired')
    KEEP_FINISHED = 50   # finished jobs kept for late polls; older ones are dropped

    def __init__(self, open_window=None, close_window=None, timeout=900):
        self._open_window = open_window
        self._close_window = close_window
        self._timeout = timeout
        self._lock = threading.Lock()
        self._jobs = {}
        self._counter = 0

    @property
    def available(self):
        return self._open_window is not None

    def _close(self, token):
        if self._close_window is not None:
            try:
                self._close_window(token)
            except Exception:
                pass

    def request(self, url, dest_dir, base_name, sound_id=""):
        """Start one sound: open its download window and return a poll token."""
        if self._open_window is None:
            raise RuntimeError("Downloads need the desktop app window; this server has none.")
        with self._lock:
            finished = [t for t, j in self._jobs.items() if j["status"] in self.FINAL]
            for t in finished[:-self.KEEP_FINISHED]:
                del self._jobs[t]
            self._counter += 1
            token = "%d-%d" % (self._counter, int(time.time() * 1000))
            self._jobs[token] = {
                "status": "waiting", "url": url, "dest_dir": dest_dir, "base_name": base_name,
                "sound_id": str(sound_id), "deadline": time.monotonic() + self._timeout,
            }
        try:
            self._open_window(token, url)
        except Exception as e:
            self.failed(token, str(e))
            raise
        return token

    def token_for_url(self, url):
        """Token of the live job that asked for ``url``, if any (the window's hook uses this)."""
        with self._lock:
            live = [t for t, j in self._jobs.items()
                    if j["url"] == url and j["status"] in ("waiting", "saving")]
            return live[-1] if live else None

    def poll(self, token):
        """Current status for ``token`` as {status, name?, kb?, error?}."""
        expired = False
        with self._lock:
            job = self._jobs.get(token)
            if job is None:
                return {"status": "unknown"}
            if job["status"] not in self.FINAL and time.monotonic() > job["deadline"]:
                job["status"] = "expired"
                expired = True
            result = {k: job[k] for k in ("status", "name", "kb", "error") if k in job}
        if expired:
            self._close(token)
        return result

    def destination_for(self, token, suggested_filename):
        """Where to save the file the site is sending, or None to skip it.

        Called by the shell's download hook. The file keeps the extension the
        site suggests (audio only, else .mp3) but takes the clean name chosen
        when the sound was requested. If another file already has that name it
        is never overwritten: this one is saved as "<name> (<sound id>)" and
        ``finished`` drops it again if it turns out to be the same audio.
        """
        close = False
        with self._lock:
            job = self._jobs.get(token)
            if job is None or job["status"] != "waiting":
                return None          # unknown, finished, or already receiving a file
            ext = os.path.splitext(os.path.basename(str(suggested_filename or "")))[1].lower()
            if ext not in self.AUDIO_EXTS:
                ext = ".mp3"
            name = job["base_name"] + ext
            path = os.path.join(job["dest_dir"], name)
            try:
                os.makedirs(job["dest_dir"], exist_ok=True)
                if os.path.exists(path):
                    job["original"] = path
                    name = "%s (%s)%s" % (job["base_name"], job["sound_id"] or "2", ext)
                    path = os.path.join(job["dest_dir"], name)
                if os.path.exists(path):
                    job.update(status="exists", name=name)
                    close = True
                    path = None
                else:
                    job.update(status="saving", name=name, path=path)
            except OSError as e:
                job.update(status="failed", error=str(e))
                close = True
                path = None
        if close:
            self._close(token)
        return path

    def finished(self, token):
        close = False
        with self._lock:
            job = self._jobs.get(token)
            if job is None or job.get("status") != "saving":
                return
            try:
                original = job.get("original")
                if original and filecmp.cmp(original, job["path"], shallow=False):
                    os.remove(job["path"])       # same audio as the file already saved
                    job.update(status="exists", name=os.path.basename(original))
                else:
                    job["kb"] = round(os.path.getsize(job["path"]) / 1024, 1)
                    job["status"] = "saved"
            except OSError as e:
                job.update(status="failed", error=str(e))
            close = True
        if close:
            self._close(token)

    def failed(self, token, message):
        with self._lock:
            job = self._jobs.get(token)
            if job is None or job["status"] in self.FINAL:
                return
            path = job.get("path")
            job.update(status="failed", error=str(message))
        if path:
            try:
                os.remove(path)   # never leave a partial file behind
            except OSError:
                pass
        self._close(token)

    def window_closed(self, token):
        """The person closed the download window; cancel unless a file is arriving."""
        with self._lock:
            job = self._jobs.get(token)
            if job is not None and job["status"] == "waiting":
                job["status"] = "cancelled"


def _ensure_ca_bundle():
    """Best-effort CA-bundle setup so HTTPS works on every entry point.

    python.org macOS Python often ships without a usable system trust store, so
    requests to soundboard.com fail with CERTIFICATE_VERIFY_FAILED. If nothing is
    configured and a real system bundle is absent, fall back to certifi when it is
    importable. Optional — no hard dependency; degrades to stdlib defaults when
    certifi is missing (preserving the zero-third-party-dependency baseline).
    """
    if os.environ.get("SSL_CERT_FILE"):
        return
    try:
        import ssl
        cafile = ssl.get_default_verify_paths().openssl_cafile
        if cafile and os.path.isfile(cafile):
            return  # a real system bundle exists; leave defaults alone
    except Exception:
        pass
    try:
        import certifi
        os.environ["SSL_CERT_FILE"] = certifi.where()
    except Exception:
        pass  # no certifi → unchanged behavior


def run_server(host, port, download_root, max_jobs=2, coordinator=None, on_ready=None):
    """Serve the web UI and its JSON/SSE API on ``host:port`` until shut down.

    Search and board lookups run here. Sound downloads go through ``coordinator``
    (a DownloadCoordinator), which the desktop shell wires to its download
    windows; without one the server is headless and cannot download. Saved files
    land under ``download_root`` (or the current directory), one folder per board.
    ``on_ready(httpd)`` is called once the socket is bound, so a caller that
    asked for port 0 can learn the real port and later call ``httpd.shutdown()``.
    """
    import json as _json
    import queue as _queue
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
    from urllib.parse import urlparse as _urlparse, parse_qs, unquote as _unquote

    web_root = os.path.realpath(os.path.join(os.path.dirname(os.path.abspath(__file__)), "web"))
    server_root = os.path.realpath(download_root) if download_root else os.path.realpath(os.getcwd())
    job_sem = threading.BoundedSemaphore(max_jobs)
    if coordinator is None:
        coordinator = DownloadCoordinator()   # headless: no download windows

    # Short-lived page cache: repeating a search, or loading a board's full
    # sound list right after a search listed it, reuses the page instead of
    # asking soundboard.com again.
    PAGE_CACHE_TTL = 600   # seconds
    PAGE_CACHE_MAX = 60    # pages
    page_cache = {}
    page_cache_lock = threading.Lock()

    def cached_fetch(url):
        now = time.monotonic()
        with page_cache_lock:
            hit = page_cache.get(url)
            if hit and now - hit[0] < PAGE_CACHE_TTL:
                return hit[1]
        text = _http_get(url)   # errors propagate and are never cached
        with page_cache_lock:
            for stale in [u for u, (at, _) in page_cache.items() if now - at >= PAGE_CACHE_TTL]:
                del page_cache[stale]
            if len(page_cache) >= PAGE_CACHE_MAX:
                for old in sorted(page_cache, key=lambda u: page_cache[u][0])[:PAGE_CACHE_MAX // 4]:
                    del page_cache[old]
            page_cache[url] = (now, text)
        return text

    CONTENT_TYPES = {
        ".html": "text/html; charset=utf-8",
        ".css": "text/css; charset=utf-8",
        ".js": "application/javascript; charset=utf-8",
        ".svg": "image/svg+xml",
        ".json": "application/json; charset=utf-8",
        ".ico": "image/x-icon",
        ".woff2": "font/woff2",
    }

    def valid_board(value):
        # CLI parity: board_name is a display-ish identifier (may contain spaces/%).
        # Reject only what could escape a path; _quote_path_segment encodes the rest.
        if not value:
            return False
        if "/" in value or "\\" in value:
            return False
        if any(ord(c) < 32 for c in value):
            return False
        return True

    def is_loopback(host):
        host = (host or "").strip()
        if host.startswith("::ffff:"):  # IPv4-mapped IPv6 (dual-stack)
            host = host[7:]
        return host in ("::1", "localhost") or host.startswith("127.")

    class Handler(BaseHTTPRequestHandler):
        server_version = "soundboard-snag"
        protocol_version = "HTTP/1.1"

        def log_message(self, fmt, *args):
            pass   # no request log

        # ---- response helpers ----
        def _send_json(self, obj, status=200):
            body = _json.dumps(obj, ensure_ascii=False).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _send_bytes(self, body, content_type, status=200):
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")  # never cache dev assets (always refetch)
            self.end_headers()
            self.wfile.write(body)

        def _start_sse(self):
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-cache")
            self.send_header("Connection", "close")
            self.end_headers()

        def _sse_send(self, event, data):
            try:
                payload = "event: %s\ndata: %s\n\n" % (event, _json.dumps(data, ensure_ascii=False))
                self.wfile.write(payload.encode("utf-8"))
                self.wfile.flush()
                return True
            except (BrokenPipeError, ConnectionResetError, OSError):
                return False

        def _sse_comment(self):
            try:
                self.wfile.write(b": keepalive\n\n")
                self.wfile.flush()
                return True
            except (BrokenPipeError, ConnectionResetError, OSError):
                return False

        def _pump(self, q, sentinel, cancel=None):
            """Drain the worker queue to the SSE stream until the sentinel.

            Returns when the worker signals completion or the client disconnects.
            Sets ``cancel`` (if given) on disconnect so the worker can stop early.
            """
            while True:
                try:
                    item = q.get(timeout=15)
                except _queue.Empty:
                    if not self._sse_comment():
                        if cancel is not None:
                            cancel.set()
                        return
                    continue
                if item is sentinel:
                    return
                event_type, data = item
                if not self._sse_send(event_type, data):
                    if cancel is not None:
                        cancel.set()
                    return

        # ---- routing ----
        def do_GET(self):
            parsed = _urlparse(self.path)
            path = parsed.path
            if path in ("/", ""):
                return self._serve_static("/index.html")
            if path == "/api/search":
                return self._api_search(parse_qs(parsed.query))
            if path.startswith("/api/board/"):
                return self._api_board(path[len("/api/board/"):])
            if path.startswith("/api/fetch-sound/"):
                return self._send_json(coordinator.poll(_unquote(path[len("/api/fetch-sound/"):])))
            return self._serve_static(path)

        def do_POST(self):
            path = _urlparse(self.path).path
            if path == "/api/fetch-sound":
                return self._api_fetch_sound()
            self._send_json({"error": "not found"}, status=404)

        # ---- static files (path-traversal hardened) ----
        def _serve_static(self, urlpath):
            rel = _unquote(urlpath).lstrip("/")
            candidate = os.path.realpath(os.path.join(web_root, rel))
            try:
                inside = os.path.commonpath([web_root, candidate]) == web_root
            except ValueError:
                inside = False
            if not inside or not os.path.isfile(candidate):
                self._send_json({"error": "not found"}, status=404)
                return
            ext = os.path.splitext(candidate)[1].lower()
            try:
                with open(candidate, "rb") as f:
                    body = f.read()
            except OSError:
                self._send_json({"error": "not found"}, status=404)
                return
            self._send_bytes(body, CONTENT_TYPES.get(ext, "application/octet-stream"))

        # ---- shared API helpers ----
        def _origin_ok(self):
            # CSRF guard for state-changing POSTs: a browser attaches Origin on
            # cross-site requests; reject any that isn't this local server. No
            # Origin = non-browser (curl/automation) → allow.
            origin = self.headers.get("Origin")
            if not origin:
                return True
            try:
                host = _urlparse(origin).hostname
            except Exception:
                return False
            return is_loopback(host or "")

        def _run_sse_job(self, build_worker):
            """Shared SSE lifecycle for the streaming endpoints.

            Acquire a job slot, stream a worker's events until its sentinel, and
            release the slot only when the worker TRULY finishes — so a client
            disconnect can't free the slot while the scrape keeps running (which
            would defeat the concurrency cap and amplify requests). build_worker
            receives (emit, cancel) and returns a no-arg worker callable; the
            handler sets `cancel` on disconnect so the worker stops early.
            """
            if not job_sem.acquire(blocking=False):
                self._start_sse()
                self._sse_send("busy", {"message": "server busy, try again"})
                return
            self._start_sse()
            q = _queue.Queue()  # unbounded
            sentinel = object()
            cancel = threading.Event()

            def emit(event_type, data=None, **fields):
                q.put((event_type, data if data is not None else fields))

            worker_fn = build_worker(emit, cancel)

            def runner():
                try:
                    worker_fn()
                finally:
                    q.put(sentinel)
                    job_sem.release()

            threading.Thread(target=runner, daemon=True).start()
            self._pump(q, sentinel, cancel=cancel)

        # ---- GET /api/search (SSE) ----
        def _api_search(self, qs):
            def first(name, default=None):
                v = qs.get(name)
                return v[0] if v else default

            try:
                query = (first("q", "") or "").strip()
                if not query:
                    self._send_json({"error": "missing q"}, status=400)
                    return
                # Clamp to sane bounds so one request can't drive an unbounded scrape.
                max_results = max(1, min(int(first("max", "20")), 100))
                min_views = max(0, int(first("min_views", "0")))
                min_sounds = max(0, int(first("min_sounds", "0")))
                sort_by = first("sort", "views")
                if sort_by not in ("views", "recent"):
                    self._send_json({"error": "bad sort"}, status=400)
                    return
                include_dates = (first("include_dates", "0") in ("1", "true", "yes"))
                rd = first("recent_days")
                recent_days = max(1, min(int(rd), 3650)) if rd not in (None, "") else None
                date_sample_size = max(0, min(int(first("date_sample_size", "0")), 50))
            except ValueError:
                self._send_json({"error": "bad params"}, status=400)
                return

            if recent_days is not None or sort_by == "recent":
                include_dates = True

            def build(emit, cancel):
                class _Sink:
                    def event(self, event_type, **fields):
                        # The page only uses these; the rest is engine diagnostics.
                        if event_type in ("board_analyze_start", "board_result", "search_partial"):
                            emit(event_type, **fields)

                def worker():
                    try:
                        # Bound date inference: a small per-board sample keeps a
                        # recent/dated search from probing every track of every
                        # board, and time_budget caps total wall-clock so a popular
                        # term returns partial results instead of hanging.
                        dss = date_sample_size if date_sample_size > 0 else 4
                        results = search_boards(
                            query, max_results, min_views, min_sounds,
                            include_dates=include_dates, recent_days=recent_days,
                            sort_by=sort_by, date_sample_size=dss,
                            logger=_Sink(), cancel_event=cancel, time_budget=30,
                            fetch=cached_fetch,
                        )
                        emit("results", [board_result_to_dict(b) for b in results])
                    except Exception as e:
                        emit("error", {"message": str(e)})
                return worker

            self._run_sse_job(build)

        # ---- POST /api/fetch-sound (open a download window for one sound) ----
        def _api_fetch_sound(self):
            """Start one sound: the shell opens its download page in a window.

            Returns a token to poll at GET /api/fetch-sound/<token>. The request
            to soundboard.com is made by the window, i.e. like a browser visit
            the person completes themself, never by this server.
            """
            if not self._origin_ok() or not is_loopback(self.client_address[0]):  # CSRF guard
                self._send_json({"error": "forbidden"}, status=403)
                return
            try:
                length = int(self.headers.get("Content-Length", 0) or 0)
            except ValueError:
                length = 0
            if length > 64 * 1024:
                self._send_json({"error": "request too large"}, status=413)
                return
            try:
                body = _json.loads(self.rfile.read(length).decode("utf-8")) if length else {}
            except Exception:
                self._send_json({"error": "bad json"}, status=400)
                return
            board = (body.get("board") or "").strip()
            sound_id = str(body.get("sound_id") or "").strip()
            title = (body.get("title") or "").strip()
            if not valid_board(board) or not sound_id.isdigit():
                self._send_json({"error": "bad params"}, status=400)
                return
            if not coordinator.available:
                self._send_json({"status": "error",
                                 "error": "downloads need the desktop app window"})
                return
            try:
                board_url = "%s/sb/%s" % (BASE_URL, _quote_path_segment(board))
                tool = SoundboardSnag(board_url, download_root=server_root)
                dest_dir = os.path.join(tool.download_root, tool._board_output_dirname())
                clean = tool._sanitize_filename("%s.mp3" % (title or "audio_" + sound_id), sound_id, title)
                token = coordinator.request("%s/track/download/%s" % (BASE_URL, sound_id),
                                            dest_dir, os.path.splitext(clean)[0], sound_id)
            except Exception as e:
                self._send_json({"status": "error", "error": str(e)})
                return
            self._send_json({"status": "waiting", "token": token})

        # ---- GET /api/board/<board> (JSON) ----
        def _api_board(self, raw_id):
            board = _unquote(raw_id).strip()
            if not valid_board(board):
                self._send_json({"error": "bad board"}, status=400)
                return
            if not job_sem.acquire(blocking=False):
                self._send_json({"error": "busy"}, status=503)
                return
            try:
                board_url = "%s/sb/%s" % (BASE_URL, _quote_path_segment(board))
                tool = SoundboardSnag(board_url, download_root=server_root, fetcher=cached_fetch)
                html_content = tool._fetch_page()
                has_downloads, _count = tool._check_downloads_enabled(html_content)
                items = tool._parse_sound_items(html_content)
                self._send_json({
                    "board": board,
                    "has_downloads": bool(has_downloads),
                    "total_count": len(items),
                    "sounds": [{"id": sid, "title": title} for sid, title in items],
                    "error": None,
                })
            except Exception as e:
                self._send_json({
                    "board": board, "has_downloads": False, "total_count": 0,
                    "sounds": [], "error": str(e),
                })
            finally:
                job_sem.release()

    httpd = ThreadingHTTPServer((host, port), Handler)
    if on_ready is not None:
        on_ready(httpd)
    try:
        httpd.serve_forever()
    finally:
        httpd.server_close()


# ---------------------------------------------------------------------------
# Desktop app (macOS window around the local web UI)
# ---------------------------------------------------------------------------

APP_NAME = "Soundboard Snag"
DEFAULT_DOWNLOAD_ROOT = "~/Downloads/Soundboard Snag"
APP_SUPPORT_DIR = "~/Library/Application Support/Soundboard Snag"


def _install_download_hook(cocoa, AppKit, Foundation, WebKit, objc, coordinator):
    """Make audio responses in the app's download windows real file downloads.

    pywebview's own macOS delegate would play the audio inline instead of saving
    it. We subclass its delegate: when a response's URL belongs to a live job of
    ``coordinator`` and is audio (or an attachment), it becomes a WKDownload whose
    destination ``coordinator`` chooses; everything else falls through to
    pywebview. The coordinator also closes the window once the job ends. Must run
    before the first window is created.
    """
    hook = "webView_decidePolicyForNavigationResponse_decisionHandler_"
    if not hasattr(cocoa.BrowserView.BrowserDelegate, hook):
        raise SystemExit("This version of pywebview is not supported (its macOS delegate "
                         "changed); install the one in requirements.txt.")
    live = {}   # WKDownload holds its delegate weakly: keep ours alive until done

    class SnagDownloadDelegate(AppKit.NSObject):
        def download_decideDestinationUsingResponse_suggestedFilename_completionHandler_(
                self, download, response, suggested, handler):
            path = coordinator.destination_for(self.token, str(suggested or ""))
            handler(Foundation.NSURL.fileURLWithPath_(path) if path else None)

        def downloadDidFinish_(self, download):
            live.pop(self.token, None)
            coordinator.finished(self.token)

        def download_didFailWithError_resumeData_(self, download, error, resume_data):
            live.pop(self.token, None)
            coordinator.failed(self.token, str(error))

    class SnagBrowserDelegate(cocoa.BrowserView.BrowserDelegate):
        def webView_decidePolicyForNavigationResponse_decisionHandler_(
                self, webview, navigationResponse, decisionHandler):
            response = navigationResponse.response()
            if coordinator.token_for_url(str(response.URL().absoluteString())) is not None:
                headers = dict(response.allHeaderFields() or {})
                disposition = str(headers.get("Content-Disposition") or "").lower()
                if str(response.MIMEType() or "").startswith("audio/") or "attachment" in disposition:
                    decisionHandler(getattr(WebKit, "WKNavigationResponsePolicyDownload", 2))
                    return
            objc.super(SnagBrowserDelegate, self).webView_decidePolicyForNavigationResponse_decisionHandler_(
                webview, navigationResponse, decisionHandler)

        def webView_navigationResponse_didBecomeDownload_(self, webview, navigationResponse, download):
            token = coordinator.token_for_url(str(navigationResponse.response().URL().absoluteString()))
            if token is None:
                return
            delegate = SnagDownloadDelegate.alloc().init()
            delegate.token = token
            live[token] = delegate
            download.setDelegate_(delegate)

    cocoa.BrowserView.BrowserDelegate = SnagBrowserDelegate


def run_desktop(download_root):
    """Open the app: the web UI in a native window, plus download windows on demand."""
    try:
        import AppKit
        import Foundation
        import objc
        import WebKit
        import webview
        from webview.platforms import cocoa
    except ImportError:
        raise SystemExit(
            "Soundboard Snag needs macOS and its window libraries.\n"
            "Install them with:  python3 -m pip install -r requirements.txt")

    support = os.path.expanduser(APP_SUPPORT_DIR)
    os.makedirs(support, exist_ok=True)
    windows = {}   # token -> download window

    def close_window(token):
        win = windows.pop(token, None)
        if win is not None:
            try:
                win.destroy()
            except Exception:
                pass

    def open_window(token, url):
        win = webview.create_window("Download — verify if asked", url, width=520, height=700)
        windows[token] = win
        win.events.closed += lambda: (windows.pop(token, None), coordinator.window_closed(token))

    coordinator = DownloadCoordinator(open_window, close_window)
    _install_download_hook(cocoa, AppKit, Foundation, WebKit, objc, coordinator)
    webview.settings["ALLOW_DOWNLOADS"] = True

    ready = threading.Event()
    holder = []

    def on_ready(httpd):
        holder.append(httpd)
        ready.set()

    threading.Thread(
        target=run_server, args=("127.0.0.1", 0, os.path.expanduser(download_root)),
        kwargs={"coordinator": coordinator, "on_ready": on_ready}, daemon=True).start()
    if not ready.wait(10):
        raise SystemExit("The local server did not start.")
    httpd = holder[0]

    main_window = webview.create_window(
        APP_NAME, "http://127.0.0.1:%d/" % httpd.server_address[1],
        width=1100, height=820, min_size=(720, 560))

    def on_main_closed():
        for token in list(windows):
            close_window(token)
    main_window.events.closed += on_main_closed

    try:
        # Not private: the site's "verified" state then lasts across downloads.
        webview.start(private_mode=False, storage_path=os.path.join(support, "webview"))
    finally:
        httpd.shutdown()


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Soundboard Snag: find soundboard.com boards and save their sounds.")
    parser.add_argument("--serve", action="store_true",
                        help="Developer mode: serve the web UI headlessly (no download windows) "
                             "so it can be opened in any browser.")
    parser.add_argument("--host", default="127.0.0.1", help="Host for --serve (default: 127.0.0.1).")
    parser.add_argument("--port", type=int, default=8765, help="Port for --serve (default: 8765).")
    parser.add_argument("-d", "--download-root", default=DEFAULT_DOWNLOAD_ROOT,
                        help="Folder sounds are saved under, one subfolder per board "
                             "(default: %s)." % DEFAULT_DOWNLOAD_ROOT)
    args = parser.parse_args(argv)

    _ensure_ca_bundle()   # python.org Python ships without a system trust store
    root = os.path.expanduser(args.download_root)
    if args.serve:
        print("Serving http://%s:%d (headless: searching works, downloads need the app window)"
              % (args.host, args.port))
        run_server(args.host, args.port, root)
    else:
        run_desktop(root)


if __name__ == "__main__":
    main()

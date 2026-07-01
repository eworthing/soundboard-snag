#!/usr/bin/env python3
"""
Soundboard Snag
Snags audio files from soundboard.com with clean, normalized filenames.

Usage (Recommended - Search then Download by Name):
    # Step 1: Search for boards (automatically filters low-quality boards)
    python3 soundboard-snag.py --search "star wars"

    # Search with custom quality filters
    python3 soundboard-snag.py --search "star wars" --min-views 100 --min-sounds 10

    # Search without filters (show all boards)
    python3 soundboard-snag.py --search "star wars" --min-views 0 --min-sounds 0

    # Show approximate updated dates (best-effort)
    python3 soundboard-snag.py --search "star wars" --include-dates

    # Find recently updated boards (approximate)
    python3 soundboard-snag.py --search "star wars" --recent-days 7 --sort recent

    # Step 2: Download by board name from search results
    python3 soundboard-snag.py --board starwars

    # Download to a custom location
    python3 soundboard-snag.py --board starwars --download-root ~/Music/Soundboards

Alternative Usage (Direct URL):
    python3 soundboard-snag.py --url https://www.soundboard.com/sb/starwars

    # Download URL to custom location
    python3 soundboard-snag.py --url https://www.soundboard.com/sb/starwars -d ~/Downloads/Sounds

Search and Download:
    # Search and download all results to a custom location
    python3 soundboard-snag.py --search-and-download "nature" --max 5 -d ~/Sounds

Interactive Mode:
    python3 soundboard-snag.py

Requirements:
    Python 3.6+ (uses only standard library - no external dependencies)

Cross-Platform:
    Works on Windows, macOS, and Linux

Search Quality Filters:
    By default, search results filter out low-quality boards (min 10 views, min 3 sounds).
    This eliminates empty/test boards and improves result quality.
    Use --min-views 0 --min-sounds 0 to disable filtering and see all results.

Limitations:
    Only works with boards that have download buttons enabled by the owner.
    Boards with play-only mode cannot be downloaded (audio is access-controlled).
    The script will detect and warn about restricted boards before attempting downloads.
    Approximate updated dates (when enabled) are inferred from HTTP Last-Modified headers
    for downloadable tracks. These are best-effort and not authoritative.
"""

import argparse
import html
import json
import math
import os
import re
import sys
import time
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from typing import List, NamedTuple, Optional, Tuple
from urllib.parse import urlparse, quote, unquote
from urllib.request import Request, urlopen
from urllib.error import HTTPError, URLError


# Module-level constants
BASE_URL = "https://www.soundboard.com"
USER_AGENT = 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36'
CHUNK_SIZE = 8192
REQUEST_DELAY = 0.5  # Delay between requests in seconds (be respectful to server)
HTTP_TIMEOUT = 10  # Network timeout for requests (seconds)
DOWNLOAD_TIMEOUT = 30  # Slightly higher timeout for actual file downloads
HEADER_REQUEST_DELAY = 0.05  # Small delay between header checks when scanning many tracks
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


def _format_updated_line(approx_updated, approx_source, stats):
    """Format the 'Updated: ...' detail line (pure; no color or indent).

    ``stats`` is ``None`` or an ``(ok, total)`` pair of track-header counts.
    Returns e.g. ``'Updated: 2025-01-02 (approx via track; track headers: 3/5)'``
    or ``'Updated: unknown (approx)'``. Callers add their own color/indent.
    """
    extra = ""
    if stats:
        ok, total = stats
        extra = f"; track headers: {ok}/{total}"
    if approx_updated:
        src = f" via {approx_source}" if approx_source else ""
        return f"Updated: {_format_date(approx_updated)} (approx{src}{extra})"
    return f"Updated: unknown (approx{extra})"


def _format_skipped_breakdown(skipped_buckets):
    """Join the non-zero skip-bucket counts into the 'breakdown' string.

    Returns '' when every bucket is zero. Pure; order is fixed for stable output.
    """
    labels = (
        ("views", "views"),
        ("sounds", "sounds"),
        ("updated_unknown", "updated unknown"),
        ("updated_too_old", "updated too old"),
    )
    parts = [f"{label}: {skipped_buckets[key]}" for key, label in labels if skipped_buckets.get(key)]
    return ", ".join(parts)


def _render_board_lines(board, stats, include_dates):
    """Build the detail lines for one board in the results listing (pure).

    ``board`` is a BoardResult; ``stats`` is None or an ``(ok, total)`` pair of
    track-header counts. Returns a list of fully-formatted (colored) strings;
    the caller prints them. No network, no I/O.
    """
    if board.has_downloads:
        status = f"{Colors.GREEN}✓ DOWNLOADABLE{Colors.RESET}"
    else:
        status = f"{Colors.RED}✗ PLAY-ONLY{Colors.RESET}"
    lines = [
        f"{Colors.BOLD}Board:{Colors.RESET} {Colors.CYAN}{board.board_name}{Colors.RESET} - {status} {Colors.GRAY}({board.total_count} sounds total){Colors.RESET}",
        f"{Colors.GRAY}URL: {BASE_URL}/sb/{_quote_path_segment(board.board_name)}{Colors.RESET}",
    ]
    if board.board_desc:
        lines.append(f"{Colors.GRAY}Description: {board.board_desc}{Colors.RESET}")
    if board.category:
        lines.append(f"{Colors.GRAY}Category: {board.category}{Colors.RESET}")
    if board.views:
        lines.append(f"{Colors.GRAY}Views: {board.views}{Colors.RESET}")
    if include_dates:
        lines.append(f"{Colors.GRAY}{_format_updated_line(board.approx_updated, board.approx_source, stats)}{Colors.RESET}")
    if board.tags:
        lines.append(f"{Colors.GRAY}Tags: {', '.join(board.tags)}{Colors.RESET}")
    if board.sounds_info:
        lines.append(f"\n{Colors.BOLD}Sample files (showing {len(board.sounds_info)} of {board.total_count}):{Colors.RESET}")
        for idx, (sound_id, title) in enumerate(board.sounds_info, 1):
            lines.append(f"  {Colors.YELLOW}{idx:2}.{Colors.RESET} {title}")
    return lines


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
    slugs = re.findall(
        r"href\s*=\s*['\"]?/sb/([^'\"\s>#?]+)",
        html_content,
        flags=re.IGNORECASE,
    )
    return [html.unescape(s).strip() for s in slugs if s and s.strip()]

# ANSI color codes (disabled when stdout is not a TTY)
class Colors:
    """ANSI color codes for terminal output (disabled when stdout is not a TTY)."""
    RESET = '\033[0m'
    BOLD = '\033[1m'

    # Foreground colors
    RED = '\033[91m'
    GREEN = '\033[92m'
    YELLOW = '\033[93m'
    BLUE = '\033[94m'
    CYAN = '\033[96m'
    GRAY = '\033[90m'


def _init_colors():
    """Disable ANSI colors when stdout is not a TTY (e.g., piped or redirected)."""
    if not hasattr(sys.stdout, 'isatty') or not sys.stdout.isatty():
        for attr in list(vars(Colors)):
            if not attr.startswith('_'):
                setattr(Colors, attr, '')


_init_colors()


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
    """Format a datetime for display."""
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


class JsonlLogger:
    """Simple JSONL logger (one JSON object per line)."""

    def __init__(self, file_path):
        self.file_path = os.path.abspath(os.path.expanduser(file_path))
        os.makedirs(os.path.dirname(self.file_path) or ".", exist_ok=True)
        self._fp = open(self.file_path, "a", encoding="utf-8")

    def event(self, event_type, **fields):
        record = {
            "ts": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
            "event": event_type,
        }
        record.update(fields)
        self._fp.write(json.dumps(record, ensure_ascii=False) + "\n")
        self._fp.flush()

    def close(self):
        try:
            self._fp.close()
        except Exception:
            pass

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        self.close()


class SoundboardSnag:
    """Snags and manages soundboard audio files."""

    def __init__(self, soundboard_url, download_root=None, fetcher=None,
                 event_cb=None, render=True, cancel_event=None):
        """Initialize snag tool with a soundboard URL.

        Args:
            soundboard_url: A string URL pointing to a soundboard.com page.
                Expected format: https://www.soundboard.com/sb/boardname
            download_root: Optional root directory for downloads. If None, uses CWD.
            fetcher: Optional callable(url) -> decoded page text. Defaults to
                _http_get (real network). Tests inject an in-memory fake to
                exercise snag()'s guard and abort logic offline. Covers the board
                *page* fetch only; per-track downloads go through ``urlopen``.
            event_cb: Optional callable(event_type, **fields) invoked alongside
                printing at each step (download_start, file_start, file_saved, …).
                The web layer passes a sink that pushes onto an SSE queue. Default
                ``None`` is a no-op, so CLI behavior is unchanged.
            render: When False, all human stdout output from snag() is suppressed
                (the web/programmatic path). Default True = current CLI output.
            cancel_event: Optional ``threading.Event``; when set mid-run the
                per-file loop stops between files and emits ``download_aborted``.
                Default ``None`` never cancels.

        Raises:
            ValueError: If the URL format is invalid or board name cannot
                be extracted.
        """
        self.url = urlparse(soundboard_url)
        self.board_slug, self.board_name = self._extract_board_slug_and_name()
        self.base_url = BASE_URL
        self.download_root = download_root if download_root else os.getcwd()
        self.fetcher = fetcher if fetcher is not None else _http_get
        self.event_cb = event_cb
        self.render = render
        self.cancel_event = cancel_event

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

    def _extract_filename_from_headers(self, headers):
        """Extract filename from Content-Disposition header."""
        content_disp = headers.get('content-disposition', '')

        # Try different quote styles
        for pattern in [r'filename="([^"]+)"', r"filename='([^']+)'", r'filename=([^\s;]+)']:
            match = re.search(pattern, content_disp)
            if match:
                return match.group(1)

        return None

    def _snag_sound(self, sound_id, page_title, output_dir):
        """Snag a single sound file."""
        download_url = f"{self.base_url}/track/download/{sound_id}"

        try:
            # Create request with User-Agent
            req = Request(download_url, headers={'User-Agent': USER_AGENT})

            with urlopen(req, timeout=DOWNLOAD_TIMEOUT) as response:
                status_code = response.getcode()

                if status_code != 200:
                    return False, f"HTTP {status_code}"

                # Prefer page title from HTML over header filename
                # (header filenames are often base64/hash codes)
                if page_title and page_title.strip():
                    raw_filename = f"{page_title.strip()}.mp3"
                else:
                    # Fallback to header filename
                    raw_filename = self._extract_filename_from_headers(dict(response.headers))
                    if not raw_filename:
                        raw_filename = f"audio_{sound_id}.mp3"

                # Clean and normalize filename
                final_filename = self._sanitize_filename(raw_filename, sound_id, page_title)
                filepath = os.path.join(output_dir, final_filename)

                # Skip if already exists
                if os.path.isfile(filepath):
                    return None, final_filename  # None indicates skip

                # Write file in chunks; remove partial file on failure
                try:
                    with open(filepath, 'wb') as f:
                        while True:
                            chunk = response.read(CHUNK_SIZE)
                            if not chunk:
                                break
                            f.write(chunk)
                except BaseException:
                    try:
                        os.remove(filepath)
                    except OSError:
                        pass
                    raise

                file_size_kb = os.path.getsize(filepath) / 1024
                return True, (final_filename, file_size_kb)

        except HTTPError as e:
            return False, f"HTTP {e.code}: {e.reason}"
        except URLError as e:
            return False, f"Network error: {e.reason}"
        except OSError as e:
            return False, str(e)

    def snag(self):
        """Main snagging process."""
        def rprint(*args, **kwargs):
            if self.render:
                print(*args, **kwargs)

        def emit(event_type, **fields):
            if self.event_cb is not None:
                self.event_cb(event_type, **fields)

        if self.board_slug and self.board_name and self.board_slug != self.board_name:
            rprint(f"{Colors.BOLD}{Colors.CYAN}Snagging from board: {self.board_name} ({self.board_slug}){Colors.RESET}")
        else:
            rprint(f"{Colors.BOLD}{Colors.CYAN}Snagging from board: {self.board_name}{Colors.RESET}")

        # Fetch and parse page
        html_content = self._fetch_page()

        # Check if downloads are enabled
        has_downloads, download_count = self._check_downloads_enabled(html_content)

        sound_items = self._parse_sound_items(html_content)

        if not sound_items:
            raise RuntimeError("No audio files found on this soundboard page")

        rprint(f"{Colors.GREEN}Located {len(sound_items)} audio files to snag!{Colors.RESET}")

        # Check if downloads are enabled - fail fast if not
        if not has_downloads:
            board_url = self._board_url()
            rprint(f"\n{Colors.RED}❌ ERROR: This board has downloads disabled!{Colors.RESET}")
            rprint(f"   Found {len(sound_items)} sounds but {Colors.YELLOW}no download buttons{Colors.RESET}.")
            rprint(f"   The board owner has restricted this board to play-only mode.")
            rprint(f"\n   Board URL: {Colors.CYAN}{board_url}{Colors.RESET}")
            rprint(f"   You can verify by visiting the board and checking for download links.")
            rprint(f"\n   This board cannot be downloaded. Please try a different board.")
            rprint(f"   Boards with download buttons will work (e.g., starwars, R2D2_R2_D2_sounds)")
            raise RuntimeError("Board has downloads disabled - cannot proceed")

        rprint(f"   {Colors.GRAY}({download_count} download buttons detected){Colors.RESET}")

        emit("download_start", board=self.board_name, total=len(sound_items))
        emit("board_parsed", count=len(sound_items))

        # Show download location
        output_dir = os.path.join(self.download_root, self._board_output_dirname())
        rprint(f"   {Colors.GRAY}Download location: {os.path.abspath(output_dir)}{Colors.RESET}\n")

        # Download each sound
        snagged_count = 0
        existing_count = 0
        failed_count = 0
        consecutive_failures = 0
        max_consecutive_failures = 2  # Exit if this many failures in a row
        early_exit = False

        for i, (sound_id, page_title) in enumerate(sound_items, 1):
            # Best-effort cooperative cancellation (web client disconnect).
            if self.cancel_event is not None and self.cancel_event.is_set():
                emit("download_aborted", reason="cancelled")
                early_exit = True
                break

            rprint(f"{Colors.GRAY}[{i}/{len(sound_items)}]{Colors.RESET} Snagging audio ID {Colors.CYAN}{sound_id}{Colors.RESET}...")
            emit("file_start", i=i, n=len(sound_items), sound_id=sound_id)

            # Create output directory only when needed (before first download attempt)
            if not os.path.exists(output_dir):
                os.makedirs(output_dir)
                rprint(f"  {Colors.BLUE}Created directory: {os.path.abspath(output_dir)}{Colors.RESET}")

            result, data = self._snag_sound(sound_id, page_title, output_dir)

            if result is True:
                final_filename, size_kb = data
                rprint(f"  {Colors.GREEN}✓ Snagged:{Colors.RESET} {final_filename} {Colors.GRAY}({size_kb:.1f} KB){Colors.RESET}")
                snagged_count += 1
                consecutive_failures = 0  # Reset on success
                emit("file_saved", i=i, n=len(sound_items), name=final_filename, kb=round(size_kb, 1))
            elif result is None:
                rprint(f"  {Colors.YELLOW}○ Skipped (exists):{Colors.RESET} {data}")
                existing_count += 1
                consecutive_failures = 0  # Reset on skip (file exists = not a failure)
                emit("file_skipped", i=i, n=len(sound_items), name=data)
            else:
                rprint(f"  {Colors.RED}✗ Failed:{Colors.RESET} {data}")
                failed_count += 1
                consecutive_failures += 1
                emit("file_failed", i=i, n=len(sound_items), error=data)

                # Check if we've hit the consecutive failure limit
                if consecutive_failures >= max_consecutive_failures:
                    remaining = len(sound_items) - i
                    rprint(f"\n{Colors.RED}❌ ERROR: {consecutive_failures} consecutive download failures detected!{Colors.RESET}")
                    rprint(f"   This board appears to have invalid or broken download links.")
                    rprint(f"   Attempted: {i}/{len(sound_items)} files")
                    rprint(f"   Skipping remaining {remaining} file(s) to avoid wasting time and server resources.")

                    # Clean up empty directory if no files were successfully downloaded or existed
                    if snagged_count == 0 and existing_count == 0:
                        if os.path.exists(output_dir) and os.path.isdir(output_dir):
                            try:
                                os.rmdir(output_dir)
                                rprint(f"   {Colors.GRAY}Removed empty directory: {os.path.abspath(output_dir)}{Colors.RESET}")
                            except OSError:
                                pass  # Directory not empty or other error, leave it

                    emit("download_aborted", reason="too many consecutive failures")
                    early_exit = True
                    break

            # Add delay between downloads to be respectful to server
            if i < len(sound_items):  # Don't delay after the last one
                time.sleep(REQUEST_DELAY)

        # Summary
        full_path = os.path.abspath(output_dir)
        rprint(f"\n{Colors.GREEN}{Colors.BOLD}✓ Snagging complete!{Colors.RESET} {Colors.CYAN}{snagged_count}{Colors.RESET} files saved to:")
        rprint(f"  {Colors.BOLD}{full_path}{Colors.RESET}")
        if existing_count > 0:
            rprint(f"  {Colors.YELLOW}({existing_count} files were already present){Colors.RESET}")
        if failed_count > 0:
            rprint(f"  {Colors.RED}⚠️  {failed_count} files failed to download{Colors.RESET}")
            if not has_downloads:
                rprint(f"  Note: This board has downloads disabled by the owner.")

        # Only a clean run is "complete"; an aborted run already emitted
        # download_aborted and must not also report success to the web client.
        if not early_exit:
            emit("download_complete", snagged=snagged_count, existing=existing_count, failed=failed_count)
        return not early_exit



def search_boards(
    query,
    max_results=20,
    debug=False,
    min_views=0,
    min_sounds=0,
    include_dates=False,
    recent_days=None,
    sort_by="views",
    date_sample_size=0,
    progress=False,
    verbose=False,
    logger=None,
    fetch=None,
    render=True,
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
        debug: If True, show all boards being analyzed including non-downloadable ones and filter reasons
        min_views: Minimum number of views required (default set by CLI, 0 = no filter)
        min_sounds: Minimum number of sounds required (default set by CLI, 0 = no filter)
        include_dates: If True, fetch approximate Last-Modified dates (extra requests)
        recent_days: If set, only include boards updated within the last N days (approx)
        sort_by: Sort results by "views" or "recent" (approx update date)
        progress: If True, show realtime progress updates while searching
        verbose: If True, print detailed per-step output (requests, parsing, filters, dates)
        logger: Optional JsonlLogger for capturing structured run details
        fetch: Optional callable(url) -> decoded page text. Defaults to _http_get
            (real network). Tests inject an in-memory fake to exercise the
            orchestration (pagination, dedup, early-stop, near-misses) offline.
    Returns:
        List[BoardResult]: one named record per board (fields accessed by name).
    """
    if fetch is None:
        fetch = _http_get
    encoded_query = quote(query)

    def rprint(*args, **kwargs):
        if render:
            print(*args, **kwargs)

    def vprint(message):
        if verbose:
            rprint(f"{Colors.GRAY}[verbose]{Colors.RESET} {message}")

    if logger:
        logger.event(
            "search_start",
            query=query,
            max_results=max_results,
            debug=bool(debug),
            min_views=min_views,
            min_sounds=min_sounds,
            include_dates=bool(include_dates),
            recent_days=recent_days,
            sort_by=sort_by,
            date_sample_size=date_sample_size,
            progress=bool(progress),
            verbose=bool(verbose),
        )

    # Display filter information to user
    filter_info = []
    if min_views > 0:
        filter_info.append(f"min {min_views} views")
    if min_sounds > 0:
        filter_info.append(f"min {min_sounds} sounds")
    if recent_days is not None:
        filter_info.append(f"updated within {recent_days} days (approx)")

    filter_text = f" {Colors.GRAY}(filtering: {', '.join(filter_info)}){Colors.RESET}" if filter_info else ""
    rprint(f"{Colors.BOLD}{Colors.CYAN}Searching for: '{query}'...{filter_text}{Colors.RESET}")

    if filter_info:
        rprint(f"{Colors.GRAY}💡 Tip: Use --min-views 0 --min-sounds 0 to see all results{Colors.RESET}\n")

    recent_threshold = None
    if recent_days is not None:
        recent_threshold = datetime.now(timezone.utc) - timedelta(days=recent_days)
        if logger:
            logger.event("recent_threshold", recent_days=recent_days, threshold_utc=_format_datetime_utc(recent_threshold))

    results = []
    downloadable_count = 0
    skipped_count = 0  # Track boards that were downloadable but didn't meet filters
    skipped_buckets = {
        "views": 0,
        "sounds": 0,
        "updated_unknown": 0,
        "updated_too_old": 0,
    }
    target_downloadable = max_results
    # Cache: url -> (datetime|None, diag_string)
    last_modified_cache = {}
    board_date_stats = {}

    boards_analyzed_total = 0
    boards_fetch_errors = 0
    boards_with_downloads_total = 0
    boards_with_track_date_total = 0
    boards_with_unknown_date_total = 0
    track_headers_ok_total = 0
    track_headers_total_total = 0

    # When using --recent-days, keep track of the newest boards that would have
    # passed the basic filters (downloads/views/sounds) but missed the date window.
    # This lets us provide a helpful suggestion when the date filter yields no results.
    recent_near_misses_too_old = []  # List[Tuple[datetime, str]]
    recent_near_misses_unknown = []  # List[str]

    progress_tty = bool(progress) and render and (not debug) and (not verbose) and sys.stdout.isatty()
    progress_lines = bool(progress) and render and (not debug) and (not verbose) and (not sys.stdout.isatty())
    progress_len = 0
    last_progress_line_at = 0.0

    def _progress(msg):
        nonlocal progress_len, last_progress_line_at
        if not (progress_tty or progress_lines):
            return
        msg = msg.replace("\n", " ")
        if len(msg) > 140:
            msg = msg[:137] + "..."

        if progress_tty:
            if progress_len:
                sys.stdout.write("\r" + msg.ljust(progress_len))
            else:
                sys.stdout.write("\r" + msg)
            progress_len = max(progress_len, len(msg))
            sys.stdout.flush()
        else:
            # Non-TTY fallback: print a status line occasionally.
            now = time.monotonic()
            if (now - last_progress_line_at) >= 2.0:
                rprint(f"{Colors.GRAY}{msg}{Colors.RESET}")
                last_progress_line_at = now

    def _progress_clear():
        nonlocal progress_len
        if not progress_tty or progress_len == 0:
            return
        sys.stdout.write("\r" + (" " * progress_len) + "\r")
        sys.stdout.flush()
        progress_len = 0

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

        vprint(f"Fetching search page {page}: {search_url}")
        if logger:
            logger.event("search_page_fetch_start", page=page, url=search_url)

        # Show "Searching..." message only in debug mode
        if debug:
            if page == 1:
                rprint(f"{Colors.GRAY}Searching page {page}...{Colors.RESET}\n")
            else:
                rprint(f"{Colors.GRAY}Searching page {page} for more results...{Colors.RESET}\n")
        elif page == 1:
            # In normal mode, just show a simple searching message at the start
            rprint(f"{Colors.GRAY}Searching...{Colors.RESET}\n")

        try:
            html_content = fetch(search_url)
            if logger:
                logger.event("search_page_fetch_ok", page=page, url=search_url, bytes=len(html_content))
        except (HTTPError, URLError) as e:
            rprint(f"{Colors.RED}Error searching page {page}: {e}{Colors.RESET}")
            if logger:
                logger.event("search_page_fetch_error", page=page, url=search_url, error=str(e))
            break

        # Extract board slugs from this page (permissive; handles URL-encoded names)
        boards = _extract_board_slugs_from_search_html(html_content)
        vprint(f"Extracted {len(boards)} raw board slug(s) from page {page}")
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
            _progress_clear()
            rprint(f"{Colors.YELLOW}No more boards found (end of search results).{Colors.RESET}\n")
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

            boards_analyzed_total += 1
            _progress(
                f"Analyzing page {page}/{max_pages} ({board_index}/{len(page_boards)}): {board_name} "
                f"| found {downloadable_count}/{target_downloadable}, skipped {skipped_count}"
            )

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
                vprint(f"Fetching board page: {board_url}")
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

                if has_downloads:
                    boards_with_downloads_total += 1
                    status = f"{Colors.GREEN}✓{Colors.RESET}"
                else:
                    status = f"{Colors.RED}✗{Colors.RESET}"
                preview_count = len(sounds_info)

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

                if verbose:
                    vprint(
                        f"Parsed {board_name}: has_downloads={'yes' if has_downloads else 'no'}, "
                        f"sounds={sound_count}, views_int={views_int}, download_buttons={len(download_ids_deduped)}"
                    )

                # Approximate last-updated date (best-effort)
                approx_updated = None
                approx_source = None
                track_headers_ok = None
                track_headers_total = None
                need_date_scan = include_dates or recent_threshold is not None or sort_by == "recent"
                should_scan_dates = False
                if need_date_scan:
                    # Efficiency: date inference requires hitting per-track download URLs.
                    # Skip this work for play-only boards (no download buttons), and also skip
                    # for boards that already fail view/sound filters in non-debug mode.
                    if not has_downloads:
                        vprint(f"Skipping date scan for play-only board: {board_name}")
                    elif fails_basic_filters and not debug:
                        vprint(f"Skipping date scan for filtered board: {board_name}")
                    else:
                        should_scan_dates = True

                if should_scan_dates:
                    # Heuristic:
                    # - Use the max Last-Modified across track download URLs
                    track_date = None

                    candidate_ids = []
                    if download_ids_deduped:
                        candidate_ids = download_ids_deduped
                    elif sound_matches:
                        # Fallback to IDs from data-src if download IDs aren't found
                        candidate_ids = [sid for sid, _ in sound_matches if str(sid).isdigit()]

                    if candidate_ids:
                        # date_sample_size=0 => scan all tracks. Otherwise, scan only the last N as displayed.
                        if date_sample_size and int(date_sample_size) > 0:
                            candidate_ids = candidate_ids[-int(date_sample_size):]

                        vprint(
                            f"Date scan candidates for {board_name}: {len(candidate_ids)} track(s)"
                            + (" (sampled)" if (date_sample_size and int(date_sample_size) > 0) else " (all)")
                        )
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
                            if verbose:
                                vprint(f"  track {sid}: Last-Modified = {_format_datetime_utc(dt)} ({diag})")
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
                            if len(candidate_ids) > 1:
                                time.sleep(HEADER_REQUEST_DELAY)

                        track_date = max_dt

                    if track_date:
                        approx_updated = track_date
                        approx_source = "track"

                    if verbose:
                        vprint(f"Board {board_name}: max Last-Modified = {_format_datetime_utc(approx_updated)}")
                    if logger:
                        logger.event(
                            "board_date_scan_result",
                            board=board_name,
                            board_last_modified_utc=_format_datetime_utc(approx_updated) if approx_updated else None,
                            track_headers_ok=(track_headers_ok or 0),
                            track_headers_total=track_headers_total,
                        )

                    if track_headers_total is not None:
                        board_date_stats[board_name] = (track_headers_ok or 0, track_headers_total)
                        track_headers_ok_total += (track_headers_ok or 0)
                        track_headers_total_total += track_headers_total

                    if has_downloads:
                        if approx_updated:
                            boards_with_track_date_total += 1
                        else:
                            boards_with_unknown_date_total += 1


                # Apply filters (pure decision; bucket attribution stays here).
                # Date filter is only evaluated when the basic filters pass, which
                # keeps skip reasons accurate — see _evaluate_filters.
                meets_filters, filter_failures = _evaluate_filters(
                    views_int, sound_count, approx_updated,
                    min_views, min_sounds, recent_threshold, recent_days,
                )
                filter_reasons = [reason for _, reason in filter_failures]
                if has_downloads:
                    for bucket_key, _ in filter_failures:
                        skipped_buckets[bucket_key] += 1

                # Collect suggestions for --recent-days near-misses.
                # Only consider boards that are downloadable and would otherwise pass the basic filters.
                if recent_threshold is not None and has_downloads and (not fails_basic_filters):
                    if not approx_updated:
                        recent_near_misses_unknown.append(board_name)
                    elif approx_updated < recent_threshold:
                        recent_near_misses_too_old.append((approx_updated, board_name))

                if verbose:
                    vprint(
                        f"Filters for {board_name}: downloads={'yes' if has_downloads else 'no'}, "
                        f"views={views_int}, sounds={sound_count}, updated={_format_datetime_utc(approx_updated)} "
                        f"=> {'PASS' if meets_filters else 'FAIL'}"
                    )
                    if filter_reasons:
                        for reason in filter_reasons:
                            vprint(f"  filtered: {reason}")

                if logger:
                    logger.event(
                        "board_filter_result",
                        board=board_name,
                        has_downloads=bool(has_downloads),
                        meets_filters=bool(meets_filters),
                        filter_reasons=filter_reasons,
                        approx_updated_utc=_format_datetime_utc(approx_updated) if approx_updated else None,
                    )

                # Decide whether to show output (only downloadable boards that meet filters, or everything in debug mode)
                should_show = (has_downloads and meets_filters) or debug

                if should_show:
                    _progress_clear()
                    # Determine the counter to display
                    current_count = downloadable_count + 1 if (has_downloads and meets_filters) else downloadable_count
                    counter_display = f"{Colors.GRAY}[{current_count}/{target_downloadable}]{Colors.RESET}"

                    # Print the board name line
                    if has_downloads and meets_filters:
                        # Normal mode: just show board name with counter
                        rprint(f"{counter_display} {Colors.CYAN}{board_name}{Colors.RESET}")
                    elif debug:
                        # Debug mode: show "Analyzing" prefix for non-qualifying boards
                        rprint(f"{counter_display} Analyzing {Colors.CYAN}{board_name}{Colors.RESET}...")

                    rprint(f"  {status} {sound_count} sounds {Colors.GRAY}(views: {views if views else '0'}){Colors.RESET}")

                    if include_dates:
                        updated_line = _format_updated_line(approx_updated, approx_source, board_date_stats.get(board_name))
                        rprint(f"  {Colors.GRAY}{updated_line}{Colors.RESET}")

                    # In debug mode, show why it was filtered
                    if debug and not meets_filters:
                        for reason in filter_reasons:
                            rprint(f"  {Colors.YELLOW}⚠️  Filtered out: {reason}{Colors.RESET}")

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
                else:
                    # Count skipped downloadable boards (didn't meet filters)
                    if has_downloads:
                        skipped_count += 1

                # Add delay between board checks to be respectful to server
                time.sleep(REQUEST_DELAY)

            except Exception as e:
                boards_fetch_errors += 1
                _progress_clear()
                rprint(f"  {Colors.RED}Error: {e}{Colors.RESET}")
                if logger:
                    logger.event("board_analyze_error", board=board_name, error=str(e))
                # Don't increment downloadable_count on error
                # Move cursor up to hide the "Analyzing" line if not in debug mode
                # (render gates this too — a render=False web worker must not touch
                # the terminal of a server started from a tty).
                if render and (not debug) and (not verbose) and sys.stdout.isatty():
                    sys.stdout.write('\033[F\033[K')
                    sys.stdout.flush()
                continue

        # Move to next page if we haven't found enough boards yet
        if downloadable_count >= target_downloadable:
            break

        page += 1
        time.sleep(REQUEST_DELAY)  # Delay between pages

    _progress_clear()

    # Filter results to only show downloadable boards (already done in meets_filters logic)
    results = [r for r in results if r.has_downloads]

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

    if not results:
        if skipped_count > 0:
            rprint(f"\n{Colors.YELLOW}⚠️  No boards matched your filter criteria.{Colors.RESET}")
            rprint(f"   {skipped_count} downloadable board(s) were skipped due to filters.")
            rprint(f"   Diagnostics: analyzed {boards_analyzed_total} board(s) across up to {page} page(s); fetch errors: {boards_fetch_errors}.")
            if include_dates or recent_threshold is not None or sort_by == "recent":
                rprint(
                    f"   Track-date coverage (downloadable boards): {boards_with_track_date_total} with dates, {boards_with_unknown_date_total} unknown."
                )
                if track_headers_total_total:
                    rprint(
                        f"   Track headers overall: {track_headers_ok_total}/{track_headers_total_total} OK."
                    )
            breakdown = _format_skipped_breakdown(skipped_buckets)
            if breakdown:
                rprint(f"   Skipped breakdown (may overlap): {breakdown}")

            # If the date filter eliminated everything, suggest a more realistic --recent-days.
            if recent_threshold is not None:
                if recent_near_misses_too_old:
                    now_utc = datetime.now(timezone.utc)
                    recent_near_misses_too_old.sort(key=lambda x: x[0], reverse=True)
                    top = recent_near_misses_too_old[:3]
                    needed_days = []

                    rprint(f"\n{Colors.GRAY}💡 Newest boards outside your {recent_days}-day window:{Colors.RESET}")
                    for dt, board_name in top:
                        age_days = int(math.ceil((now_utc - dt).total_seconds() / 86400.0))
                        needed_days.append(age_days)
                        rprint(
                            f"   - {Colors.CYAN}{board_name}{Colors.RESET}: {_format_date(dt)} (~{age_days} days ago)"
                        )

                    if needed_days:
                        suggested_days = max(needed_days)
                        start_date = (now_utc - timedelta(days=suggested_days)).date().isoformat()
                        rprint(
                            f"   Try {Colors.YELLOW}--recent-days {suggested_days}{Colors.RESET} "
                            f"(window starts ~{start_date}) to include these."
                        )

                if recent_near_misses_unknown:
                    # These can never satisfy a strict recent-days filter because updated date can't be inferred.
                    rprint(
                        f"\n{Colors.GRAY}ℹ️  Also found {len(recent_near_misses_unknown)} downloadable board(s) with unknown updated dates; "
                        f"they can't pass a strict {Colors.YELLOW}--recent-days{Colors.RESET} filter.{Colors.RESET}"
                    )
                    rprint(f"   Tip: Remove --recent-days to include them, or increase --date-sample-size for more thorough date inference.")

            rprint("   Try increasing --max, adjusting --min-views/--min-sounds, relaxing --recent-days, or use --debug to see why boards were filtered.")
        else:
            rprint(f"\n{Colors.YELLOW}⚠️  No downloadable boards found.{Colors.RESET}")
            rprint("   Try a different search, adjust filters with --min-views 0 --min-sounds 0, or use --debug to see all analyzed boards.")
            rprint(f"   Diagnostics: analyzed {boards_analyzed_total} board(s) across up to {page} page(s); fetch errors: {boards_fetch_errors}.")
        if logger:
            logger.event(
                "search_end_no_results",
                analyzed=boards_analyzed_total,
                pages=page,
                fetch_errors=boards_fetch_errors,
                skipped=skipped_count,
                skipped_buckets=skipped_buckets,
                track_headers_ok_total=track_headers_ok_total,
                track_headers_total_total=track_headers_total_total,
                boards_with_track_date_total=boards_with_track_date_total,
                boards_with_unknown_date_total=boards_with_unknown_date_total,
            )
        return results

    rprint(f"\n{Colors.BOLD}{'='*80}")
    rprint(f"{'SEARCH RESULTS':^80}")
    rprint(f"{'='*80}{Colors.RESET}\n")

    for board in results:
        for line in _render_board_lines(board, board_date_stats.get(board.board_name), include_dates):
            rprint(line)
        rprint("\n")  # Two newlines after each board

    rprint(f"{Colors.BOLD}{'='*80}{Colors.RESET}")

    # Show skipped boards summary if any were filtered out
    if skipped_count > 0:
        rprint(f"\n{Colors.YELLOW}ℹ️  {skipped_count} downloadable board(s) were skipped due to filter criteria.{Colors.RESET}")
        breakdown = _format_skipped_breakdown(skipped_buckets)
        if breakdown:
            rprint(f"   Skipped breakdown (may overlap): {breakdown}")
        if min_views > 0 or min_sounds > 0:
            rprint(f"   Adjust --min-views or --min-sounds to include them in results.")

    if results and results[0].has_downloads:
        rprint(f"\n{Colors.BOLD}To download a board, use:{Colors.RESET}")
        rprint(f"  {Colors.GRAY}python3 soundboard-snag.py --board \"{results[0].board_name}\"{Colors.RESET}")

    return results

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


def run_server(host, port, download_root, logger=None, max_jobs=2):
    """Start the local web UI + JSON/SSE API, reusing the search/download engines.

    Additive feature: the one-shot CLI is unaffected. Server-side downloads land
    under ``download_root`` (or CWD), mirroring the CLI. Stdlib only.
    """
    import json as _json
    import threading
    import queue as _queue
    from http.server import BaseHTTPRequestHandler
    try:
        from http.server import ThreadingHTTPServer  # Python 3.7+
    except ImportError:  # Python 3.6 fallback
        import socketserver
        from http.server import HTTPServer

        class ThreadingHTTPServer(socketserver.ThreadingMixIn, HTTPServer):
            daemon_threads = True
    from urllib.parse import urlparse as _urlparse, parse_qs, unquote as _unquote

    web_root = os.path.realpath(os.path.join(os.path.dirname(os.path.abspath(__file__)), "web"))
    server_root = os.path.realpath(download_root) if download_root else os.path.realpath(os.getcwd())
    job_sem = threading.BoundedSemaphore(max_jobs)

    CONTENT_TYPES = {
        ".html": "text/html; charset=utf-8",
        ".css": "text/css; charset=utf-8",
        ".js": "application/javascript; charset=utf-8",
        ".svg": "image/svg+xml",
        ".json": "application/json; charset=utf-8",
        ".ico": "image/x-icon",
        ".woff2": "font/woff2",
    }

    def emit_log(event_type, **fields):
        if logger:
            try:
                logger.event(event_type, **fields)
            except Exception:
                pass

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
            emit_log("http", msg=(fmt % args))

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
            return self._serve_static(path)

        def do_POST(self):
            path = _urlparse(self.path).path
            if path == "/api/download":
                return self._api_download()
            if path == "/api/download-sound":
                return self._api_download_sound()
            if path == "/api/shutdown":
                return self._api_shutdown()
            self._send_json({"error": "not found"}, status=404)

        # ---- POST /api/shutdown (stops the server; used by the app's Stop button) ----
        def _api_shutdown(self):
            # State-changing: refuse cross-site requests (CSRF) and non-loopback
            # peers (even if the server was bound to 0.0.0.0).
            if not self._origin_ok() or not is_loopback(self.client_address[0]):
                self._send_json({"error": "forbidden"}, status=403)
                return
            self._send_json({"ok": True})
            # shutdown() must run off the serve_forever thread, so spawn one.
            threading.Thread(target=self.server.shutdown, daemon=True).start()

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
                        emit(event_type, **fields)

                def worker():
                    try:
                        # Bound date inference: a small per-board sample keeps a
                        # recent/dated search from probing every track of every
                        # board, and time_budget caps total wall-clock so a popular
                        # term returns partial results instead of hanging.
                        dss = date_sample_size if date_sample_size > 0 else 4
                        results = search_boards(
                            query, max_results, False, min_views, min_sounds,
                            include_dates=include_dates, recent_days=recent_days,
                            sort_by=sort_by, date_sample_size=dss,
                            progress=False, verbose=False, logger=_Sink(),
                            render=False, cancel_event=cancel, time_budget=30,
                        )
                        emit("results", [board_result_to_dict(b) for b in results])
                    except Exception as e:
                        emit("error", {"message": str(e)})
                return worker

            self._run_sse_job(build)

        # ---- POST /api/download (SSE) ----
        def _api_download(self):
            if not self._origin_ok():  # CSRF guard
                self._send_json({"error": "forbidden"}, status=403)
                return
            try:
                length = int(self.headers.get("Content-Length", 0) or 0)
            except ValueError:
                length = 0
            if length > 64 * 1024:  # cap body size — no reason a board POST is large
                self._send_json({"error": "request too large"}, status=413)
                return
            raw = self.rfile.read(length) if length else b""
            try:
                body = _json.loads(raw.decode("utf-8")) if raw else {}
            except Exception:
                self._send_json({"error": "bad json"}, status=400)
                return
            board = (body.get("board") or "").strip()
            if not valid_board(board):
                self._send_json({"error": "bad board"}, status=400)
                return

            root = server_root
            req_root = body.get("download_root")
            if req_root:
                # expanduser BEFORE abspath so a leading ~ resolves to $HOME.
                cand = os.path.realpath(os.path.abspath(os.path.expanduser(req_root)))
                try:
                    if os.path.commonpath([server_root, cand]) != server_root:
                        self._send_json({"error": "download_root escapes server root"}, status=400)
                        return
                except ValueError:
                    self._send_json({"error": "bad download_root"}, status=400)
                    return
                root = cand

            def build(emit, cancel):
                def worker():
                    try:
                        board_url = "%s/sb/%s" % (BASE_URL, _quote_path_segment(board))
                        SoundboardSnag(board_url, download_root=root, event_cb=emit,
                                       render=False, cancel_event=cancel).snag()
                    except Exception as e:
                        emit("download_error", {"error": str(e)})
                return worker

            self._run_sse_job(build)

        # ---- POST /api/download-sound (one track; JSON, not SSE) ----
        def _api_download_sound(self):
            if not self._origin_ok():  # CSRF guard
                self._send_json({"error": "forbidden"}, status=403)
                return
            try:
                length = int(self.headers.get("Content-Length", 0) or 0)
            except ValueError:
                length = 0
            if length > 64 * 1024:
                self._send_json({"error": "request too large"}, status=413)
                return
            raw = self.rfile.read(length) if length else b""
            try:
                body = _json.loads(raw.decode("utf-8")) if raw else {}
            except Exception:
                self._send_json({"error": "bad json"}, status=400)
                return
            board = (body.get("board") or "").strip()
            sound_id = str(body.get("sound_id") or "").strip()
            title = (body.get("title") or "").strip()
            if not valid_board(board) or not sound_id.isdigit():
                self._send_json({"error": "bad params"}, status=400)
                return
            if not job_sem.acquire(blocking=False):
                self._send_json({"error": "busy"}, status=503)
                return
            try:
                board_url = "%s/sb/%s" % (BASE_URL, _quote_path_segment(board))
                tool = SoundboardSnag(board_url, download_root=server_root, render=False)
                output_dir = os.path.join(tool.download_root, tool._board_output_dirname())
                os.makedirs(output_dir, exist_ok=True)
                result, data = tool._snag_sound(sound_id, title, output_dir)
                if result is True:
                    name, kb = data
                    self._send_json({"status": "saved", "name": name, "kb": round(kb, 1)})
                elif result is None:
                    self._send_json({"status": "exists", "name": data})
                else:
                    self._send_json({"status": "error", "error": data})
            except Exception as e:
                self._send_json({"status": "error", "error": str(e)})
            finally:
                job_sem.release()

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
                tool = SoundboardSnag(board_url, download_root=server_root)
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
    url = "http://%s:%d" % (host, port)
    print(f"{Colors.BOLD}{Colors.CYAN}soundboard-snag{Colors.RESET}  web UI  →  {Colors.GREEN}{url}{Colors.RESET}")
    print(f"{Colors.GRAY}  serving assets from {web_root}{Colors.RESET}")
    print(f"{Colors.GRAY}  downloads save under {server_root}{Colors.RESET}")
    if host not in ("127.0.0.1", "localhost", "::1"):
        print(f"{Colors.YELLOW}  warning: bound to {host} — anyone on the network can trigger downloads.{Colors.RESET}")
    print(f"{Colors.GRAY}  press Ctrl+C to stop.{Colors.RESET}")
    emit_log("server_start", host=host, port=port, web_root=web_root, download_root=server_root)
    try:
        httpd.serve_forever()
    finally:
        httpd.server_close()


def main():
    """Command-line interface."""
    # Make HTTPS work on python.org Python regardless of how we were launched.
    _ensure_ca_bundle()
    # Get current working directory for help text
    cwd = os.getcwd()

    epilog_text = f"""Examples:
  # Search for boards (recommended first step)
  # Note: Search automatically filters low-quality boards by default
  %(prog)s --search "star wars"

  # Search with stricter quality filters
  %(prog)s --search "star wars" --min-views 100 --min-sounds 10

  # Search and download all results automatically
  %(prog)s --search-and-download "star wars" --max 5

  # Search without any filters (show all results)
  %(prog)s --search "star wars" --min-views 0 --min-sounds 0

  # Show approximate updated dates (best-effort)
  %(prog)s --search "star wars" --include-dates

  # Find recently updated boards (approx)
  %(prog)s --search "star wars" --recent-days 7 --sort recent

  # Download by board name (from search results)
  %(prog)s --board starwars

  # Alternative: Download by URL
  %(prog)s --url https://www.soundboard.com/sb/starwars

  # Specify custom download location
  %(prog)s --board starwars --download-root ~/Music/Soundboards

Note: A subfolder will be created with the name of the soundboard (e.g., 'starwars').
      Default download location: {cwd}"""

    parser = argparse.ArgumentParser(
        description="Snag audio files from soundboard.com with clean filenames. "
                    "Search automatically filters low-quality boards (use --min-views 0 --min-sounds 0 to disable).",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=epilog_text
    )
    parser.add_argument(
        "-b", "--board",
        type=str,
        help="Board name to download (e.g., 'starwars' - get from search results)"
    )
    parser.add_argument(
        "-u", "--url",
        type=str,
        help="Full URL to a soundboard.com page (alternative to --board)"
    )
    parser.add_argument(
        "-s", "--search",
        type=str,
        help="Search for downloadable boards (e.g., 'star wars', 'hockey')"
    )
    parser.add_argument(
        "--search-and-download",
        type=str,
        help="Search for boards and download all results automatically (e.g., 'star wars', 'hockey')"
    )
    parser.add_argument(
        "--max",
        type=int,
        default=20,
        help="Maximum boards to check in search (default: 20)"
    )
    parser.add_argument(
        "--debug",
        action="store_true",
        help="Show all boards being analyzed, including non-downloadable ones and filter reasons"
    )
    parser.add_argument(
        "--min-views",
        type=int,
        default=10,
        help="Minimum views required for search results (default: 10, use 0 for no filter)"
    )
    parser.add_argument(
        "--min-sounds",
        type=int,
        default=3,
        help="Minimum number of sounds required for search results (default: 3, use 0 for no filter)"
    )
    parser.add_argument(
        "--include-dates",
        action="store_true",
        help="Show approximate updated dates via Last-Modified headers (extra requests)"
    )
    parser.add_argument(
        "--recent-days",
        type=int,
        default=None,
        help="Only show boards updated within the last N days (approx)"
    )
    parser.add_argument(
        "--sort",
        choices=["views", "recent"],
        default="views",
        help="Sort search results by views or recent updated date (approx)"
    )

    parser.add_argument(
        "--no-progress",
        dest="progress",
        action="store_false",
        help="Disable realtime progress updates during searches"
    )
    parser.add_argument(
        "--verbose",
        action="store_true",
        help="Verbose output showing detailed steps, detection, parsing, and HTTP date checks"
    )
    parser.add_argument(
        "--log-file",
        type=str,
        default=None,
        help="Write a JSONL log of actions/events to this file (one JSON object per line)"
    )
    parser.add_argument(
        "--date-sample-size",
        type=int,
        default=0,
        help="When fetching updated dates, how many track headers to check per board. "
             "0 scans all tracks (most accurate, more requests)."
    )
    parser.add_argument(
        "-d", "--download-root",
        type=str,
        default=None,
        help="Root directory for downloads (default: current working directory). "
             "A subfolder with the board name will be created inside this directory."
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="With --search, print results as a JSON array to stdout (for automation); "
             "suppresses the human-readable rendering."
    )
    parser.add_argument(
        "--serve",
        action="store_true",
        help="Start a local web UI / JSON+SSE API instead of running a one-shot command."
    )
    parser.add_argument(
        "--host",
        type=str,
        default="127.0.0.1",
        help="Host/interface for --serve (default: 127.0.0.1, localhost only). "
             "Use 0.0.0.0 to expose on your network (lets others trigger downloads)."
    )
    parser.add_argument(
        "--port",
        type=int,
        default=8765,
        help="Port for --serve (default: 8765)."
    )


    args = parser.parse_args()

    if args.recent_days is not None and args.recent_days <= 0:
        print(f"{Colors.RED}Error: --recent-days must be a positive integer.{Colors.RESET}")
        sys.exit(1)

    include_dates = args.include_dates or args.recent_days is not None or args.sort == "recent"

    run_logger = None
    if getattr(args, "log_file", None):
        try:
            run_logger = JsonlLogger(args.log_file)
        except Exception as e:
            print(f"{Colors.RED}Error opening --log-file: {e}{Colors.RESET}")
            sys.exit(1)

    # Expand and resolve the download root path if provided
    download_root = None
    if args.download_root:
        download_root = os.path.abspath(os.path.expanduser(args.download_root))
        # Create the root directory if it doesn't exist
        if not os.path.exists(download_root):
            try:
                os.makedirs(download_root)
                print(f"{Colors.BLUE}Created download root directory: {download_root}{Colors.RESET}\n")
            except OSError as e:
                print(f"{Colors.RED}Error creating download root directory: {e}{Colors.RESET}")
                sys.exit(1)

    # Web UI mode: short-circuit before the one-shot CLI dispatch, but after the
    # shared download_root resolution above so the server's root already exists.
    if getattr(args, "serve", False):
        try:
            run_server(args.host, args.port, download_root, logger=run_logger)
        except KeyboardInterrupt:
            print("\nServer stopped.")
        finally:
            if run_logger:
                run_logger.close()
        sys.exit(0)

    # Handle search-and-download mode
    if args.search_and_download:
        try:
            results = search_boards(
                args.search_and_download,
                args.max,
                args.debug,
                args.min_views,
                args.min_sounds,
                include_dates=include_dates,
                recent_days=args.recent_days,
                sort_by=args.sort,
                date_sample_size=args.date_sample_size,
                progress=getattr(args, "progress", True),
                verbose=getattr(args, "verbose", False),
                logger=run_logger,
            )

            if not results:
                print(f"\n{Colors.YELLOW}No boards to download.{Colors.RESET}")
                sys.exit(0)

            # Download each board. search_boards already returns only
            # downloadable boards, so no re-filter is needed here.
            downloadable_boards = results
            total_boards = len(downloadable_boards)

            print(f"\n{Colors.BOLD}{Colors.CYAN}Starting download of {total_boards} board(s)...{Colors.RESET}\n")

            successful = 0
            failed = 0

            for idx, board in enumerate(downloadable_boards, 1):
                print(f"\n{Colors.BOLD}{'='*80}")
                print(f"Board {idx}/{total_boards}: {board.board_name}")
                print(f"{'='*80}{Colors.RESET}\n")

                try:
                    board_url = f"{BASE_URL}/sb/{_quote_path_segment(board.board_name)}"
                    snag_tool = SoundboardSnag(board_url, download_root=download_root)
                    success = snag_tool.snag()

                    if success:
                        successful += 1
                    else:
                        failed += 1

                except Exception as e:
                    print(f"{Colors.RED}Error downloading {board.board_name}: {e}{Colors.RESET}")
                    failed += 1

                # Add delay between boards
                if idx < total_boards:
                    time.sleep(REQUEST_DELAY)

            # Summary
            print(f"\n{Colors.BOLD}{'='*80}")
            print(f"{'DOWNLOAD SUMMARY':^80}")
            print(f"{'='*80}{Colors.RESET}")
            print(f"{Colors.GREEN}Successful: {successful}{Colors.RESET}")
            if failed > 0:
                print(f"{Colors.RED}Failed: {failed}{Colors.RESET}")
            print(f"{Colors.BOLD}Total: {total_boards}{Colors.RESET}\n")

            sys.exit(0)

        except KeyboardInterrupt:
            print("\n\nSearch and download cancelled by user.")
            sys.exit(1)
        except Exception as e:
            print(f"Search and download error: {e}")
            sys.exit(1)
        finally:
            if run_logger:
                run_logger.close()

    # Handle search mode
    if args.search:
        try:
            results = search_boards(
                args.search,
                args.max,
                args.debug,
                args.min_views,
                args.min_sounds,
                include_dates=include_dates,
                recent_days=args.recent_days,
                sort_by=args.sort,
                date_sample_size=args.date_sample_size,
                progress=getattr(args, "progress", True),
                verbose=getattr(args, "verbose", False),
                logger=run_logger,
                render=not getattr(args, "json", False),
            )
            if getattr(args, "json", False):
                json.dump([board_result_to_dict(b) for b in results], sys.stdout, ensure_ascii=False)
                sys.stdout.write("\n")
            sys.exit(0)
        except KeyboardInterrupt:
            print("\n\nSearch cancelled by user.")
            sys.exit(1)
        except Exception as e:
            print(f"Search error: {e}")
            sys.exit(1)
        finally:
            if run_logger:
                run_logger.close()

    # Get URL from command line (board name or full URL) or interactive input
    if args.board:
        # User provided board name - construct URL
        soundboard_url = f"{BASE_URL}/sb/{_quote_path_segment(args.board)}"
        print(f"{Colors.CYAN}Using board name: {args.board}{Colors.RESET}")
        print(f"{Colors.GRAY}Constructed URL: {soundboard_url}{Colors.RESET}\n")
    elif args.url:
        soundboard_url = args.url
    else:
        print("Enter a soundboard.com page URL or board name.")
        print("Example URL: https://www.soundboard.com/sb/starwars")
        print("Example board name: starwars\n")
        print("NOTE: When using VS Code debugger, set arguments in launch.json\n")

        try:
            user_input = input("URL or board name: ").strip()
        except EOFError:
            print("\nError: No input received.")
            print("For debugging, add arguments to launch configuration:")
            print('  "args": ["--url", "https://www.soundboard.com/sb/starwars"]')
            sys.exit(1)

        # Detect if user entered a plain board name (no scheme/host)
        if user_input and not user_input.startswith(('http://', 'https://')):
            soundboard_url = f"{BASE_URL}/sb/{_quote_path_segment(user_input)}"
            print(f"{Colors.CYAN}Using board name: {user_input}{Colors.RESET}")
            print(f"{Colors.GRAY}Constructed URL: {soundboard_url}{Colors.RESET}\n")
        else:
            soundboard_url = user_input

    # Run snag tool
    try:
        snag_tool = SoundboardSnag(soundboard_url, download_root=download_root)
        success = snag_tool.snag()

        if not success:
            sys.exit(1)

        # Interactive mode: wait for keypress (only when no CLI args were given)
        if not args.url and not args.board:
            input("\nComplete. Press Enter to exit...")

    except (ValueError, RuntimeError) as e:
        print(f"Error: {e}")
        sys.exit(1)
    except KeyboardInterrupt:
        print("\n\nSnagging cancelled by user.")
        sys.exit(1)
    except Exception as e:
        print(f"Unexpected error: {type(e).__name__}: {e}")
        sys.exit(1)
    finally:
        if run_logger:
            run_logger.close()


if __name__ == "__main__":
    main()

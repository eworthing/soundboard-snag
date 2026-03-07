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
DOWNLOAD_BUTTON_PATTERN = r'<a href="/sb/sound/(\d+)"[^>]*class="[^"]*btn-download-track'
WINDOWS_RESERVED_NAMES = {'CON', 'PRN', 'AUX', 'NUL'}
WINDOWS_RESERVED_NAMES.update({f'COM{i}' for i in range(1, 10)})
WINDOWS_RESERVED_NAMES.update({f'LPT{i}' for i in range(1, 10)})


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
    """ANSI color codes for terminal output."""
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

    def __init__(self, soundboard_url, download_root=None):
        """Initialize snag tool with a soundboard URL.

        Args:
            soundboard_url: A string URL pointing to a soundboard.com page.
                Expected format: https://www.soundboard.com/sb/boardname
            download_root: Optional root directory for downloads. If None, uses CWD.

        Raises:
            ValueError: If the URL format is invalid or board name cannot
                be extracted.
        """
        self.url = urlparse(soundboard_url)
        self.board_slug, self.board_name = self._extract_board_slug_and_name()
        self.base_url = BASE_URL
        self.download_root = download_root if download_root else os.getcwd()

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
            RuntimeError: If the page cannot be retrieved (HTTP errors, network
                errors, or non-200 status codes).
        """
        page_url = self._board_url()

        # Create request with User-Agent to avoid being blocked
        req = Request(page_url, headers={'User-Agent': USER_AGENT})

        try:
            with urlopen(req, timeout=HTTP_TIMEOUT) as response:
                content = response.read().decode('utf-8')
                return content

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
        download_buttons = re.findall(DOWNLOAD_BUTTON_PATTERN, html_content)
        return (len(download_buttons) > 0, len(download_buttons))

    def _parse_sound_items(self, html_content):
        """Extract sound IDs and titles from the page HTML."""
        # Pattern matches: data-src="ID" ... <span>Title</span>
        pattern = r'<div class="item r"[^>]*data-src="(\d+)"[^>]*>.*?<div class="item-title text-ellipsis">\s*<span>(.*?)</span>'
        matches = re.findall(pattern, html_content, re.DOTALL)

        if matches:
            return matches

        # Fallback: extract IDs only if pattern doesn't match
        sound_ids = re.findall(DOWNLOAD_BUTTON_PATTERN, html_content)
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
        if self.board_slug and self.board_name and self.board_slug != self.board_name:
            print(f"{Colors.BOLD}{Colors.CYAN}Snagging from board: {self.board_name} ({self.board_slug}){Colors.RESET}")
        else:
            print(f"{Colors.BOLD}{Colors.CYAN}Snagging from board: {self.board_name}{Colors.RESET}")

        # Fetch and parse page
        html_content = self._fetch_page()

        # Check if downloads are enabled
        has_downloads, download_count = self._check_downloads_enabled(html_content)

        sound_items = self._parse_sound_items(html_content)

        if not sound_items:
            raise RuntimeError("No audio files found on this soundboard page")

        print(f"{Colors.GREEN}Located {len(sound_items)} audio files to snag!{Colors.RESET}")

        # Check if downloads are enabled - fail fast if not
        if not has_downloads:
            board_url = self._board_url()
            print(f"\n{Colors.RED}❌ ERROR: This board has downloads disabled!{Colors.RESET}")
            print(f"   Found {len(sound_items)} sounds but {Colors.YELLOW}no download buttons{Colors.RESET}.")
            print(f"   The board owner has restricted this board to play-only mode.")
            print(f"\n   Board URL: {Colors.CYAN}{board_url}{Colors.RESET}")
            print(f"   You can verify by visiting the board and checking for download links.")
            print(f"\n   This board cannot be downloaded. Please try a different board.")
            print(f"   Boards with download buttons will work (e.g., starwars, R2D2_R2_D2_sounds)")
            raise RuntimeError("Board has downloads disabled - cannot proceed")

        print(f"   {Colors.GRAY}({download_count} download buttons detected){Colors.RESET}")

        # Show download location
        output_dir = os.path.join(self.download_root, self._board_output_dirname())
        print(f"   {Colors.GRAY}Download location: {os.path.abspath(output_dir)}{Colors.RESET}\n")

        # Download each sound
        snagged_count = 0
        existing_count = 0
        failed_count = 0
        consecutive_failures = 0
        max_consecutive_failures = 2  # Exit if this many failures in a row
        early_exit = False

        for i, (sound_id, page_title) in enumerate(sound_items, 1):
            print(f"{Colors.GRAY}[{i}/{len(sound_items)}]{Colors.RESET} Snagging audio ID {Colors.CYAN}{sound_id}{Colors.RESET}...")

            # Create output directory only when needed (before first download attempt)
            if not os.path.exists(output_dir):
                os.makedirs(output_dir)
                print(f"  {Colors.BLUE}Created directory: {os.path.abspath(output_dir)}{Colors.RESET}")

            result, data = self._snag_sound(sound_id, page_title, output_dir)

            if result is True:
                final_filename, size_kb = data
                print(f"  {Colors.GREEN}✓ Snagged:{Colors.RESET} {final_filename} {Colors.GRAY}({size_kb:.1f} KB){Colors.RESET}")
                snagged_count += 1
                consecutive_failures = 0  # Reset on success
            elif result is None:
                print(f"  {Colors.YELLOW}○ Skipped (exists):{Colors.RESET} {data}")
                existing_count += 1
                consecutive_failures = 0  # Reset on skip (file exists = not a failure)
            else:
                print(f"  {Colors.RED}✗ Failed:{Colors.RESET} {data}")
                failed_count += 1
                consecutive_failures += 1

                # Check if we've hit the consecutive failure limit
                if consecutive_failures >= max_consecutive_failures:
                    remaining = len(sound_items) - i
                    print(f"\n{Colors.RED}❌ ERROR: {consecutive_failures} consecutive download failures detected!{Colors.RESET}")
                    print(f"   This board appears to have invalid or broken download links.")
                    print(f"   Attempted: {i}/{len(sound_items)} files")
                    print(f"   Skipping remaining {remaining} file(s) to avoid wasting time and server resources.")

                    # Clean up empty directory if no files were successfully downloaded or existed
                    if snagged_count == 0 and existing_count == 0:
                        if os.path.exists(output_dir) and os.path.isdir(output_dir):
                            try:
                                os.rmdir(output_dir)
                                print(f"   {Colors.GRAY}Removed empty directory: {os.path.abspath(output_dir)}{Colors.RESET}")
                            except OSError:
                                pass  # Directory not empty or other error, leave it

                    early_exit = True
                    break

            # Add delay between downloads to be respectful to server
            if i < len(sound_items):  # Don't delay after the last one
                time.sleep(REQUEST_DELAY)

        # Summary
        full_path = os.path.abspath(output_dir)
        print(f"\n{Colors.GREEN}{Colors.BOLD}✓ Snagging complete!{Colors.RESET} {Colors.CYAN}{snagged_count}{Colors.RESET} files saved to:")
        print(f"  {Colors.BOLD}{full_path}{Colors.RESET}")
        if existing_count > 0:
            print(f"  {Colors.YELLOW}({existing_count} files were already present){Colors.RESET}")
        if failed_count > 0:
            print(f"  {Colors.RED}⚠️  {failed_count} files failed to download{Colors.RESET}")
            if not has_downloads:
                print(f"  Note: This board has downloads disabled by the owner.")

        return not early_exit


def _format_date_line(approx_updated, approx_source, board_date_stats, board_name, indent="  "):
    """Format the approximate-updated-date display line."""
    extra = ""
    stats = board_date_stats.get(board_name)
    if stats:
        ok, total = stats
        extra = f"; track headers: {ok}/{total}"
    if approx_updated:
        src = f" via {approx_source}" if approx_source else ""
        return f"{indent}{Colors.GRAY}Updated: {_format_date(approx_updated)} (approx{src}{extra}){Colors.RESET}"
    return f"{indent}{Colors.GRAY}Updated: unknown (approx{extra}){Colors.RESET}"


def _format_skip_breakdown(skipped_buckets):
    """Format the skipped-boards breakdown string, or empty string if none."""
    parts = []
    if skipped_buckets["views"]:
        parts.append(f"views: {skipped_buckets['views']}")
    if skipped_buckets["sounds"]:
        parts.append(f"sounds: {skipped_buckets['sounds']}")
    if skipped_buckets["updated_unknown"]:
        parts.append(f"updated unknown: {skipped_buckets['updated_unknown']}")
    if skipped_buckets["updated_too_old"]:
        parts.append(f"updated too old: {skipped_buckets['updated_too_old']}")
    if parts:
        return f"   Skipped breakdown (may overlap): {', '.join(parts)}"
    return ""


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
    Returns:
        List of tuples: (board_name, has_downloads, sounds_list, total_count, board_desc, category, views, tags, views_int, approx_updated, approx_source)
    """
    encoded_query = quote(query)

    def vprint(message):
        if verbose:
            print(f"{Colors.GRAY}[verbose]{Colors.RESET} {message}")

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
    print(f"{Colors.BOLD}{Colors.CYAN}Searching for: '{query}'...{filter_text}{Colors.RESET}")

    if filter_info:
        print(f"{Colors.GRAY}💡 Tip: Use --min-views 0 --min-sounds 0 to see all results{Colors.RESET}\n")

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

    progress_tty = bool(progress) and (not debug) and (not verbose) and sys.stdout.isatty()
    progress_lines = bool(progress) and (not debug) and (not verbose) and (not sys.stdout.isatty())
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
                print(f"{Colors.GRAY}{msg}{Colors.RESET}")
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

    while keep_searching and page <= max_pages:
        search_url = f"{BASE_URL}/search/{encoded_query}?page={page}" if page > 1 else f"{BASE_URL}/search/{encoded_query}"

        vprint(f"Fetching search page {page}: {search_url}")
        if logger:
            logger.event("search_page_fetch_start", page=page, url=search_url)

        # Show "Searching..." message only in debug mode
        if debug:
            if page == 1:
                print(f"{Colors.GRAY}Searching page {page}...{Colors.RESET}\n")
            else:
                print(f"{Colors.GRAY}Searching page {page} for more results...{Colors.RESET}\n")
        elif page == 1:
            # In normal mode, just show a simple searching message at the start
            print(f"{Colors.GRAY}Searching...{Colors.RESET}\n")

        try:
            req = Request(search_url, headers={'User-Agent': USER_AGENT})
            with urlopen(req, timeout=HTTP_TIMEOUT) as response:
                html_content = response.read().decode('utf-8')
            if logger:
                logger.event("search_page_fetch_ok", page=page, url=search_url, bytes=len(html_content))
        except (HTTPError, URLError) as e:
            print(f"{Colors.RED}Error searching page {page}: {e}{Colors.RESET}")
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
            print(f"{Colors.YELLOW}No more boards found (end of search results).{Colors.RESET}\n")
            if logger:
                logger.event("search_end_no_more_boards", page=page)
            break

        # Analyze boards from this page
        for board_index, board_name in enumerate(page_boards, 1):
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
                req = Request(board_url, headers={'User-Agent': USER_AGENT})

                with urlopen(req, timeout=HTTP_TIMEOUT) as response:
                    board_html = response.read().decode('utf-8')
                if logger:
                    logger.event("board_fetch_ok", board=board_name, url=board_url, bytes=len(board_html))

                # Extract sound IDs and titles
                sound_pattern = r'data-src="(\d+)".*?<span>([^<]+)</span>'
                sound_matches = re.findall(sound_pattern, board_html, re.DOTALL)

                # Check if first sound has download button
                has_downloads = re.search(DOWNLOAD_BUTTON_PATTERN, board_html) is not None
                if has_downloads:
                    boards_with_downloads_total += 1

                # Extract downloadable sound IDs (more reliable for date checks than data-src)
                download_ids = re.findall(DOWNLOAD_BUTTON_PATTERN, board_html)
                # De-duplicate while preserving order
                download_ids_deduped = []
                seen_download_ids = set()
                for sid in download_ids:
                    if sid not in seen_download_ids:
                        download_ids_deduped.append(sid)
                        seen_download_ids.add(sid)

                # Extract board description
                desc_pattern = r'<p class="item-desc[^"]*"[^>]*>([^<]*)</p>'
                desc_match = re.search(desc_pattern, board_html)
                board_desc = html.unescape(desc_match.group(1).strip()) if desc_match and desc_match.group(1).strip() else ""

                # Extract category
                cat_pattern = r'<strong>Category:\s*</strong>\s*<span class="text-muted">\s*([^<]+)</span>'
                cat_match = re.search(cat_pattern, board_html)
                category = html.unescape(cat_match.group(1).strip()) if cat_match else ""

                # Extract views
                views_pattern = r'<strong>Views:\s*</strong>\s*<span class="text-muted">\s*([^<]+)</span>'
                views_match = re.search(views_pattern, board_html)
                views = html.unescape(views_match.group(1).strip()) if views_match else ""

                # Extract tags
                tags_section = r'<strong>Tags:\s*</strong>(.*?)</div>'
                tags_match = re.search(tags_section, board_html, re.DOTALL)
                tags = []
                if tags_match:
                    tags_html = tags_match.group(1)
                    tag_pattern = r'<a[^>]*>([^<]+)</a>'
                    tags = [html.unescape(t.strip()) for t in re.findall(tag_pattern, tags_html) if t.strip()]

                # Extract filenames for preview (first 10)
                sounds_info = []
                preview_limit = 10  # Show first 10 files
                for sound_id, title in sound_matches[:preview_limit]:
                    title_clean = html.unescape(title.strip())
                    sounds_info.append((sound_id, title_clean))

                if has_downloads:
                    status = f"{Colors.GREEN}✓{Colors.RESET}"
                else:
                    status = f"{Colors.RED}✗{Colors.RESET}"
                sound_count = len(sound_matches)
                preview_count = len(sounds_info)

                # Convert views to integer for sorting (handle commas and missing values)
                views_int = _parse_views_count(views)

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


                # Apply filters first
                meets_filters = True
                filter_reasons = []
                if min_views > 0 and views_int < min_views:
                    meets_filters = False
                    if has_downloads:
                        skipped_buckets["views"] += 1
                    filter_reasons.append(f"views ({views_int}) < min_views ({min_views})")
                if min_sounds > 0 and sound_count < min_sounds:
                    meets_filters = False
                    if has_downloads:
                        skipped_buckets["sounds"] += 1
                    filter_reasons.append(f"sounds ({sound_count}) < min_sounds ({min_sounds})")
                # Only apply the date-based filter if the board still passes the basic filters.
                # This keeps skip reasons accurate (e.g., don't mark a board as "updated unknown"
                # if we intentionally skipped date inference because it already failed views/sounds).
                if recent_threshold is not None and meets_filters:
                    if not approx_updated:
                        meets_filters = False
                        if has_downloads:
                            skipped_buckets["updated_unknown"] += 1
                        filter_reasons.append("updated date unavailable")
                    elif approx_updated < recent_threshold:
                        meets_filters = False
                        if has_downloads:
                            skipped_buckets["updated_too_old"] += 1
                        filter_reasons.append(f"updated ({_format_date(approx_updated)}) older than {recent_days} days")

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
                        print(f"{counter_display} {Colors.CYAN}{board_name}{Colors.RESET}")
                    elif debug:
                        # Debug mode: show "Analyzing" prefix for non-qualifying boards
                        print(f"{counter_display} Analyzing {Colors.CYAN}{board_name}{Colors.RESET}...")

                    print(f"  {status} {sound_count} sounds {Colors.GRAY}(views: {views if views else '0'}){Colors.RESET}")

                    if include_dates:
                        print(_format_date_line(approx_updated, approx_source, board_date_stats, board_name))

                    # In debug mode, show why it was filtered
                    if debug and not meets_filters:
                        for reason in filter_reasons:
                            print(f"  {Colors.YELLOW}⚠️  Filtered out: {reason}{Colors.RESET}")

                # Only add to results if it meets filters
                if meets_filters:
                    results.append((board_name, has_downloads, sounds_info, sound_count, board_desc, category, views, tags, views_int, approx_updated, approx_source))

                    # Count downloadable boards that meet filters
                    if has_downloads and meets_filters:
                        downloadable_count += 1
                else:
                    # Count skipped downloadable boards (didn't meet filters)
                    if has_downloads:
                        skipped_count += 1

                # Add delay between board checks to be respectful to server
                time.sleep(REQUEST_DELAY)

            except (HTTPError, URLError, OSError, ValueError) as e:
                boards_fetch_errors += 1
                _progress_clear()
                print(f"  {Colors.RED}Error: {e}{Colors.RESET}")
                if logger:
                    logger.event("board_analyze_error", board=board_name, error=str(e))
                # Don't increment downloadable_count on error
                # Move cursor up to hide the "Analyzing" line if not in debug mode
                if (not debug) and (not verbose) and sys.stdout.isatty():
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
    results = [r for r in results if r[1]]  # Filter by has_downloads

    # Sort results
    if sort_by == "recent":
        min_date = datetime.min.replace(tzinfo=timezone.utc)
        results.sort(key=lambda x: x[9] or min_date, reverse=True)
    else:
        # Sort by views (highest first) - views_int is at index 8
        results.sort(key=lambda x: x[8], reverse=True)

    if not results:
        if skipped_count > 0:
            print(f"\n{Colors.YELLOW}⚠️  No boards matched your filter criteria.{Colors.RESET}")
            print(f"   {skipped_count} downloadable board(s) were skipped due to filters.")
            print(f"   Diagnostics: analyzed {boards_analyzed_total} board(s) across up to {page} page(s); fetch errors: {boards_fetch_errors}.")
            if include_dates or recent_threshold is not None or sort_by == "recent":
                print(
                    f"   Track-date coverage (downloadable boards): {boards_with_track_date_total} with dates, {boards_with_unknown_date_total} unknown."
                )
                if track_headers_total_total:
                    print(
                        f"   Track headers overall: {track_headers_ok_total}/{track_headers_total_total} OK."
                    )
            breakdown = _format_skip_breakdown(skipped_buckets)
            if breakdown:
                print(breakdown)

            # If the date filter eliminated everything, suggest a more realistic --recent-days.
            if recent_threshold is not None:
                if recent_near_misses_too_old:
                    now_utc = datetime.now(timezone.utc)
                    recent_near_misses_too_old.sort(key=lambda x: x[0], reverse=True)
                    top = recent_near_misses_too_old[:3]
                    needed_days = []

                    print(f"\n{Colors.GRAY}💡 Newest boards outside your {recent_days}-day window:{Colors.RESET}")
                    for dt, board_name in top:
                        age_days = int(math.ceil((now_utc - dt).total_seconds() / 86400.0))
                        needed_days.append(age_days)
                        print(
                            f"   - {Colors.CYAN}{board_name}{Colors.RESET}: {_format_date(dt)} (~{age_days} days ago)"
                        )

                    if needed_days:
                        suggested_days = max(needed_days)
                        start_date = (now_utc - timedelta(days=suggested_days)).date().isoformat()
                        print(
                            f"   Try {Colors.YELLOW}--recent-days {suggested_days}{Colors.RESET} "
                            f"(window starts ~{start_date}) to include these."
                        )

                if recent_near_misses_unknown:
                    # These can never satisfy a strict recent-days filter because updated date can't be inferred.
                    print(
                        f"\n{Colors.GRAY}ℹ️  Also found {len(recent_near_misses_unknown)} downloadable board(s) with unknown updated dates; "
                        f"they can't pass a strict {Colors.YELLOW}--recent-days{Colors.RESET} filter.{Colors.RESET}"
                    )
                    print(f"   Tip: Remove --recent-days to include them, or increase --date-sample-size for more thorough date inference.")

            print("   Try increasing --max, adjusting --min-views/--min-sounds, relaxing --recent-days, or use --debug to see why boards were filtered.")
        else:
            print(f"\n{Colors.YELLOW}⚠️  No downloadable boards found.{Colors.RESET}")
            print("   Try a different search, adjust filters with --min-views 0 --min-sounds 0, or use --debug to see all analyzed boards.")
            print(f"   Diagnostics: analyzed {boards_analyzed_total} board(s) across up to {page} page(s); fetch errors: {boards_fetch_errors}.")
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

    print(f"\n{Colors.BOLD}{'='*80}")
    print(f"{'SEARCH RESULTS':^80}")
    print(f"{'='*80}{Colors.RESET}\n")

    for board_name, has_downloads, sounds_info, total_count, board_desc, category, views, tags, views_int, approx_updated, approx_source in results:
        if has_downloads:
            status = f"{Colors.GREEN}✓ DOWNLOADABLE{Colors.RESET}"
        else:
            status = f"{Colors.RED}✗ PLAY-ONLY{Colors.RESET}"
        print(f"{Colors.BOLD}Board:{Colors.RESET} {Colors.CYAN}{board_name}{Colors.RESET} - {status} {Colors.GRAY}({total_count} sounds total){Colors.RESET}")
        print(f"{Colors.GRAY}URL: {BASE_URL}/sb/{_quote_path_segment(board_name)}{Colors.RESET}")

        if board_desc:
            print(f"{Colors.GRAY}Description: {board_desc}{Colors.RESET}")
        if category:
            print(f"{Colors.GRAY}Category: {category}{Colors.RESET}")
        if views:
            print(f"{Colors.GRAY}Views: {views}{Colors.RESET}")
        if include_dates:
            print(_format_date_line(approx_updated, approx_source, board_date_stats, board_name, indent=""))
        if tags:
            print(f"{Colors.GRAY}Tags: {', '.join(tags)}{Colors.RESET}")

        if sounds_info:
            print(f"\n{Colors.BOLD}Sample files (showing {len(sounds_info)} of {total_count}):{Colors.RESET}")
            for idx, (sound_id, title) in enumerate(sounds_info, 1):
                print(f"  {Colors.YELLOW}{idx:2}.{Colors.RESET} {title}")
        print("\n")  # Two newlines after each board

    print(f"{Colors.BOLD}{'='*80}{Colors.RESET}")

    # Show skipped boards summary if any were filtered out
    if skipped_count > 0:
        print(f"\n{Colors.YELLOW}ℹ️  {skipped_count} downloadable board(s) were skipped due to filter criteria.{Colors.RESET}")
        breakdown = _format_skip_breakdown(skipped_buckets)
        if breakdown:
            print(breakdown)
        if min_views > 0 or min_sounds > 0:
            print(f"   Adjust --min-views or --min-sounds to include them in results.")

    if results and results[0][1]:
        print(f"\n{Colors.BOLD}To download a board, use:{Colors.RESET}")
        print(f"  {Colors.GRAY}python3 soundboard-snag.py --board \"{results[0][0]}\"{Colors.RESET}")

    return results

def main():
    """Command-line interface."""
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

    try:
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

                # Download each board
                downloadable_boards = [r for r in results if r[1]]  # r[1] is has_downloads
                total_boards = len(downloadable_boards)

                print(f"\n{Colors.BOLD}{Colors.CYAN}Starting download of {total_boards} board(s)...{Colors.RESET}\n")

                successful = 0
                failed = 0

                for idx, (board_name, has_downloads, sounds_info, total_count, board_desc, category, views, tags, views_int, approx_updated, approx_source) in enumerate(downloadable_boards, 1):
                    print(f"\n{Colors.BOLD}{'='*80}")
                    print(f"Board {idx}/{total_boards}: {board_name}")
                    print(f"{'='*80}{Colors.RESET}\n")

                    try:
                        board_url = f"{BASE_URL}/sb/{_quote_path_segment(board_name)}"
                        snag_tool = SoundboardSnag(board_url, download_root=download_root)
                        success = snag_tool.snag()

                        if success:
                            successful += 1
                        else:
                            failed += 1

                    except (HTTPError, URLError, OSError, ValueError, RuntimeError) as e:
                        print(f"{Colors.RED}Error downloading {board_name}: {e}{Colors.RESET}")
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
            except SystemExit:
                raise
            except Exception as e:
                print(f"Search and download error: {e}")
                sys.exit(1)

        # Handle search mode
        if args.search:
            try:
                search_boards(
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
                )
                sys.exit(0)
            except KeyboardInterrupt:
                print("\n\nSearch cancelled by user.")
                sys.exit(1)
            except SystemExit:
                raise
            except Exception as e:
                print(f"Search error: {e}")
                sys.exit(1)

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
        print("\n\nCancelled by user.")
        sys.exit(1)
    except SystemExit:
        raise
    except Exception as e:
        print(f"Unexpected error: {type(e).__name__}: {e}")
        sys.exit(1)
    finally:
        if run_logger:
            run_logger.close()


if __name__ == "__main__":
    main()

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
"""

import argparse
import html
import os
import re
import sys
import time
from urllib.parse import urlparse, quote
from urllib.request import Request, urlopen
from urllib.error import HTTPError, URLError


# Module-level constants
BASE_URL = "https://www.soundboard.com"
USER_AGENT = 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36'
CHUNK_SIZE = 8192
REQUEST_DELAY = 0.5  # Delay between requests in seconds (be respectful to server)
WINDOWS_RESERVED_NAMES = {'CON', 'PRN', 'AUX', 'NUL'}
WINDOWS_RESERVED_NAMES.update({f'COM{i}' for i in range(1, 10)})
WINDOWS_RESERVED_NAMES.update({f'LPT{i}' for i in range(1, 10)})

# ANSI color codes (cross-platform compatible)
class Colors:
    """ANSI color codes for terminal output."""
    RESET = '\033[0m'
    BOLD = '\033[1m'

    # Foreground colors
    RED = '\033[91m'
    GREEN = '\033[92m'
    YELLOW = '\033[93m'
    BLUE = '\033[94m'
    MAGENTA = '\033[95m'
    CYAN = '\033[96m'
    WHITE = '\033[97m'
    GRAY = '\033[90m'

    # Background colors (optional)
    BG_RED = '\033[101m'
    BG_GREEN = '\033[102m'
    BG_YELLOW = '\033[103m'
    BG_BLUE = '\033[104m'


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
        self.board_name = self._extract_board_name()
        self.base_url = BASE_URL
        self.download_root = download_root if download_root else os.getcwd()

    def _extract_board_name(self):
        """Extract the board name from the URL path.

        Returns:
            The board name as a string.

        Raises:
            ValueError: If URL path is empty or board name cannot be
                determined.
        """
        if not self.url.path or self.url.path == "/":
            raise ValueError("Invalid URL: No soundboard path found. Expected format: https://www.soundboard.com/sb/boardname")

        name = self.url.path.replace("/sb/", "").replace("/", "").strip()
        if not name:
            raise ValueError("Could not extract board name from URL path")

        return name

    def _fetch_page(self):
        """Fetch the soundboard page content.

        Returns:
            The page HTML content as a UTF-8 decoded string.

        Raises:
            RuntimeError: If the page cannot be retrieved (HTTP errors, network
                errors, or non-200 status codes).
        """
        page_url = f"{self.base_url}/sb/{self.board_name}"

        # Create request with User-Agent to avoid being blocked
        req = Request(page_url, headers={'User-Agent': USER_AGENT})

        try:
            with urlopen(req) as response:
                if response.getcode() != 200:
                    raise RuntimeError(f"Unable to retrieve soundboard page. HTTP status: {response.getcode()}")

                # Decode bytes to string
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

        # Title case if all lowercase or all uppercase (improves readability)
        if name and (name.islower() or name.isupper()):
            name = name.title()

        # Handle Windows reserved names (CON, PRN, AUX, NUL, COM1-9, LPT1-9)
        name_upper = name.upper()
        if name_upper in WINDOWS_RESERVED_NAMES:
            name = f'_{name}'  # Prefix with underscore to make it safe

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

            with urlopen(req) as response:
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

                # Write file in chunks
                with open(filepath, 'wb') as f:
                    while True:
                        chunk = response.read(CHUNK_SIZE)
                        if not chunk:
                            break
                        f.write(chunk)

                file_size_kb = os.path.getsize(filepath) / 1024
                return True, (final_filename, file_size_kb)

        except HTTPError as e:
            return False, f"HTTP {e.code}: {e.reason}"
        except URLError as e:
            return False, f"Network error: {e.reason}"
        except Exception as e:
            return False, str(e)

    def snag(self):
        """Main snagging process."""
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
            board_url = f"{self.base_url}/sb/{self.board_name}"
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
        output_dir = os.path.join(self.download_root, self.board_name)
        print(f"   {Colors.GRAY}Download location: {os.path.abspath(output_dir)}{Colors.RESET}\n")

        # Download each sound
        snagged_count = 0
        existing_count = 0
        failed_count = 0
        consecutive_failures = 0
        max_consecutive_failures = 2  # Exit if this many failures in a row

        output_dir = os.path.join(self.download_root, self.board_name)

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

        return True



def search_boards(query, max_results=20, debug=False, min_views=0, min_sounds=0):
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
        min_sounds: Minimum number of sounds required (default set by CLI, 0 = no filter)    Returns:
        List of tuples: (board_name, has_downloads, sounds_list, total_count, board_desc, category, views, tags, views_int)
    """
    encoded_query = quote(query)

    # Display filter information to user
    filter_info = []
    if min_views > 0:
        filter_info.append(f"min {min_views} views")
    if min_sounds > 0:
        filter_info.append(f"min {min_sounds} sounds")

    filter_text = f" {Colors.GRAY}(filtering: {', '.join(filter_info)}){Colors.RESET}" if filter_info else ""
    print(f"{Colors.BOLD}{Colors.CYAN}Searching for: '{query}'...{filter_text}{Colors.RESET}")

    if filter_info:
        print(f"{Colors.GRAY}💡 Tip: Use --min-views 0 --min-sounds 0 to see all results{Colors.RESET}\n")

    results = []
    downloadable_count = 0
    skipped_count = 0  # Track boards that were downloadable but didn't meet filters
    target_downloadable = max_results

    # Fetch boards page by page, analyzing as we go
    seen = set()
    page = 1
    max_pages = 10  # Safety limit to prevent infinite loops
    keep_searching = True

    while keep_searching and page <= max_pages:
        search_url = f"{BASE_URL}/search/{encoded_query}?page={page}" if page > 1 else f"{BASE_URL}/search/{encoded_query}"

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
            with urlopen(req) as response:
                html_content = response.read().decode('utf-8')
        except (HTTPError, URLError) as e:
            print(f"{Colors.RED}Error searching page {page}: {e}{Colors.RESET}")
            break

        # Extract board names from this page
        board_pattern = r'/sb/([a-zA-Z0-9_-]+)'
        boards = re.findall(board_pattern, html_content)

        page_boards = []
        for board in boards:
            board = html.unescape(board)
            if board not in seen and board.lower() not in ['search', 'popular', 'new']:
                seen.add(board)
                page_boards.append(board)

        # If no new boards found on this page, we've reached the end
        if not page_boards:
            print(f"{Colors.YELLOW}No more boards found.{Colors.RESET}\n")
            break

        # Analyze boards from this page
        for board_name in page_boards:
            # If we have enough downloadable boards, we can stop
            if downloadable_count >= target_downloadable:
                keep_searching = False
                break

            try:
                board_url = f"{BASE_URL}/sb/{board_name}"
                req = Request(board_url, headers={'User-Agent': USER_AGENT})

                with urlopen(req) as response:
                    board_html = response.read().decode('utf-8')

                # Extract sound IDs and titles
                sound_pattern = r'data-src="(\d+)".*?<span>([^<]+)</span>'
                sound_matches = re.findall(sound_pattern, board_html, re.DOTALL)

                # Check if first sound has download button
                download_pattern = r'<a href="/sb/sound/\d+"[^>]*class="[^"]*btn-download-track'
                has_downloads = re.search(download_pattern, board_html) is not None

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
                views_int = 0
                if views:
                    try:
                        views_int = int(views.replace(',', ''))
                    except ValueError:
                        views_int = 0

                # Apply filters first
                meets_filters = True
                filter_reasons = []
                if min_views > 0 and views_int < min_views:
                    meets_filters = False
                    filter_reasons.append(f"views ({views_int}) < min_views ({min_views})")
                if min_sounds > 0 and sound_count < min_sounds:
                    meets_filters = False
                    filter_reasons.append(f"sounds ({sound_count}) < min_sounds ({min_sounds})")

                # Decide whether to show output (only downloadable boards that meet filters, or everything in debug mode)
                should_show = (has_downloads and meets_filters) or debug

                if should_show:
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

                    # In debug mode, show why it was filtered
                    if debug and not meets_filters:
                        for reason in filter_reasons:
                            print(f"  {Colors.YELLOW}⚠️  Filtered out: {reason}{Colors.RESET}")

                # Only add to results if it meets filters
                if meets_filters:
                    results.append((board_name, has_downloads, sounds_info, sound_count, board_desc, category, views, tags, views_int))

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
                print(f"  {Colors.RED}Error: {e}{Colors.RESET}")
                # Don't increment downloadable_count on error
                # Move cursor up to hide the "Analyzing" line if not in debug mode
                if not debug:
                    sys.stdout.write('\033[F\033[K')
                    sys.stdout.flush()
                continue

        # Move to next page if we haven't found enough boards yet
        if downloadable_count >= target_downloadable:
            break

        page += 1
        time.sleep(REQUEST_DELAY)  # Delay between pages

    # Filter results to only show downloadable boards (already done in meets_filters logic)
    results = [r for r in results if r[1]]  # Filter by has_downloads

    # Sort by views (highest first) - views_int is at index 8
    results.sort(key=lambda x: x[8], reverse=True)

    if not results:
        print(f"\n{Colors.YELLOW}⚠️  No downloadable boards found.{Colors.RESET}")
        print("   Try a different search, adjust filters with --min-views 0 --min-sounds 0, or use --debug to see all analyzed boards.")
        return results

    print(f"\n{Colors.BOLD}{'='*80}")
    print(f"{'SEARCH RESULTS':^80}")
    print(f"{'='*80}{Colors.RESET}\n")

    for board_name, has_downloads, sounds_info, total_count, board_desc, category, views, tags, views_int in results:
        if has_downloads:
            status = f"{Colors.GREEN}✓ DOWNLOADABLE{Colors.RESET}"
        else:
            status = f"{Colors.RED}✗ PLAY-ONLY{Colors.RESET}"
        print(f"{Colors.BOLD}Board:{Colors.RESET} {Colors.CYAN}{board_name}{Colors.RESET} - {status} {Colors.GRAY}({total_count} sounds total){Colors.RESET}")
        print(f"{Colors.GRAY}URL: {BASE_URL}/sb/{board_name}{Colors.RESET}")

        if board_desc:
            print(f"{Colors.GRAY}Description: {board_desc}{Colors.RESET}")
        if category:
            print(f"{Colors.GRAY}Category: {category}{Colors.RESET}")
        if views:
            print(f"{Colors.GRAY}Views: {views}{Colors.RESET}")
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
        if min_views > 0 or min_sounds > 0:
            print(f"   Adjust --min-views or --min-sounds to include them in results.")

    if results and results[0][1]:
        print(f"\n{Colors.BOLD}To download a board, use:{Colors.RESET}")
        print(f"  {Colors.GRAY}python3 soundboard-snag.py --board {results[0][0]}{Colors.RESET}")

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
        "-d", "--download-root",
        type=str,
        default=None,
        help="Root directory for downloads (default: current working directory). "
             "A subfolder with the board name will be created inside this directory."
    )


    args = parser.parse_args()

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

    # Handle search-and-download mode
    if args.search_and_download:
        try:
            results = search_boards(args.search_and_download, args.max, args.debug, args.min_views, args.min_sounds)

            if not results:
                print(f"\n{Colors.YELLOW}No boards to download.{Colors.RESET}")
                sys.exit(0)

            # Download each board
            downloadable_boards = [r for r in results if r[1]]  # r[1] is has_downloads
            total_boards = len(downloadable_boards)

            print(f"\n{Colors.BOLD}{Colors.CYAN}Starting download of {total_boards} board(s)...{Colors.RESET}\n")

            successful = 0
            failed = 0

            for idx, (board_name, has_downloads, sounds_info, total_count, board_desc, category, views, tags, views_int) in enumerate(downloadable_boards, 1):
                print(f"\n{Colors.BOLD}{'='*80}")
                print(f"Board {idx}/{total_boards}: {board_name}")
                print(f"{'='*80}{Colors.RESET}\n")

                try:
                    board_url = f"{BASE_URL}/sb/{board_name}"
                    snag_tool = SoundboardSnag(board_url, download_root=download_root)
                    success = snag_tool.snag()

                    if success:
                        successful += 1
                    else:
                        failed += 1

                except Exception as e:
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
        except Exception as e:
            print(f"Search and download error: {e}")
            sys.exit(1)

    # Handle search mode
    if args.search:
        try:
            search_boards(args.search, args.max, args.debug, args.min_views, args.min_sounds)
            sys.exit(0)
        except KeyboardInterrupt:
            print("\n\nSearch cancelled by user.")
            sys.exit(1)
        except Exception as e:
            print(f"Search error: {e}")
            sys.exit(1)

    # Get URL from command line (board name or full URL) or interactive input
    if args.board:
        # User provided board name - construct URL
        soundboard_url = f"{BASE_URL}/sb/{args.board}"
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
            soundboard_url = input("URL: ").strip()
        except EOFError:
            print("\nError: No input received.")
            print("For debugging, add arguments to launch configuration:")
            print('  "args": ["--url", "https://www.soundboard.com/sb/starwars"]')
            sys.exit(1)

    # Run snag tool
    try:
        snag_tool = SoundboardSnag(soundboard_url, download_root=download_root)
        success = snag_tool.snag()

        if not success:
            sys.exit(1)

        # Interactive mode: wait for keypress
        if not args.url:
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


if __name__ == "__main__":
    main()

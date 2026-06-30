# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

A single-file CLI tool that downloads audio from soundboard.com with cleaned-up filenames. Pure Python standard library, **zero third-party dependencies**, targets Python 3.6+. All real logic lives in `soundboard-snag.py` (~1650 lines). `debug_track_dates.py` is a standalone diagnostic.

## Commands

There is no build system, test suite, or linter configured.

```bash
# Syntax check (the de-facto "build" — used in .vscode/settings.json auto-approve)
python3 -m py_compile soundboard-snag.py

# Run it
python3 soundboard-snag.py --search "star wars"
python3 soundboard-snag.py --board starwars
python3 soundboard-snag.py --search-and-download "hockey" --max 5 -d ~/Sounds

# Inspect track ordering vs HTTP Last-Modified for one board
python3 debug_track_dates.py <board-slug> --n 5
```

Verify changes by running the script against a real board; there are no unit tests to run.

## Architecture

Everything hangs off `main()` (argparse) which dispatches into one of four modes, checked in order: `--search-and-download`, `--search`, `--board`/`--url` download, or interactive prompt. There are two independent engines:

**`SoundboardSnag` class — single-board download pipeline.**
`__init__(url)` parses the board slug/name from the URL, then `snag()` orchestrates:
`_fetch_page` → `_check_downloads_enabled` (detects play-only boards before downloading) → `_parse_sound_items` (scrape sound IDs/titles from HTML) → loop `_snag_sound` per sound → `_extract_filename_from_headers` + `_sanitize_filename`. Files land in `<download_root>/<board-dirname>/`. `snag()` aborts early after 2 consecutive download failures.

**`search_boards()` — the big standalone function** (~720 lines, the most complex code here). Scrapes the search results page, fetches each candidate board, applies quality filters (`--min-views`, `--min-sounds`), and optionally infers approximate update dates. It **returns a list of fixed-shape 11-tuples**:
`(board_name, has_downloads, sounds_info, total_count, board_desc, category, views, tags, views_int, approx_updated, approx_source)`.
`main()` unpacks this positionally (search around line 1533) — **changing the tuple order/length breaks the caller silently**.

### Cross-cutting details that matter

- **Date inference is best-effort and opt-in.** Approximate "updated" dates come from HTTP `Last-Modified` headers on track download URLs, not real metadata. Gated behind the `include_dates` flag, which `main()` sets true if any of `--include-dates`, `--recent-days`, or `--sort recent` is present. `--date-sample-size` caps how many track headers get fetched per board (0 = scan all = most accurate, most requests). Core helpers: `_fetch_last_modified_detailed`, `_parse_http_datetime`.
- **soundboard.com URL conventions.** Board page: `/sb/<slug>`. Track download: `/track/download/<id>`. Use `_quote_path_segment` for slugs — it keeps `%` safe to avoid double-encoding already-percent-encoded names.
- **Network etiquette is intentional.** Spoofed `USER_AGENT`, `REQUEST_DELAY` (0.5s) between board requests, `HEADER_REQUEST_DELAY` (0.05s) between header probes, separate `HTTP_TIMEOUT` vs `DOWNLOAD_TIMEOUT`. Keep these when adding network calls.
- **Filename sanitization** (`_sanitize_filename`) decodes HTML entities, strips UUID patterns, title-cases all-upper/all-lower names, and rewrites `WINDOWS_RESERVED_NAMES` (CON, PRN, COM1…) for cross-platform safety.
- **Output channels:** `Colors` (ANSI), and `JsonlLogger` (one JSON object per line) wired via `--log-file`, threaded through `search_boards` as the `logger` arg.

When editing the scrapers, remember all parsing is regex over raw HTML — it's tightly coupled to soundboard.com's current markup and will break if the site changes.

# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

A macOS desktop app for finding soundboard.com boards and saving their sounds with clean filenames. Python: `soundboard-snag.py` holds the engine, a small local web server and the window shell; `web/` is the UI (vanilla JS/CSS, no build step) styled to look native: system font stack, system color keywords (`CanvasText`, `AccentColor`), native form controls, light/dark via `prefers-color-scheme`; no web fonts or frameworks. The app window loads it with `?app=1` so the page goes transparent over pywebview's vibrancy material. `assets/` holds the generated app icon. The window is pywebview (`requirements.txt`); everything else, including the unit tests, uses only the standard library.

## Commands

```bash
python3 -m py_compile soundboard-snag.py            # syntax check
python3 -m unittest test_soundboard_snag             # offline unit tests
python3 soundboard-snag.py --serve --port 8765       # headless dev server: open the UI in any browser
.venv/bin/python soundboard-snag.py                  # the real app (needs requirements.txt installed)
./scripts/build-macos-app.sh                         # build "Soundboard Snag.app"
```

There is no CLI any more. `--serve` exists only so the UI and search can be tested in a browser pane; it has no download windows. Verify downloads in the real app.

## Architecture

`main()` runs `run_desktop()` (default) or `run_server()` (`--serve`).

- **`run_desktop`**: starts `run_server` on an ephemeral loopback port in a thread, opens a pywebview window on it, and wires a `DownloadCoordinator` whose `open_window(token, url)` opens a second pywebview window for one sound's download page (`close_window(token)` closes it). `_install_download_hook` subclasses pywebview's macOS `BrowserDelegate` (must run before the first window is created, and checks the method it overrides exists) so an audio/attachment response whose URL belongs to a live job becomes a real `WKDownload`; its delegate asks the coordinator where to save. Not private mode: the site's "verified" state persists in `~/Library/Application Support/Soundboard Snag/webview`. `requirements.txt` pins pywebview below 7 because the hook relies on its internals.
- **`DownloadCoordinator`**: pure bookkeeping per sound (`waiting → saving → saved | exists | cancelled | failed | expired`). Picks `<download_root>/<board>/<clean name>.<ext>`; a name clash is saved as `<name> (<sound id>)` and dropped again if byte-identical (so same-titled sounds aren't lost and repeats aren't duplicated). Never overwrites, removes partial files on failure, closes the window when a job ends, prunes old jobs. Unit-tested without any window.
- **`run_server`**: `GET /api/search` (SSE), `GET /api/board/<board>`, `POST /api/fetch-sound`, `GET /api/fetch-sound/<token>` (its saved result includes the file `path`), `POST /api/file` ({path, action: open|reveal}; only files inside the download folder; runs `open` / `open -R`), static files from `web/`. State-changing POSTs check `Origin` and loopback peer. All search and board fetches go through `cached_fetch`: pages are cached 10 minutes (so server tests need unique queries), a 403/429/5xx from the site starts a 60 s cooldown with no requests for any feature (cached pages still served), and `cached_fetch.was_cached()` lets `search_boards` skip its politeness pause. `mark_saved` adds `saved` to each sound already present in the board's folder. Tests that trigger a cooldown must use their own server (`ServerBackoffAndSavedTests`). The app serves on port 47865 (`APP_PORT`, fallback any free port) so the page's origin, and its remembered filters in `localStorage`, are stable between launches.
- **`search_boards()`**: scrapes the search page, fetches each candidate board, applies filters (`_evaluate_filters`), optionally infers dates. Returns a list of `BoardResult` named tuples (`board_result_to_dict` serialises for the UI). Progress and diagnostics go out only through the `logger` event sink.
- **`SoundboardSnag`**: parses a board URL; `_fetch_page`, `_check_downloads_enabled`, `_parse_sound_items`, `_sanitize_filename`, `_board_output_dirname`. It no longer downloads anything.

## Cross-cutting details that matter

- **Downloads are gated by a human check (seen 2026-11).** `GET /track/download/<id>` returns a Cloudflare Turnstile "Verify you are human" page; `HEAD` still returns audio headers, so date inference works. Never bypass the check and never make download requests from Python: only the app's own window requests them, driven by the user's click. Don't script loading download pages without a click.
- **Date inference is best-effort and opt-in** (`include_dates`, `recent_days`, or `sort_by="recent"`). Dates come from `Last-Modified` on track download URLs, not real metadata. Helpers: `_fetch_last_modified_detailed`, `_parse_http_datetime`.
- **soundboard.com URL conventions.** Board page `/sb/<slug>`, track `/track/download/<id>`, canonical host `soundboard.com` (`www.` redirects). Use `_quote_path_segment` for slugs; it keeps `%` safe.
- **Network etiquette is intentional.** Spoofed `USER_AGENT`, `REQUEST_DELAY` (0.5s) between board requests, `HEADER_REQUEST_DELAY` (0.5s) between header probes, `HTTP_TIMEOUT`. Keep these when adding network calls. `search_boards` backs off: it stops on HTTP 403/429/5xx (`_site_says_stop`), checks at most `max(30, 3 × max_results)` boards, and makes at most `MAX_DATE_PROBES_PER_BOARD` (10) header probes per board, none after one is refused.
- **Filename sanitization** (`_sanitize_filename`) decodes HTML entities, strips UUID patterns, title-cases all-upper/all-lower names, and rewrites `WINDOWS_RESERVED_NAMES` (CON, PRN, COM1…).

All parsing is regex over raw HTML and tightly coupled to soundboard.com's current markup; it breaks if the site changes.

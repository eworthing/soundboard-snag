# Soundboard Snag

A macOS app for finding [soundboard.com](https://soundboard.com) boards and saving their sounds with clean filenames.

- **Search** boards with quality filters (minimum views and sounds), sorted by popularity or approximate update date.
- **See the sounds** of each board right in the results; sounds you already saved are marked, and your filters are remembered between launches (⌘F jumps to the search field).
- **Click a sound to save it** to `~/Downloads/Soundboard Snag/<board>/` under a tidy name.

## How downloading works

soundboard.com puts a "Verify you are human" check in front of every download. This app does **not** bypass it. Clicking a sound opens a small window with the site's own download page; if the site asks, you pass the check there, and the file is saved and the window closes by itself. The site remembers a passed check for a short while, so consecutive sounds are often one click each. The next unsaved sound is highlighted after each save, and **Get next sound** on a board saves it in one press.

Search, board listings and approximate dates make ordinary page requests, paced and capped to stay polite (see `AGENTS.md`).

## Requirements

- macOS 11.3 or later
- Python 3.9 or later

## Install and run

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
.venv/bin/python soundboard-snag.py
```

Or build a double-clickable app:

```bash
./scripts/build-macos-app.sh
open "Soundboard Snag.app"
```

Re-run the build script after moving the folder or upgrading Python. The window uses the system's own fonts, colors, controls and translucent material, follows light and dark mode, and the File menu has **Open Downloads Folder**. The icon is `assets/AppIcon.icns`, drawn by `assets/make_icon.py` with AppKit (re-run it with `.venv/bin/python assets/make_icon.py` after changing the design).

### Options

| Flag | Meaning |
|---|---|
| `-d, --download-root DIR` | Where sounds are saved (default `~/Downloads/Soundboard Snag`) |
| `--serve` | Developer mode: serve the web UI headlessly on `--host`/`--port` (default `127.0.0.1:8765`) to open in any browser. Search works; downloads need the app window. |

## Tests

The unit tests are offline and need only the standard library:

```bash
python3 -m unittest test_soundboard_snag
```

## Limitations

- Only boards whose owners enabled download buttons can be saved; play-only boards are skipped.
- Dates are approximate: inferred from file `Last-Modified` headers on a few tracks per board.
- The site can change its markup or protections at any time; the parsing is regular expressions over its HTML.

## License

MIT License, see [LICENSE](LICENSE).

## Disclaimer

For personal use. Respect copyright and soundboard.com's terms of service, and only save content you have the right to access.

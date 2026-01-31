#!/usr/bin/env python3
"""Quick helper to inspect Soundboard track ordering vs HTTP Last-Modified.

This script fetches a board page, takes the first/last N `data-src` IDs as they
appear in HTML, then fetches `Last-Modified` from each track download URL.

Usage:
  python3 debug_track_dates.py bpsports
  python3 debug_track_dates.py bpsports --n 5
"""

import argparse
import re
from urllib.parse import quote
from urllib.request import Request, urlopen
from urllib.error import HTTPError, URLError

BASE_URL = "https://www.soundboard.com"
USER_AGENT = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36"
TIMEOUT = 10


def _fetch(url: str) -> str:
    req = Request(url, headers={"User-Agent": USER_AGENT})
    with urlopen(req, timeout=TIMEOUT) as resp:
        return resp.read().decode("utf-8", "replace")


def _last_modified_for_track(track_id: str):
    url = f"{BASE_URL}/track/download/{track_id}"

    # Prefer HEAD when allowed.
    try:
        req = Request(url, headers={"User-Agent": USER_AGENT}, method="HEAD")
        with urlopen(req, timeout=TIMEOUT) as resp:
            return ("HEAD", resp.status, resp.headers.get("Last-Modified"))
    except Exception:
        pass

    # Fallback to a 1-byte range request.
    try:
        req = Request(url, headers={"User-Agent": USER_AGENT, "Range": "bytes=0-0"})
        with urlopen(req, timeout=TIMEOUT) as resp:
            return ("RANGE", resp.status, resp.headers.get("Last-Modified"))
    except (HTTPError, URLError) as e:
        return ("ERR", getattr(e, "code", type(e).__name__), None)
    except Exception as e:
        return ("ERR", type(e).__name__, None)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("board")
    ap.add_argument("--n", type=int, default=5)
    args = ap.parse_args()

    board = args.board
    n = max(1, args.n)

    board_url = f"{BASE_URL}/sb/{quote(board, safe='%-_.~')}"
    html = _fetch(board_url)
    ids = re.findall(r'data-src="(\d+)"', html)

    print(f"Board: {board} ({board_url})")
    print(f"Tracks on page (data-src): {len(ids)}")
    if not ids:
        return 0

    sample = ids[:n] + ids[-n:]
    seen = set()
    sample = [x for x in sample if not (x in seen or seen.add(x))]

    print(f"\nSample IDs in page order (first/last {n}):")
    print("  " + ", ".join(sample))

    print("\nLast-Modified headers:")
    for track_id in sample:
        method, status, lm = _last_modified_for_track(track_id)
        print(f"  {track_id}: {method} {status}  Last-Modified={lm}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())

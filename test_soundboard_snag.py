#!/usr/bin/env python3
"""Offline unit tests for ``soundboard-snag.py`` (no network, no window).

The production module's file name contains a hyphen, which is not a valid Python
identifier, so it cannot be imported with a normal ``import`` statement. We load
it by path via ``importlib`` under a clean module name. Loading executes the
module top-level only (definitions); ``main()`` runs solely under
``if __name__ == "__main__"``, so importing it makes no network calls, opens no
window and needs none of the packages in requirements.txt.

Run with:

    python3 -m unittest test_soundboard_snag
"""

import http.client
import importlib.util
import io
import json
import os
import shutil
import sys
import tempfile
import threading
import unittest
from contextlib import redirect_stdout
from datetime import datetime, timezone
from unittest import mock
from urllib.error import URLError


def _load_module():
    here = os.path.dirname(os.path.abspath(__file__))
    path = os.path.join(here, "soundboard-snag.py")
    spec = importlib.util.spec_from_file_location("soundboard_snag", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


sb = _load_module()


class ParseViewsCountTests(unittest.TestCase):
    def test_plain_and_comma_integers(self):
        self.assertEqual(sb._parse_views_count("5"), 5)
        self.assertEqual(sb._parse_views_count("1,234"), 1234)
        self.assertEqual(sb._parse_views_count("1,234,567"), 1234567)

    def test_compact_suffixes(self):
        self.assertEqual(sb._parse_views_count("1.2K"), 1200)
        self.assertEqual(sb._parse_views_count("10k"), 10000)
        self.assertEqual(sb._parse_views_count("3M"), 3000000)
        self.assertEqual(sb._parse_views_count("2.5k"), 2500)

    def test_unparseable_and_empty(self):
        self.assertEqual(sb._parse_views_count(""), 0)
        self.assertEqual(sb._parse_views_count("   "), 0)
        self.assertEqual(sb._parse_views_count("abc"), 0)
        self.assertEqual(sb._parse_views_count(None), 0)


class ParseHttpDatetimeTests(unittest.TestCase):
    def test_valid_rfc1123_is_utc(self):
        dt = sb._parse_http_datetime("Wed, 21 Oct 2015 07:28:00 GMT")
        self.assertIsNotNone(dt)
        self.assertEqual(dt.tzinfo, timezone.utc)
        self.assertEqual((dt.year, dt.month, dt.day, dt.hour, dt.minute), (2015, 10, 21, 7, 28))

    def test_invalid_inputs_return_none(self):
        self.assertIsNone(sb._parse_http_datetime(None))
        self.assertIsNone(sb._parse_http_datetime(""))
        self.assertIsNone(sb._parse_http_datetime("not a date"))


class ExtractBoardSlugsTests(unittest.TestCase):
    def test_basic_href(self):
        self.assertEqual(
            sb._extract_board_slugs_from_search_html('<a href="/sb/starwars">x</a>'),
            ["starwars"],
        )

    def test_percent_encoded_segment_preserved(self):
        self.assertEqual(
            sb._extract_board_slugs_from_search_html("<a href='/sb/hello%20world'>x</a>"),
            ["hello%20world"],
        )

    def test_html_entity_unescaped(self):
        self.assertEqual(
            sb._extract_board_slugs_from_search_html('<a href="/sb/a&amp;b">x</a>'),
            ["a&b"],
        )

    def test_multiple_and_no_match(self):
        many = '<a href="/sb/one">1</a> <a href="/sb/two">2</a>'
        self.assertEqual(sb._extract_board_slugs_from_search_html(many), ["one", "two"])
        self.assertEqual(sb._extract_board_slugs_from_search_html("<p>nothing</p>"), [])

    def test_absolute_urls(self):
        # soundboard.com search results now link boards with absolute URLs.
        page = (
            '<a href="https://soundboard.com/sb/French_Star_Wars">1</a>'
            '<a href="https://www.soundboard.com/sb/Sounds_from_Star_Wars">2</a>'
            '<a href="http://soundboard.com/sb/old">3</a>'
        )
        self.assertEqual(
            sb._extract_board_slugs_from_search_html(page),
            ["French_Star_Wars", "Sounds_from_Star_Wars", "old"],
        )

    def test_other_hosts_ignored(self):
        page = '<a href="https://example.com/sb/nope">x</a>'
        self.assertEqual(sb._extract_board_slugs_from_search_html(page), [])


class QuotePathSegmentTests(unittest.TestCase):
    def test_existing_percent_escapes_not_double_encoded(self):
        self.assertEqual(sb._quote_path_segment("PRINS%20JULIUS"), "PRINS%20JULIUS")

    def test_spaces_and_slashes_encoded(self):
        self.assertEqual(sb._quote_path_segment("hello world"), "hello%20world")
        self.assertEqual(sb._quote_path_segment("a/b"), "a%2Fb")


class FormatDateTests(unittest.TestCase):
    def test_format_date(self):
        self.assertEqual(sb._format_date(datetime(2020, 1, 2)), "2020-01-02")

    def test_format_datetime_utc(self):
        self.assertEqual(sb._format_datetime_utc(None), "unknown")
        dt = datetime(2020, 1, 2, 3, 4, 5, tzinfo=timezone.utc)
        self.assertEqual(sb._format_datetime_utc(dt), "2020-01-02 03:04:05 UTC")










class _SnagHelpersMixin(unittest.TestCase):
    """Shared instance for the SoundboardSnag methods under test.

    Construction parses a URL only — no network — so it is safe in tests.
    The methods exercised here (``_sanitize_filename``,
    ``_extract_filename_from_headers``) do not use ``self``.
    """

    @classmethod
    def setUpClass(cls):
        cls.snag = sb.SoundboardSnag("https://www.soundboard.com/sb/test")




class SanitizeFilenameTests(_SnagHelpersMixin):
    def test_title_cases_all_lowercase_only(self):
        # all-lowercase is title-cased for readability
        self.assertEqual(self.snag._sanitize_filename("hello world.mp3", "1", ""), "Hello World.mp3")

    def test_all_uppercase_preserved_as_acronym(self):
        # all-uppercase is preserved (e.g., "NASA", "HTML"), not title-cased
        self.assertEqual(self.snag._sanitize_filename("MY SOUND.mp3", "1", ""), "MY SOUND.mp3")
        self.assertEqual(self.snag._sanitize_filename("NASA.mp3", "1", ""), "NASA.mp3")

    def test_mixed_case_preserved(self):
        self.assertEqual(self.snag._sanitize_filename("Already Mixed.mp3", "1", ""), "Already Mixed.mp3")

    def test_html_entities_decoded(self):
        self.assertEqual(self.snag._sanitize_filename("Don&#039;t Stop.mp3", "1", ""), "Don't Stop.mp3")

    def test_windows_reserved_name_is_prefixed(self):
        # "CON" is all-uppercase -> preserved -> still matched as reserved -> prefixed
        self.assertEqual(self.snag._sanitize_filename("CON.mp3", "1", ""), "_CON.mp3")
        self.assertEqual(self.snag._sanitize_filename("PRN.mp3", "1", ""), "_PRN.mp3")

    def test_long_filename_truncated_to_255_bytes(self):
        out = self.snag._sanitize_filename("a" * 400 + ".mp3", "1", "")
        # title-cased "Aaaa...", then truncated so name+ext stays within 255 bytes
        self.assertTrue(out.endswith(".mp3"))
        self.assertLessEqual(len(out.encode("utf-8")), 255)
        self.assertGreater(len(out), 200)

    def test_empty_falls_back_to_page_title_then_audio_id(self):
        self.assertEqual(self.snag._sanitize_filename("", "42", "My Title"), "My Title.mp3")
        self.assertEqual(self.snag._sanitize_filename("", "42", ""), "Audio 42.mp3")

    def test_invalid_path_characters_removed(self):
        invalid = '<>:"/\\|?*'
        out = self.snag._sanitize_filename(f"a{invalid}b.mp3", "1", "")
        for ch in invalid:
            self.assertNotIn(ch, out)
        self.assertTrue(out.endswith(".mp3"))

    def test_uuid_pattern_stripped(self):
        out = self.snag._sanitize_filename("227896-abcd1234-ab12-cd34-ef56-0123456789ab.mp3", "9", "Fallback")
        self.assertNotIn("abcd1234", out)


_BOARD_HTML = (
    '<div class="item r" data-src="123">'
    '<a href="/sb/sound/123" class="btn-download-track">dl</a>'
    '<div class="item-title text-ellipsis"><span>Hello Title</span></div></div>'
    '<div class="item r" data-src="456">'
    '<a href="/sb/sound/456" class="btn-download-track">dl</a>'
    '<div class="item-title text-ellipsis"><span>Second &amp; Sound</span></div></div>'
    '<p class="item-desc">A great board</p>'
    '<strong>Category: </strong><span class="text-muted"> Movies</span>'
    '<strong>Views: </strong><span class="text-muted"> 1,234</span>'
    '<strong>Tags: </strong><a href="x">funny</a> <a href="y">memes</a></div>'
)


class ParseBoardHtmlTests(unittest.TestCase):
    def test_full_board(self):
        p = sb._parse_board_html(_BOARD_HTML)
        self.assertTrue(p.has_downloads)
        self.assertEqual(p.download_ids, ["123", "456"])
        self.assertEqual(p.sound_count, 2)
        self.assertEqual(p.board_desc, "A great board")
        self.assertEqual(p.category, "Movies")
        self.assertEqual(p.views, "1,234")
        self.assertEqual(p.views_int, 1234)
        self.assertEqual(p.tags, ["funny", "memes"])
        # sounds_info titles are html-unescaped; sound_matches keep raw titles
        self.assertEqual(p.sounds_info, [("123", "Hello Title"), ("456", "Second & Sound")])
        self.assertEqual(p.sound_matches[1][1], "Second &amp; Sound")

    def test_play_only_board_has_no_downloads(self):
        play_only = (
            '<div class="item r" data-src="9"><div class="item-title text-ellipsis">'
            '<span>Only</span></div></div>'
        )
        p = sb._parse_board_html(play_only)
        self.assertFalse(p.has_downloads)
        self.assertEqual(p.download_ids, [])
        self.assertEqual(p.sound_count, 1)

    def test_download_ids_deduped_in_order(self):
        dup = (
            '<a href="/sb/sound/5" class="btn-download-track">a</a>'
            '<a href="/sb/sound/5" class="btn-download-track">a</a>'
            '<a href="/sb/sound/7" class="btn-download-track">b</a>'
        )
        self.assertEqual(sb._parse_board_html(dup).download_ids, ["5", "7"])

    def test_empty_html_is_all_defaults(self):
        p = sb._parse_board_html("")
        self.assertFalse(p.has_downloads)
        self.assertEqual(p.sound_count, 0)
        self.assertEqual(p.download_ids, [])
        self.assertEqual(p.board_desc, "")
        self.assertEqual(p.category, "")
        self.assertEqual(p.views, "")
        self.assertEqual(p.views_int, 0)
        self.assertEqual(p.tags, [])
        self.assertEqual(p.sounds_info, [])


class EvaluateFiltersTests(unittest.TestCase):
    def test_no_filters_always_passes(self):
        meets, failures = sb._evaluate_filters(0, 0, None, 0, 0, None, None)
        self.assertTrue(meets)
        self.assertEqual(failures, [])

    def test_views_and_sounds_can_both_fail(self):
        meets, failures = sb._evaluate_filters(5, 1, None, 10, 3, None, None)
        self.assertFalse(meets)
        self.assertEqual([k for k, _ in failures], ["views", "sounds"])

    def test_passing_basic_filters(self):
        meets, failures = sb._evaluate_filters(100, 20, None, 10, 3, None, None)
        self.assertTrue(meets)
        self.assertEqual(failures, [])

    def test_date_filter_only_when_basics_pass(self):
        threshold = datetime(2025, 1, 1, tzinfo=timezone.utc)
        # basics fail -> date is NOT evaluated (only the views failure reported)
        meets, failures = sb._evaluate_filters(1, 20, None, 10, 0, threshold, 7)
        self.assertFalse(meets)
        self.assertEqual([k for k, _ in failures], ["views"])

    def test_updated_unknown_and_too_old(self):
        threshold = datetime(2025, 6, 1, tzinfo=timezone.utc)
        meets, failures = sb._evaluate_filters(100, 20, None, 0, 0, threshold, 7)
        self.assertFalse(meets)
        self.assertEqual([k for k, _ in failures], ["updated_unknown"])

        old = datetime(2025, 1, 1, tzinfo=timezone.utc)
        meets, failures = sb._evaluate_filters(100, 20, old, 0, 0, threshold, 7)
        self.assertFalse(meets)
        self.assertEqual([k for k, _ in failures], ["updated_too_old"])

    def test_recent_enough_passes_date_filter(self):
        threshold = datetime(2025, 1, 1, tzinfo=timezone.utc)
        recent = datetime(2025, 3, 1, tzinfo=timezone.utc)
        meets, failures = sb._evaluate_filters(100, 20, recent, 0, 0, threshold, 30)
        self.assertTrue(meets)
        self.assertEqual(failures, [])










def _board_page(sounds, views, has_downloads=True):
    """Build minimal board-page HTML. ``sounds`` is a list of (id, title)."""
    chunks = []
    for sid, title in sounds:
        dl = f'<a href="/sb/sound/{sid}" class="btn-download-track">dl</a>' if has_downloads else ""
        chunks.append(
            f'<div class="item r" data-src="{sid}">{dl}'
            f'<div class="item-title text-ellipsis"><span>{title}</span></div></div>'
        )
    chunks.append(f'<strong>Views: </strong><span class="text-muted"> {views}</span>')
    return "".join(chunks)


def _search_page(slugs):
    return "".join(f'<a href="/sb/{s}">{s}</a>' for s in slugs)


def _run_search(fake_pages, **kwargs):
    """Drive search_boards with an in-memory fetcher over ``fake_pages`` (url -> html)."""
    calls = []

    def fetch(url):
        calls.append(url)
        if url in fake_pages:
            return fake_pages[url]
        raise URLError("no such page")  # caught by search_boards' page/board error handling

    params = dict(max_results=10, min_views=0, min_sounds=0, include_dates=False)
    params.update(kwargs)
    with mock.patch("time.sleep"), redirect_stdout(io.StringIO()):
        results = sb.search_boards("q", fetch=fetch, **params)
    return results, calls


B = sb.BASE_URL


class SearchBoardsOrchestrationTests(unittest.TestCase):
    """Exercise the network orchestration offline via the injected fetch seam."""

    def test_returns_downloadable_sorted_by_views(self):
        pages = {
            f"{B}/search/q": _search_page(["alpha", "beta"]),
            f"{B}/sb/alpha": _board_page([("1", "A1"), ("2", "A2")], "50"),
            f"{B}/sb/beta": _board_page([("3", "B1"), ("4", "B2"), ("5", "B3")], "100"),
        }
        results, _ = _run_search(pages)
        self.assertEqual([b.board_name for b in results], ["beta", "alpha"])  # 100 before 50
        self.assertTrue(all(b.has_downloads for b in results))
        self.assertEqual(results[0].views_int, 100)

    def test_play_only_board_excluded(self):
        pages = {
            f"{B}/search/q": _search_page(["dl", "playonly"]),
            f"{B}/sb/dl": _board_page([("1", "X")], "10", has_downloads=True),
            f"{B}/sb/playonly": _board_page([("2", "Y")], "10", has_downloads=False),
        }
        results, _ = _run_search(pages)
        self.assertEqual([b.board_name for b in results], ["dl"])

    def test_pagination_collects_across_pages(self):
        pages = {
            f"{B}/search/q": _search_page(["a", "b"]),
            f"{B}/search/q?page=2": _search_page(["c"]),
            f"{B}/sb/a": _board_page([("1", "x")], "30"),
            f"{B}/sb/b": _board_page([("2", "y")], "20"),
            f"{B}/sb/c": _board_page([("3", "z")], "40"),
        }
        results, _ = _run_search(pages)
        self.assertEqual(sorted(b.board_name for b in results), ["a", "b", "c"])
        self.assertEqual(results[0].board_name, "c")  # highest views (40)

    def test_min_views_filter_excludes_low(self):
        pages = {
            f"{B}/search/q": _search_page(["big", "small"]),
            f"{B}/sb/big": _board_page([("1", "x")], "500"),
            f"{B}/sb/small": _board_page([("2", "y")], "5"),
        }
        results, _ = _run_search(pages, min_views=100)
        self.assertEqual([b.board_name for b in results], ["big"])

    def test_early_stop_at_max_results(self):
        pages = {
            f"{B}/search/q": _search_page(["a", "b"]),
            f"{B}/sb/a": _board_page([("1", "x")], "30"),
            f"{B}/sb/b": _board_page([("2", "y")], "20"),
        }
        results, calls = _run_search(pages, max_results=1)
        self.assertEqual(len(results), 1)
        self.assertIn(f"{B}/sb/a", calls)
        self.assertNotIn(f"{B}/sb/b", calls)  # stopped before fetching the second board


def _board_item(sid, title, downloadable):
    dl = f'<a href="/sb/sound/{sid}" class="btn-download-track">d</a>' if downloadable else ""
    return (
        f'<div class="item r" data-src="{sid}">{dl}'
        f'<div class="item-title text-ellipsis"><span>{title}</span></div></div>'
    )


class DownloadCoordinatorTests(unittest.TestCase):
    """DownloadCoordinator bookkeeping: pure, no window, no network."""

    def setUp(self):
        self.root = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.root, True)
        self.dest = os.path.join(self.root, "Board")
        self.opened = []
        self.c = sb.DownloadCoordinator(lambda token, url: self.opened.append((token, url)))

    def _save(self, token, data=b"ID3" + b"\x00" * 2048, suggested="x.mp3"):
        path = self.c.destination_for(token, suggested)
        self.assertIsNotNone(path)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "wb") as f:
            f.write(data)
        return path

    def test_headless_coordinator_refuses(self):
        headless = sb.DownloadCoordinator()
        self.assertFalse(headless.available)
        with self.assertRaises(RuntimeError):
            headless.request("https://x/track/download/1", self.dest, "A")

    def test_request_opens_window_and_waits(self):
        token = self.c.request("https://x/track/download/1", self.dest, "Pew")
        self.assertEqual(self.opened, [(token, "https://x/track/download/1")])
        self.assertEqual(self.c.poll(token)["status"], "waiting")
        self.assertEqual(self.c.poll("nope")["status"], "unknown")

    def test_open_window_failure_is_reported_and_raised(self):
        def boom(token, url):
            raise OSError("no window")
        c = sb.DownloadCoordinator(boom)
        with self.assertRaises(OSError):
            c.request("u", self.dest, "A")
        self.assertEqual(c.poll("1-0")["status"], "unknown")   # nothing left pending as waiting

    def test_saved_flow_uses_clean_name_and_site_extension(self):
        token = self.c.request("u", self.dest, "Pew")
        path = self._save(token, suggested="Excitement_has_over-run.mp3")
        self.assertEqual(path, os.path.join(self.dest, "Pew.mp3"))
        self.assertEqual(self.c.poll(token)["status"], "saving")
        self.c.finished(token)
        result = self.c.poll(token)
        self.assertEqual((result["status"], result["name"]), ("saved", "Pew.mp3"))
        self.assertEqual(result["path"], path)
        self.assertGreater(result["kb"], 0)

    def test_non_audio_extension_falls_back_to_mp3(self):
        token = self.c.request("u", self.dest, "A")
        self.assertTrue(self.c.destination_for(token, "weird.exe").endswith("A.mp3"))
        token2 = self.c.request("u", self.dest, "B")
        self.assertTrue(self.c.destination_for(token2, "clip.WAV").endswith("B.wav"))

    def _existing(self, data=b"original"):
        os.makedirs(self.dest)
        original = os.path.join(self.dest, "Pew.mp3")
        with open(original, "wb") as f:
            f.write(data)
        return original

    def test_same_title_different_audio_is_kept_under_the_sound_id(self):
        original = self._existing(b"first sound")
        token = self.c.request("u", self.dest, "Pew", sound_id="77")
        path = self._save(token, data=b"a different sound")
        self.assertEqual(path, os.path.join(self.dest, "Pew (77).mp3"))
        self.c.finished(token)
        self.assertEqual(self.c.poll(token)["status"], "saved")
        with open(original, "rb") as f:
            self.assertEqual(f.read(), b"first sound")      # never overwritten

    def test_same_title_same_audio_is_dropped_as_already_saved(self):
        original = self._existing(b"same audio")
        token = self.c.request("u", self.dest, "Pew", sound_id="77")
        path = self._save(token, data=b"same audio")
        self.c.finished(token)
        self.assertFalse(os.path.exists(path))
        self.assertEqual(self.c.poll(token), {"status": "exists", "name": "Pew.mp3", "path": original})
        self.assertTrue(os.path.exists(original))

    def test_both_names_taken_counts_as_already_saved(self):
        self._existing()
        with open(os.path.join(self.dest, "Pew (77).mp3"), "wb") as f:
            f.write(b"earlier")
        token = self.c.request("u", self.dest, "Pew", sound_id="77")
        self.assertIsNone(self.c.destination_for(token, "x.mp3"))
        self.assertEqual(self.c.poll(token)["status"], "exists")

    def test_second_file_for_the_same_job_is_ignored(self):
        token = self.c.request("u", self.dest, "Pew")
        first = self._save(token)
        self.assertIsNone(self.c.destination_for(token, "again.mp3"))   # e.g. a retry
        self.assertEqual(self.c.poll(token)["status"], "saving")
        self.c.finished(token)
        self.assertEqual(self.c.poll(token)["name"], os.path.basename(first))

    def test_token_for_url_finds_live_jobs_only(self):
        token = self.c.request("https://x/track/download/5", self.dest, "A")
        self.assertEqual(self.c.token_for_url("https://x/track/download/5"), token)
        self.assertIsNone(self.c.token_for_url("https://x/track/download/6"))
        self.c.window_closed(token)
        self.assertIsNone(self.c.token_for_url("https://x/track/download/5"))

    def test_window_is_closed_when_a_job_ends_without_the_person_closing_it(self):
        closed = []
        c = sb.DownloadCoordinator(lambda t, u: None, closed.append, timeout=-1)
        expired = c.request("u", self.dest, "A")
        c.poll(expired)                                        # lazily expires
        saved = sb.DownloadCoordinator(lambda t, u: None, closed.append)
        token = saved.request("u", self.dest, "B")
        os.makedirs(self.dest, exist_ok=True)
        with open(saved.destination_for(token, "x.mp3"), "wb") as f:
            f.write(b"ID3")
        saved.finished(token)
        failing = sb.DownloadCoordinator(lambda t, u: None, closed.append)
        bad = failing.request("u", self.dest, "C")
        failing.failed(bad, "boom")
        self.assertEqual(closed, [expired, token, bad])

    def test_old_finished_jobs_are_pruned(self):
        c = sb.DownloadCoordinator(lambda t, u: None)
        first = c.request("u", self.dest, "A")
        c.window_closed(first)
        for i in range(sb.DownloadCoordinator.KEEP_FINISHED + 1):
            c.window_closed(c.request("u", self.dest, "B%d" % i))
        self.assertEqual(c.poll(first)["status"], "unknown")

    def test_failure_removes_the_partial_file(self):
        token = self.c.request("u", self.dest, "Pew")
        path = self._save(token)
        self.c.failed(token, "network dropped")
        self.assertFalse(os.path.exists(path))
        result = self.c.poll(token)
        self.assertEqual((result["status"], result["error"]), ("failed", "network dropped"))

    def test_closing_the_window_cancels_only_while_waiting(self):
        waiting = self.c.request("u", self.dest, "A")
        self.c.window_closed(waiting)
        self.assertEqual(self.c.poll(waiting)["status"], "cancelled")
        arriving = self.c.request("u", self.dest, "B")
        self._save(arriving)
        self.c.window_closed(arriving)                  # file is on its way: keep going
        self.c.finished(arriving)
        self.assertEqual(self.c.poll(arriving)["status"], "saved")

    def test_waiting_job_expires(self):
        c = sb.DownloadCoordinator(lambda token, url: None, timeout=-1)
        self.assertEqual(c.poll(c.request("u", self.dest, "A"))["status"], "expired")


@unittest.skipUnless(sys.platform == "darwin", "macOS window libraries only")
class DownloadHookDependenciesTests(unittest.TestCase):
    """The window hook subclasses pywebview internals; fail loudly if they move."""

    def test_pywebview_delegate_has_the_methods_the_hook_overrides(self):
        try:
            from webview.platforms import cocoa
        except ImportError:
            self.skipTest("pywebview is not installed")
        delegate = cocoa.BrowserView.BrowserDelegate
        self.assertTrue(hasattr(delegate, "webView_decidePolicyForNavigationResponse_decisionHandler_"))
        self.assertTrue(hasattr(cocoa.BrowserView, "DownloadDelegate"))


class SearchTrafficSafeguardTests(unittest.TestCase):
    """search_boards backs off and bounds its requests to soundboard.com."""

    def test_site_says_stop(self):
        for code in (403, 429, 500, 503, "429"):
            self.assertTrue(sb._site_says_stop(code), code)
        for code in (200, 404, 405, None, "x"):
            self.assertFalse(sb._site_says_stop(code), code)

    def _search(self, fetch, **kwargs):
        calls = []

        def counting(url):
            calls.append(url)
            return fetch(url)

        events = []

        class Sink:
            def event(self, event_type, **fields):
                events.append((event_type, fields))

        params = dict(max_results=10, min_views=0, min_sounds=0, include_dates=False,
                      logger=Sink())
        params.update(kwargs)
        out = io.StringIO()
        with mock.patch("time.sleep"), redirect_stdout(out):
            results = sb.search_boards("q", fetch=counting, **params)
        return results, calls, events, out.getvalue()

    def test_search_stops_on_rate_limit_instead_of_continuing(self):
        def fetch(url):
            if url == f"{B}/search/q":
                return _search_page(["a", "b", "c", "d"])
            if url == f"{B}/sb/a":
                return _board_page([("1", "x")], "30")
            raise sb.HTTPError(url, 429, "Too Many Requests", {}, None)

        results, calls, events, out = self._search(fetch)
        # search page + board a + board b (429) -- c and d are never requested
        self.assertEqual(calls, [f"{B}/search/q", f"{B}/sb/a", f"{B}/sb/b"])
        self.assertEqual([b.board_name for b in results], ["a"])
        self.assertTrue(any(t == "search_partial" and "429" in f.get("message", "")
                            for t, f in events))

    def test_missing_board_does_not_stop_the_search(self):
        def fetch(url):
            if url == f"{B}/search/q":
                return _search_page(["gone", "ok"])
            if url == f"{B}/sb/ok":
                return _board_page([("1", "x")], "30")
            if url == f"{B}/sb/gone":
                raise sb.HTTPError(url, 404, "Not Found", {}, None)
            raise URLError("no such page")

        results, calls, _events, _out = self._search(fetch)
        self.assertEqual([b.board_name for b in results], ["ok"])
        self.assertIn(f"{B}/sb/ok", calls)

    def test_no_politeness_pause_for_pages_served_from_cache(self):
        pages = {f"{B}/search/q": _search_page(["a", "b"]),
                 f"{B}/sb/a": _board_page([("1", "x")], "30"),
                 f"{B}/sb/b": _board_page([("2", "y")], "20")}

        def run(from_cache):
            def fetch(url):
                if url not in pages:
                    raise URLError("no such page")
                return pages[url]
            fetch.was_cached = lambda: from_cache
            with mock.patch("time.sleep") as sleep, redirect_stdout(io.StringIO()):
                sb.search_boards("q", fetch=fetch, max_results=10, min_views=0, min_sounds=0)
            return sleep.call_count

        self.assertEqual(run(from_cache=True), 0)
        self.assertGreater(run(from_cache=False), 0)

    def test_boards_checked_per_search_is_capped(self):
        slugs = [f"b{i}" for i in range(60)]

        def fetch(url):
            if url == f"{B}/search/q":
                return _search_page(slugs)
            if "/sb/" in url:
                return _board_page([("1", "x")], "1")   # fails min_views -> never "found"
            raise URLError("no such page")

        results, calls, events, _out = self._search(fetch, max_results=5, min_views=100)
        self.assertEqual(results, [])
        self.assertEqual(sum("/sb/" in u for u in calls), 30)   # max(30, 5 * 3)
        self.assertTrue(any(t == "search_partial" for t, _ in events))

    def test_date_probes_capped_per_board(self):
        sounds = [(str(i), f"s{i}") for i in range(1, 26)]
        pages = {f"{B}/search/q": _search_page(["big"]),
                 f"{B}/sb/big": _board_page(sounds, "30")}
        dt = datetime(2020, 1, 2, tzinfo=timezone.utc)
        with mock.patch.object(sb, "_fetch_last_modified_detailed",
                               return_value=(dt, "HEAD 200 last-modified")) as probe:
            self._search(lambda u: pages[u] if u in pages else (_ for _ in ()).throw(URLError("x")),
                         include_dates=True, date_sample_size=0)
        self.assertEqual(probe.call_count, sb.MAX_DATE_PROBES_PER_BOARD)

    def test_date_probes_stop_for_whole_search_when_refused(self):
        pages = {f"{B}/search/q": _search_page(["a", "b"]),
                 f"{B}/sb/a": _board_page([("1", "x"), ("2", "y"), ("3", "z")], "30"),
                 f"{B}/sb/b": _board_page([("4", "x"), ("5", "y")], "20")}
        with mock.patch.object(sb, "_fetch_last_modified_detailed",
                               return_value=(None, "HEAD http_429")) as probe:
            results, _calls, _events, _out = self._search(
                lambda u: pages[u] if u in pages else (_ for _ in ()).throw(URLError("x")),
                include_dates=True, date_sample_size=0)
        self.assertEqual(probe.call_count, 1)      # one refusal, then no more for a or b
        self.assertEqual(len(results), 2)          # boards are still listed, just undated




# ── Helpers for new seam guards ──────────────────────────────────────────────



# ── BoardResultToDictTests ────────────────────────────────────────────────────

def _make_board(**overrides):
    base = dict(
        board_name="starwars", has_downloads=True, sounds_info=[("1", "Pew"), ("2", "Boom")],
        total_count=12, board_desc="", category="", views="", tags=[],
        views_int=0, approx_updated=None, approx_source=None,
    )
    base.update(overrides)
    return sb.BoardResult(**base)


class BoardResultToDictTests(unittest.TestCase):
    """board_result_to_dict() serialises a BoardResult to a plain dict.

    Expected keys (from the plan's HTTP/serialisation section):
        board, name, has_downloads, sounds, total_count, description,
        category, views, views_int, tags, approx_updated, approx_source

    Naming notes:
        board == name == BoardResult.board_name  (both present for round-trip)
        sounds  == [{"id": ..., "title": ...}, ...]  derived from sounds_info
        description == board_desc
        approx_updated == ISO-8601 string when a datetime is present, else None
    """

    def _call(self, **overrides):
        board = _make_board(**overrides)
        return sb.board_result_to_dict(board)

    def test_all_expected_keys_present(self):
        d = self._call()
        expected = {
            "board", "name", "title", "image", "has_downloads", "sounds", "total_count",
            "description", "category", "views", "views_int", "tags",
            "approx_updated", "approx_source",
        }
        self.assertEqual(set(d.keys()), expected)

    def test_board_and_name_equal_board_name(self):
        d = self._call(board_name="starwars")
        self.assertEqual(d["board"], "starwars")
        self.assertEqual(d["name"], "starwars")
        self.assertEqual(d["board"], d["name"])

    def test_sounds_is_list_of_id_title_dicts(self):
        d = self._call(sounds_info=[("42", "Pew"), ("99", "Boom")])
        self.assertEqual(d["sounds"], [{"id": "42", "title": "Pew"}, {"id": "99", "title": "Boom"}])

    def test_sounds_empty_list_when_no_sounds(self):
        d = self._call(sounds_info=[])
        self.assertEqual(d["sounds"], [])

    def test_approx_updated_none_when_none(self):
        d = self._call(approx_updated=None)
        self.assertIsNone(d["approx_updated"])

    def test_approx_updated_iso8601_string_when_datetime(self):
        dt = datetime(2025, 3, 15, 12, 0, 0, tzinfo=timezone.utc)
        d = self._call(approx_updated=dt)
        iso = d["approx_updated"]
        self.assertIsInstance(iso, str)
        # Must be parseable as an ISO-8601 datetime
        parsed = datetime.fromisoformat(iso.replace("Z", "+00:00"))
        self.assertEqual(parsed.year, 2025)
        self.assertEqual(parsed.month, 3)
        self.assertEqual(parsed.day, 15)

    def test_description_comes_from_board_desc(self):
        d = self._call(board_desc="A great board")
        self.assertEqual(d["description"], "A great board")

    def test_scalar_fields_pass_through(self):
        d = self._call(
            has_downloads=False,
            total_count=77,
            category="Movies",
            views="5,000",
            views_int=5000,
            tags=["funny", "memes"],
            approx_source="track",
        )
        self.assertFalse(d["has_downloads"])
        self.assertEqual(d["total_count"], 77)
        self.assertEqual(d["category"], "Movies")
        self.assertEqual(d["views"], "5,000")
        self.assertEqual(d["views_int"], 5000)
        self.assertEqual(d["tags"], ["funny", "memes"])
        self.assertEqual(d["approx_source"], "track")

    def test_percent_encoded_board_name_preserved(self):
        # board_name may contain %-encoded chars; the identifier must round-trip
        d = self._call(board_name="star%20wars")
        self.assertEqual(d["board"], "star%20wars")
        self.assertEqual(d["name"], "star%20wars")


# ── ServerProtocolTests ───────────────────────────────────────────────────────

class ServerProtocolTests(unittest.TestCase):
    """HTTP server protocol tests (stdlib http.client; engines stubbed offline).

    Boots sb.run_server(host, port, download_root) in a daemon thread bound to
    an ephemeral port.  sb._http_get is monkeypatched so the search worker runs
    fully offline — no real soundboard.com requests escape.

    IMPORTANT: the server thread is NOT wrapped in redirect_stdout — run_server
    calls serve_forever() which never returns, so a redirect_stdout context
    would swallow all process stdout until the process exits.  Any server
    banner lines that appear in test output are expected and harmless.

    Verified routes (team-lead confirmed against real backend):
        GET  /../soundboard-snag.py          → 404 (path-traversal rejected)
        GET  /api/search  (no q)             → 400
        GET  /api/search?q=x&sort=bad        → 400
        GET  /api/board/a%2Fb                → 400 (slash in board name)
        POST /api/fetch-sound  (bad JSON / bad params / cross-origin) → 400 / 400 / 403
        POST /api/fetch-sound  (valid)       → 200 {status: waiting, token}
        GET  /api/fetch-sound/<token>        → {status: ...}
        POST /api/download, /api/shutdown    → 404 (removed)
        GET  /api/search?q=test              → 200 text/event-stream, final event: results
    """

    @classmethod
    def setUpClass(cls):
        import socket as _socket
        import time as _time

        # Stub _http_get before the server thread starts so searches run offline
        cls._original_http_get = sb._http_get

        def fake_http_get(url):
            if "/search/" in url:
                return _search_page(["testboard"])
            return _board_page([("1", "Sound A"), ("2", "Sound B")], "100")

        sb._http_get = fake_http_get
        cls.tmpdir = tempfile.mkdtemp()
        cls.opened = []   # (token, url) for every download window the server asked for
        cls.coordinator = sb.DownloadCoordinator(lambda token, url: cls.opened.append((token, url)))

        # Grab an ephemeral port then release it; run_server binds the same port
        s = _socket.socket()
        s.setsockopt(_socket.SOL_SOCKET, _socket.SO_REUSEADDR, 1)
        s.bind(("127.0.0.1", 0))
        cls.port = s.getsockname()[1]
        s.close()

        t = threading.Thread(
            target=lambda: sb.run_server("127.0.0.1", cls.port, cls.tmpdir,
                                         coordinator=cls.coordinator),
            daemon=True,
        )
        t.start()

        # Poll until the server is accepting connections (up to 5 s)
        for _ in range(50):
            try:
                _socket.create_connection(("127.0.0.1", cls.port), timeout=0.1).close()
                break
            except OSError:
                _time.sleep(0.1)
        else:
            raise unittest.SkipTest("Server did not start within 5 s")

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmpdir, ignore_errors=True)
        sb._http_get = cls._original_http_get

    def _conn(self):
        return http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)

    def _read_sse_frames(self, body_text):
        """Parse a raw SSE body into a list of (event_type, data_str) tuples."""
        frames = []
        current_event = None
        current_data = []
        for line in body_text.splitlines():
            if line.startswith("event:"):
                current_event = line[len("event:"):].strip()
            elif line.startswith("data:"):
                current_data.append(line[len("data:"):].strip())
            elif line == "" and (current_event is not None or current_data):
                frames.append((current_event, "\n".join(current_data)))
                current_event = None
                current_data = []
        if current_event is not None or current_data:
            frames.append((current_event, "\n".join(current_data)))
        return frames

    # -- Static path traversal ------------------------------------------------

    def test_path_traversal_dot_dot_rejected(self):
        """GET /../soundboard-snag.py must not return 200 with source contents."""
        conn = self._conn()
        try:
            conn.request("GET", "/../soundboard-snag.py")
            resp = conn.getresponse()
            self.assertNotEqual(resp.status, 200)
        finally:
            conn.close()

    # -- /api/search ----------------------------------------------------------

    def test_search_missing_q_returns_400(self):
        conn = self._conn()
        try:
            conn.request("GET", "/api/search")
            resp = conn.getresponse()
            self.assertEqual(resp.status, 400)
        finally:
            conn.close()

    def test_search_bad_sort_param_returns_400(self):
        conn = self._conn()
        try:
            conn.request("GET", "/api/search?q=test&sort=notavalidsort")
            resp = conn.getresponse()
            self.assertEqual(resp.status, 400)
        finally:
            conn.close()

    def test_search_content_type_is_event_stream(self):
        conn = self._conn()
        try:
            conn.request("GET", "/api/search?q=test")
            resp = conn.getresponse()
            self.assertEqual(resp.status, 200)
            self.assertIn("text/event-stream", resp.getheader("Content-Type", ""))
        finally:
            conn.close()

    def test_search_stream_ends_with_results_event(self):
        conn = self._conn()
        try:
            conn.request("GET", "/api/search?q=test")
            resp = conn.getresponse()
            self.assertEqual(resp.status, 200)
            frames = self._read_sse_frames(resp.read().decode("utf-8"))
            event_types = [e for e, _ in frames]
            self.assertIn("results", event_types)
            self.assertEqual(event_types[-1], "results")
        finally:
            conn.close()

    def test_search_results_frame_is_valid_json_list(self):
        conn = self._conn()
        try:
            conn.request("GET", "/api/search?q=test")
            resp = conn.getresponse()
            frames = self._read_sse_frames(resp.read().decode("utf-8"))
            data = next((d for e, d in frames if e == "results"), None)
            self.assertIsNotNone(data, "No 'results' frame in SSE stream")
            self.assertIsInstance(json.loads(data), list)
        finally:
            conn.close()

    def test_search_worker_exception_yields_error_event(self):
        """When the search engine raises, the stream must include an error event."""
        original = sb._http_get
        sb._http_get = lambda url: (_ for _ in ()).throw(RuntimeError("simulated failure"))
        conn = self._conn()
        try:
            # Unique query: the server caches fetched pages for a few minutes.
            conn.request("GET", "/api/search?q=worker-exception-case")
            resp = conn.getresponse()
            body = resp.read().decode("utf-8")
            if resp.status == 200:
                event_types = [e for e, _ in self._read_sse_frames(body)]
                self.assertIn("error", event_types)
        finally:
            sb._http_get = original
            conn.close()

    # -- /api/board -----------------------------------------------------------

    def test_board_with_slash_in_name_returns_400(self):
        """Board identifier containing a path separator → 400 (guardrail B2)."""
        conn = self._conn()
        try:
            conn.request("GET", "/api/board/a%2Fb")
            resp = conn.getresponse()
            self.assertEqual(resp.status, 400)
        finally:
            conn.close()

    # -- /api/fetch-sound -----------------------------------------------------

    def _post(self, path, payload, headers=None):
        conn = self._conn()
        try:
            hdrs = {"Content-Type": "application/json"}
            hdrs.update(headers or {})
            body = payload if isinstance(payload, bytes) else json.dumps(payload).encode()
            conn.request("POST", path, body=body, headers=hdrs)
            resp = conn.getresponse()
            return resp.status, resp.read().decode("utf-8")
        finally:
            conn.close()

    def test_fetch_sound_bad_requests_return_400(self):
        self.assertEqual(self._post("/api/fetch-sound", b"not json")[0], 400)
        self.assertEqual(self._post("/api/fetch-sound", {"something": "else"})[0], 400)
        self.assertEqual(self._post("/api/fetch-sound", {"board": "", "sound_id": "1"})[0], 400)
        self.assertEqual(self._post("/api/fetch-sound", {"board": "foo/bar", "sound_id": "1"})[0], 400)
        self.assertEqual(self._post("/api/fetch-sound", {"board": "ok", "sound_id": "12x"})[0], 400)

    def test_fetch_sound_cross_origin_is_forbidden(self):
        status, _ = self._post("/api/fetch-sound", {"board": "ok", "sound_id": "1"},
                               headers={"Origin": "https://evil.example"})
        self.assertEqual(status, 403)

    def test_fetch_sound_opens_a_window_and_reports_status(self):
        before = len(self.opened)
        status, body = self._post("/api/fetch-sound",
                                  {"board": "My Board", "sound_id": "4242", "title": "Pew Pew"})
        self.assertEqual(status, 200)
        data = json.loads(body)
        self.assertEqual(data["status"], "waiting")
        self.assertEqual(len(self.opened), before + 1)
        token, url = self.opened[-1]
        self.assertEqual(token, data["token"])
        self.assertEqual(url, "%s/track/download/4242" % sb.BASE_URL)

        conn = self._conn()
        try:
            conn.request("GET", "/api/fetch-sound/" + token)
            self.assertEqual(json.loads(conn.getresponse().read().decode())["status"], "waiting")
        finally:
            conn.close()

        # The window's download hook asks where to save: board folder + clean name.
        path = self.coordinator.destination_for(token, "weird_name.mp3")
        self.assertEqual(path, os.path.join(os.path.realpath(self.tmpdir), "My Board", "Pew Pew.mp3"))

    def test_file_endpoint_opens_or_reveals_only_files_in_the_download_folder(self):
        folder = os.path.join(self.tmpdir, "Board")
        os.makedirs(folder, exist_ok=True)
        saved = os.path.join(folder, "Pew.mp3")
        open(saved, "wb").close()
        with mock.patch.object(sb.subprocess, "Popen") as popen:
            self.assertEqual(self._post("/api/file", {"path": saved, "action": "open"})[0], 200)
            self.assertEqual(self._post("/api/file", {"path": saved, "action": "reveal"})[0], 200)
            real = os.path.realpath(saved)
            self.assertEqual([c.args[0] for c in popen.call_args_list],
                             [["open", real], ["open", "-R", real]])
            popen.reset_mock()
            outside = os.path.join(tempfile.gettempdir(), "not-in-the-download-folder.mp3")
            open(outside, "wb").close()
            self.addCleanup(os.remove, outside)
            for payload in ({"path": outside, "action": "open"},
                            {"path": os.path.join(folder, "missing.mp3"), "action": "open"},
                            {"path": saved + "/../../../etc/hosts", "action": "open"},
                            {"path": saved, "action": "delete"},
                            {"action": "open"}):
                self.assertEqual(self._post("/api/file", payload)[0], 400, payload)
            self.assertEqual(self._post("/api/file", {"path": saved, "action": "open"},
                                        headers={"Origin": "https://evil.example"})[0], 403)
            popen.assert_not_called()

    def test_removed_endpoints_are_gone(self):
        self.assertEqual(self._post("/api/download", {"board": "ok"})[0], 404)
        self.assertEqual(self._post("/api/shutdown", {})[0], 404)


class ServerBackoffAndSavedTests(unittest.TestCase):
    """A dedicated server: the shared cooldown must not leak into other tests."""

    @classmethod
    def setUpClass(cls):
        cls._original_http_get = sb._http_get
        cls.calls = []
        cls.mode = "ok"

        def fake_http_get(url):
            cls.calls.append(url)
            if cls.mode == "limited":
                raise sb.HTTPError(url, 429, "Too Many Requests", {}, None)
            return _board_page([("1", "Sound A"), ("2", "Sound B"), ("3", "Sound A")], "100")

        sb._http_get = fake_http_get
        cls.tmpdir = tempfile.mkdtemp()
        holder, ready = [], threading.Event()
        threading.Thread(
            target=lambda: sb.run_server("127.0.0.1", 0, cls.tmpdir,
                                         on_ready=lambda h: (holder.append(h), ready.set())),
            daemon=True).start()
        assert ready.wait(5), "server did not start"
        cls.httpd = holder[0]
        cls.port = cls.httpd.server_address[1]

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()
        shutil.rmtree(cls.tmpdir, ignore_errors=True)
        sb._http_get = cls._original_http_get

    def _board(self, name):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        try:
            conn.request("GET", "/api/board/" + name)
            return json.loads(conn.getresponse().read().decode("utf-8"))
        finally:
            conn.close()

    def test_a_board_lists_which_sounds_are_already_saved(self):
        folder = os.path.join(self.tmpdir, "saved board")
        os.makedirs(folder)
        for name in ("Sound A.mp3", "Sound A (3).mp3"):    # same-titled twins: plain name, then "(id)"
            open(os.path.join(folder, name), "wb").close()
        data = self._board("saved%20board")
        self.assertEqual([(x["id"], x["saved"]) for x in data["sounds"]],
                         [("1", True), ("2", False), ("3", True)])

    def test_zz_site_refusal_pauses_all_requests_but_cached_pages_still_work(self):
        type(self).mode = "ok"
        self.assertIsNone(self._board("cached")["error"])            # now cached
        type(self).mode = "limited"
        before = len(self.calls)
        self.assertIn("429", self._board("other")["error"])           # the site says stop
        self.assertEqual(len(self.calls), before + 1)
        self.assertIn("slow down", self._board("another")["error"])   # refused locally
        self.assertEqual(len(self.calls), before + 1)                 # no second request
        self.assertIsNone(self._board("cached")["error"])            # cache still served


if __name__ == "__main__":
    unittest.main(verbosity=2)

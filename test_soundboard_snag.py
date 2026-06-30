#!/usr/bin/env python3
"""Unit tests for the pure, network-free helpers in ``soundboard-snag.py``.

The production module's file name contains a hyphen, which is not a valid Python
identifier, so it cannot be imported with a normal ``import`` statement. We load
it by path via ``importlib`` under a clean module name. Loading executes the
module top-level only (definitions); ``main()`` runs solely under
``if __name__ == "__main__"`` in the production file, so importing it here makes
no network calls and parses no CLI arguments.

These are characterization tests: they pin the *current* observable behavior of
the deterministic helpers so future refactors get a regression signal. Run with:

    python3 test_soundboard_snag.py
    python3 -m unittest test_soundboard_snag
"""

import http.client
import importlib.util
import inspect
import io
import json
import os
import re
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


class ExtractFilenameFromHeadersTests(_SnagHelpersMixin):
    def test_double_quoted(self):
        self.assertEqual(
            self.snag._extract_filename_from_headers(
                {"content-disposition": 'attachment; filename="song.mp3"'}
            ),
            "song.mp3",
        )

    def test_single_quoted(self):
        self.assertEqual(
            self.snag._extract_filename_from_headers(
                {"content-disposition": "attachment; filename='x.mp3'"}
            ),
            "x.mp3",
        )

    def test_bare_value(self):
        self.assertEqual(
            self.snag._extract_filename_from_headers(
                {"content-disposition": "attachment; filename=bare.mp3"}
            ),
            "bare.mp3",
        )

    def test_missing_header_returns_none(self):
        self.assertIsNone(self.snag._extract_filename_from_headers({}))


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


class FormatUpdatedLineTests(unittest.TestCase):
    def test_known_date_with_source_and_stats(self):
        dt = datetime(2025, 1, 2, tzinfo=timezone.utc)
        self.assertEqual(
            sb._format_updated_line(dt, "track", (3, 5)),
            "Updated: 2025-01-02 (approx via track; track headers: 3/5)",
        )

    def test_known_date_no_source_no_stats(self):
        dt = datetime(2025, 1, 2, tzinfo=timezone.utc)
        self.assertEqual(sb._format_updated_line(dt, None, None), "Updated: 2025-01-02 (approx)")

    def test_unknown_date(self):
        self.assertEqual(sb._format_updated_line(None, None, None), "Updated: unknown (approx)")
        self.assertEqual(
            sb._format_updated_line(None, None, (0, 4)),
            "Updated: unknown (approx; track headers: 0/4)",
        )


class FormatSkippedBreakdownTests(unittest.TestCase):
    def test_empty_when_all_zero(self):
        buckets = {"views": 0, "sounds": 0, "updated_unknown": 0, "updated_too_old": 0}
        self.assertEqual(sb._format_skipped_breakdown(buckets), "")

    def test_fixed_order_and_nonzero_only(self):
        buckets = {"views": 2, "sounds": 0, "updated_unknown": 1, "updated_too_old": 3}
        self.assertEqual(
            sb._format_skipped_breakdown(buckets),
            "views: 2, updated unknown: 1, updated too old: 3",
        )


def _make_board(**overrides):
    base = dict(
        board_name="starwars", has_downloads=True, sounds_info=[("1", "Pew"), ("2", "Boom")],
        total_count=12, board_desc="", category="", views="", tags=[],
        views_int=0, approx_updated=None, approx_source=None,
    )
    base.update(overrides)
    return sb.BoardResult(**base)


class RenderBoardLinesTests(unittest.TestCase):
    def _plain(self, lines):
        # strip ANSI so assertions check semantic content, not color codes
        return [re.sub(r"\033\[[0-9;]*m", "", ln) for ln in lines]

    def test_downloadable_header_and_url(self):
        lines = self._plain(sb._render_board_lines(_make_board(), None, include_dates=False))
        self.assertIn("Board: starwars - ✓ DOWNLOADABLE (12 sounds total)", lines[0])
        self.assertEqual(lines[1], "URL: https://www.soundboard.com/sb/starwars")

    def test_play_only_status(self):
        lines = self._plain(sb._render_board_lines(_make_board(has_downloads=False), None, include_dates=False))
        self.assertIn("✗ PLAY-ONLY", lines[0])

    def test_optional_fields_present_only_when_set(self):
        plain = self._plain(sb._render_board_lines(_make_board(), None, include_dates=False))
        joined = "\n".join(plain)
        self.assertNotIn("Description:", joined)
        self.assertNotIn("Category:", joined)
        self.assertNotIn("Tags:", joined)
        plain2 = self._plain(sb._render_board_lines(
            _make_board(board_desc="d", category="Movies", views="1,234", tags=["a", "b"]),
            None, include_dates=False))
        j2 = "\n".join(plain2)
        self.assertIn("Description: d", j2)
        self.assertIn("Category: Movies", j2)
        self.assertIn("Views: 1,234", j2)
        self.assertIn("Tags: a, b", j2)

    def test_dates_line_only_when_include_dates(self):
        no_dates = "\n".join(self._plain(sb._render_board_lines(_make_board(), None, include_dates=False)))
        self.assertNotIn("Updated:", no_dates)
        with_dates = "\n".join(self._plain(sb._render_board_lines(_make_board(), (3, 5), include_dates=True)))
        self.assertIn("Updated: unknown (approx; track headers: 3/5)", with_dates)

    def test_sample_files_listed(self):
        plain = self._plain(sb._render_board_lines(_make_board(), None, include_dates=False))
        joined = "\n".join(plain)
        self.assertIn("Sample files (showing 2 of 12):", joined)
        self.assertIn(" 1. Pew", joined)
        self.assertIn(" 2. Boom", joined)

    def test_no_sample_section_when_empty(self):
        joined = "\n".join(self._plain(sb._render_board_lines(_make_board(sounds_info=[]), None, include_dates=False)))
        self.assertNotIn("Sample files", joined)


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

    params = dict(max_results=10, min_views=0, min_sounds=0, include_dates=False,
                  progress=False, verbose=False)
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


class SnagPipelineTests(unittest.TestCase):
    """Exercise SoundboardSnag.snag() guard + abort logic offline via the injected fetcher."""

    def _snag(self, html, **kwargs):
        return sb.SoundboardSnag(
            "https://www.soundboard.com/sb/test", fetcher=lambda url: html, **kwargs)

    def test_play_only_board_raises(self):
        html = _board_item("1", "S1", downloadable=False) + _board_item("2", "S2", downloadable=False)
        snag = self._snag(html)
        with redirect_stdout(io.StringIO()), self.assertRaises(RuntimeError) as cm:
            snag.snag()
        self.assertIn("downloads disabled", str(cm.exception))

    def test_no_audio_found_raises(self):
        snag = self._snag("<html><body>nothing here</body></html>")
        with redirect_stdout(io.StringIO()), self.assertRaises(RuntimeError) as cm:
            snag.snag()
        self.assertIn("No audio files found", str(cm.exception))

    def test_consecutive_failure_abort(self):
        import shutil
        import tempfile
        html = "".join(_board_item(str(i), f"S{i}", downloadable=True) for i in range(1, 6))
        root = tempfile.mkdtemp()
        try:
            snag = self._snag(html, download_root=root)
            with mock.patch.object(snag, "_snag_sound", return_value=(False, "boom")) as m, \
                    mock.patch("time.sleep"), redirect_stdout(io.StringIO()):
                result = snag.snag()
            # 5 downloadable sounds, but aborts after 2 consecutive failures
            self.assertEqual(m.call_count, 2)
            # snag() reports failure (returns False) on the early-exit path
            self.assertFalse(result)
        finally:
            shutil.rmtree(root, ignore_errors=True)

    def test_fetch_page_wraps_httperror_as_runtimeerror(self):
        def boom(url):
            raise URLError("dns fail")
        snag = sb.SoundboardSnag("https://www.soundboard.com/sb/test", fetcher=boom)
        with self.assertRaises(RuntimeError) as cm:
            snag._fetch_page()
        self.assertIn("Network error", str(cm.exception))


# ── Helpers for new seam guards ──────────────────────────────────────────────

def _has_param(callable_obj, param_name):
    """Return True if callable_obj's signature includes param_name (safe wrapper)."""
    try:
        return param_name in inspect.signature(callable_obj).parameters
    except (ValueError, TypeError):
        return False


# ── BoardResultToDictTests ────────────────────────────────────────────────────

@unittest.skipUnless(hasattr(sb, "board_result_to_dict"), "seam not implemented yet")
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


# ── JsonOutputTests ───────────────────────────────────────────────────────────

@unittest.skipUnless(hasattr(sb, "board_result_to_dict"), "seam not implemented yet")
class JsonOutputTests(unittest.TestCase):
    """--search --json serialisation path (offline).

    NOTE: The end-to-end --json CLI path (subprocess + real argparse dispatch)
    is covered by the manual verification step in the plan (step 5).  These
    tests cover the board_result_to_dict helper, which is the only new logic,
    to confirm the JSON payload is valid and free of ANSI codes.
    """

    def test_list_serialises_to_valid_json(self):
        boards = [
            _make_board(board_name="alpha", sounds_info=[("1", "A")]),
            _make_board(board_name="beta", has_downloads=False, sounds_info=[]),
        ]
        dicts = [sb.board_result_to_dict(b) for b in boards]
        payload = json.dumps(dicts, default=str)
        parsed = json.loads(payload)
        self.assertIsInstance(parsed, list)
        self.assertEqual(len(parsed), 2)

    def test_json_payload_contains_no_ansi_escape_codes(self):
        board = _make_board(board_name="sw", category="Movies")
        payload = json.dumps([sb.board_result_to_dict(board)])
        self.assertNotIn("\033[", payload)

    def test_board_name_round_trips_through_json(self):
        board = _make_board(board_name="star%20wars")
        parsed = json.loads(json.dumps([sb.board_result_to_dict(board)]))
        self.assertEqual(parsed[0]["board"], "star%20wars")

    def test_approx_updated_serialises_as_json_string_not_object(self):
        # board_result_to_dict must convert the datetime to a string so that
        # json.dumps succeeds without a custom encoder
        dt = datetime(2025, 6, 1, tzinfo=timezone.utc)
        board = _make_board(approx_updated=dt)
        d = sb.board_result_to_dict(board)
        payload = json.dumps(d)  # must not raise TypeError
        parsed = json.loads(payload)
        self.assertIsInstance(parsed["approx_updated"], str)

    def test_none_approx_updated_serialises_as_json_null(self):
        board = _make_board(approx_updated=None)
        d = sb.board_result_to_dict(board)
        payload = json.dumps(d)
        parsed = json.loads(payload)
        self.assertIsNone(parsed["approx_updated"])


# ── SearchBoardsRenderTests ───────────────────────────────────────────────────

_RENDER_SEAM_GUARD = unittest.skipUnless(
    _has_param(sb.search_boards, "render"),
    "render= seam not implemented yet",
)


@_RENDER_SEAM_GUARD
class SearchBoardsRenderTests(unittest.TestCase):
    """search_boards(render=False) must produce NO stdout while returning results.

    Each test captures stdout around a search_boards() call and asserts that
    render=False → empty capture, render=True → non-empty capture.  Stubs the
    fetch= seam (same style as SearchBoardsOrchestrationTests) so no real
    network.  Does NOT use contextlib.redirect_stdout in the production call
    itself — search_boards must gate its own prints (R2-N1 / B1 in the plan).
    """

    def _call(self, fake_pages, render, **extra):
        """Run search_boards with a stub fetcher; return (results, captured_stdout)."""
        def fetch(url):
            if url in fake_pages:
                return fake_pages[url]
            raise URLError("no such page")

        params = dict(
            max_results=10, min_views=0, min_sounds=0,
            include_dates=False, progress=False, verbose=False,
            render=render,
        )
        params.update(extra)
        buf = io.StringIO()
        with mock.patch("time.sleep"), redirect_stdout(buf):
            results = sb.search_boards("q", fetch=fetch, **params)
        return results, buf.getvalue()

    def test_render_false_no_stdout_normal_results(self):
        pages = {
            f"{B}/search/q": _search_page(["sw"]),
            f"{B}/sb/sw": _board_page([("1", "X")], "100"),
        }
        results, out = self._call(pages, render=False)
        self.assertEqual(out, "")
        self.assertEqual(len(results), 1)

    def test_render_false_no_stdout_zero_results(self):
        pages = {f"{B}/search/q": _search_page([])}
        results, out = self._call(pages, render=False)
        self.assertEqual(out, "")
        self.assertIsInstance(results, list)

    def test_render_false_no_stdout_on_board_fetch_error(self):
        # "broken" slug not in pages → fetch raises URLError → search_boards handles it
        pages = {f"{B}/search/q": _search_page(["broken"])}
        results, out = self._call(pages, render=False)
        self.assertEqual(out, "")

    def test_render_false_no_stdout_when_board_filtered_out(self):
        pages = {
            f"{B}/search/q": _search_page(["low"]),
            f"{B}/sb/low": _board_page([("1", "x")], "1"),  # 1 view < min_views 10
        }
        results, out = self._call(pages, render=False, min_views=10)
        self.assertEqual(out, "")
        self.assertEqual(results, [])

    def test_render_true_produces_stdout(self):
        """Sanity: render=True (default) must still print something."""
        pages = {
            f"{B}/search/q": _search_page(["sw"]),
            f"{B}/sb/sw": _board_page([("1", "X")], "100"),
        }
        results, out = self._call(pages, render=True)
        self.assertGreater(len(out.strip()), 0)
        self.assertEqual(len(results), 1)


# ── SnagEventCbTests ──────────────────────────────────────────────────────────

_SNAG_EVENT_CB_GUARD = unittest.skipUnless(
    _has_param(sb.SoundboardSnag.__init__, "event_cb"),
    "event_cb seam not implemented yet",
)


@_SNAG_EVENT_CB_GUARD
class SnagEventCbTests(unittest.TestCase):
    """SoundboardSnag event_cb / render / cancel_event seams.

    Downloads are stubbed by patching the instance method _snag_sound — exactly
    like SnagPipelineTests.test_consecutive_failure_abort — so no network or
    real filesystem writes occur.  _snag_sound return-value contract:
        (True,  (name, kb))  → file_saved event
        (None,  name)        → file_skipped event
        (False, err_msg)     → file_failed event

    Verified event sequence (team-lead confirmed against real backend):
        download_start → board_parsed → file_start → file_saved (×N) → download_complete
    After cancel mid-run:
        … file_saved → download_aborted → download_complete
        (download_complete always fires; it's the summary step)

    event_cb contract (plan N3):
        download_start{board, total}
        board_parsed{count}
        file_start{i, n, sound_id}
        file_saved{i, n, name, kb}
        file_skipped{i, n, name}
        file_failed{i, n, error}
        download_aborted{reason}
        download_complete{snagged, existing, failed}
        download_error{error}
    """

    # Minimal HTML fixtures — one, two, and three downloadable sounds
    _HTML_ONE = _board_item("1", "Sound A", True)
    _HTML_TWO = _board_item("1", "Sound A", True) + _board_item("2", "Sound B", True)
    _HTML_THREE = (
        _board_item("1", "Sound A", True) +
        _board_item("2", "Sound B", True) +
        _board_item("3", "Sound C", True)
    )

    def _run_snag(self, html, tmpdir, snag_results=None, **kwargs):
        """Build SoundboardSnag, stub _snag_sound, run snag(), return captured stdout.

        snag_results is a list of return values for successive _snag_sound calls:
            [(True, ("Clip.mp3", 1.0)), ...]  → success
            [(None, "Clip.mp3"), ...]          → already-exists skip
            [(False, "error msg"), ...]        → failure
        """
        if snag_results is None:
            snag_results = [(True, ("Clip.mp3", 1.0))]
        snag_obj = sb.SoundboardSnag(
            "https://www.soundboard.com/sb/test",
            fetcher=lambda url: html,
            download_root=tmpdir,
            **kwargs,
        )
        buf = io.StringIO()
        with mock.patch.object(snag_obj, "_snag_sound", side_effect=snag_results), \
             mock.patch("time.sleep"), \
             redirect_stdout(buf):
            snag_obj.snag()
        return buf.getvalue()

    def _normalize(self, text, tmpdir):
        """Replace tmpdir absolute path so two independent runs can be compared."""
        return text.replace(os.path.abspath(tmpdir), "<ROOT>")

    def test_byte_identical_output_with_explicit_defaults(self):
        """event_cb=None, render=True must produce byte-identical output to no-kwargs."""
        results = [(True, ("Clip.mp3", 1.0))]
        with tempfile.TemporaryDirectory() as tmp1, \
             tempfile.TemporaryDirectory() as tmp2:
            out_legacy = self._run_snag(self._HTML_ONE, tmp1, snag_results=list(results))
            out_new = self._run_snag(
                self._HTML_ONE, tmp2, snag_results=list(results),
                event_cb=None, render=True,
            )
        self.assertEqual(self._normalize(out_legacy, tmp1), self._normalize(out_new, tmp2))

    def test_event_cb_receives_documented_events_in_order(self):
        """event_cb gets download_start → board_parsed → file_start/file_saved → download_complete."""
        events = []

        def cb(event_type, **fields):
            events.append(event_type)

        snag_results = [(True, ("ClipA.mp3", 1.0)), (True, ("ClipB.mp3", 2.0))]
        with tempfile.TemporaryDirectory() as tmpdir:
            snag_obj = sb.SoundboardSnag(
                "https://www.soundboard.com/sb/test",
                fetcher=lambda url: self._HTML_TWO,
                download_root=tmpdir,
                event_cb=cb,
                render=False,
            )
            with mock.patch.object(snag_obj, "_snag_sound", side_effect=snag_results), \
                 mock.patch("time.sleep"), \
                 redirect_stdout(io.StringIO()):
                snag_obj.snag()

        self.assertIn("download_start", events)
        self.assertIn("board_parsed", events)
        self.assertIn("file_start", events)
        self.assertIn("file_saved", events)
        self.assertIn("download_complete", events)

        # Ordering: download_start → board_parsed → first file_start → … → download_complete
        self.assertLess(events.index("download_start"), events.index("board_parsed"))
        self.assertLess(events.index("board_parsed"), events.index("file_start"))
        self.assertEqual(events[-1], "download_complete")

    def test_render_false_with_event_cb_produces_no_stdout(self):
        """render=False suppresses all snag() prints even when event_cb is set."""
        snag_results = [(True, ("Clip.mp3", 1.0)), (True, ("Clip2.mp3", 2.0))]
        with tempfile.TemporaryDirectory() as tmpdir:
            out = self._run_snag(
                self._HTML_TWO, tmpdir,
                snag_results=snag_results,
                event_cb=lambda t, **f: None,
                render=False,
            )
        self.assertEqual(out, "")

    def test_cancel_event_set_before_run_aborts_with_download_aborted(self):
        """cancel_event already set → loop exits before first file; emits download_aborted."""
        events = []

        def cb(event_type, **fields):
            events.append(event_type)

        cancel = threading.Event()
        cancel.set()  # pre-set → abort immediately when the loop checks

        snag_results = [(True, ("Clip.mp3", 1.0)), (True, ("Clip2.mp3", 2.0))]
        with tempfile.TemporaryDirectory() as tmpdir:
            snag_obj = sb.SoundboardSnag(
                "https://www.soundboard.com/sb/test",
                fetcher=lambda url: self._HTML_TWO,
                download_root=tmpdir,
                event_cb=cb,
                render=False,
                cancel_event=cancel,
            )
            with mock.patch.object(snag_obj, "_snag_sound", side_effect=snag_results), \
                 mock.patch("time.sleep"), \
                 redirect_stdout(io.StringIO()):
                snag_obj.snag()

        self.assertIn("download_aborted", events)
        self.assertNotIn("file_saved", events)

    def test_cancel_event_mid_run_stops_between_files(self):
        """cancel_event set during first file_saved callback stops before file 2 and 3.

        Verified contract (team-lead confirmed):
            download_start, board_parsed, file_start, file_saved,
            download_aborted, download_complete
        download_complete always fires (it's the summary step, not skipped on abort).
        """
        events = []
        file_saved_count = [0]
        cancel = threading.Event()

        def cb(event_type, **fields):
            events.append(event_type)
            if event_type == "file_saved":
                file_saved_count[0] += 1
                cancel.set()  # arm cancellation after the first completed file

        snag_results = [
            (True, ("ClipA.mp3", 1.0)),
            (True, ("ClipB.mp3", 2.0)),
            (True, ("ClipC.mp3", 3.0)),
        ]
        with tempfile.TemporaryDirectory() as tmpdir:
            snag_obj = sb.SoundboardSnag(
                "https://www.soundboard.com/sb/test",
                fetcher=lambda url: self._HTML_THREE,
                download_root=tmpdir,
                event_cb=cb,
                render=False,
                cancel_event=cancel,
            )
            with mock.patch.object(snag_obj, "_snag_sound", side_effect=snag_results), \
                 mock.patch("time.sleep"), \
                 redirect_stdout(io.StringIO()):
                snag_obj.snag()

        self.assertIn("download_aborted", events)
        self.assertIn("download_complete", events)  # summary always fires
        self.assertEqual(file_saved_count[0], 1)    # exactly one file saved before abort
        self.assertLess(events.count("file_saved"), 3)


# ── ServerProtocolTests ───────────────────────────────────────────────────────

_SERVER_SEAM_GUARD = unittest.skipUnless(
    hasattr(sb, "run_server"),
    "run_server entrypoint not implemented yet",
)


@_SERVER_SEAM_GUARD
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
        POST /api/download  (bad JSON)       → 400
        POST /api/download  (missing board)  → 400
        POST /api/download  {board:""}       → 400
        POST /api/download  {board:"a/b"}    → 400
        POST /api/download  (escaping root)  → 400
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

        # Grab an ephemeral port then release it; run_server binds the same port
        s = _socket.socket()
        s.setsockopt(_socket.SOL_SOCKET, _socket.SO_REUSEADDR, 1)
        s.bind(("127.0.0.1", 0))
        cls.port = s.getsockname()[1]
        s.close()

        t = threading.Thread(
            target=lambda: sb.run_server("127.0.0.1", cls.port, cls.tmpdir),
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
            conn.request("GET", "/api/search?q=test")
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

    # -- /api/download --------------------------------------------------------

    def test_download_bad_json_body_returns_400(self):
        conn = self._conn()
        try:
            conn.request("POST", "/api/download", body=b"not json",
                         headers={"Content-Type": "application/json"})
            resp = conn.getresponse()
            self.assertEqual(resp.status, 400)
        finally:
            conn.close()

    def test_download_missing_board_field_returns_400(self):
        conn = self._conn()
        try:
            body = json.dumps({"something": "else"}).encode()
            conn.request("POST", "/api/download", body=body,
                         headers={"Content-Type": "application/json"})
            resp = conn.getresponse()
            self.assertEqual(resp.status, 400)
        finally:
            conn.close()

    def test_download_empty_board_returns_400(self):
        conn = self._conn()
        try:
            body = json.dumps({"board": ""}).encode()
            conn.request("POST", "/api/download", body=body,
                         headers={"Content-Type": "application/json"})
            resp = conn.getresponse()
            self.assertEqual(resp.status, 400)
        finally:
            conn.close()

    def test_download_board_with_slash_returns_400(self):
        conn = self._conn()
        try:
            body = json.dumps({"board": "foo/bar"}).encode()
            conn.request("POST", "/api/download", body=body,
                         headers={"Content-Type": "application/json"})
            resp = conn.getresponse()
            self.assertEqual(resp.status, 400)
        finally:
            conn.close()

    def test_download_root_escaping_returns_400(self):
        """download_root that would escape the server root → 400 (guardrail B4)."""
        conn = self._conn()
        try:
            body = json.dumps({"board": "starwars", "download_root": "/tmp/../../etc"}).encode()
            conn.request("POST", "/api/download", body=body,
                         headers={"Content-Type": "application/json"})
            resp = conn.getresponse()
            self.assertEqual(resp.status, 400)
        finally:
            conn.close()


if __name__ == "__main__":
    unittest.main(verbosity=2)

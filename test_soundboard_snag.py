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

import importlib.util
import io
import os
import re
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
    def test_title_cases_all_lower_or_all_upper(self):
        self.assertEqual(self.snag._sanitize_filename("hello world.mp3", "1", ""), "Hello World.mp3")
        self.assertEqual(self.snag._sanitize_filename("MY SOUND.mp3", "1", ""), "My Sound.mp3")

    def test_mixed_case_preserved(self):
        self.assertEqual(self.snag._sanitize_filename("Already Mixed.mp3", "1", ""), "Already Mixed.mp3")

    def test_html_entities_decoded(self):
        self.assertEqual(self.snag._sanitize_filename("Don&#039;t Stop.mp3", "1", ""), "Don't Stop.mp3")

    def test_windows_reserved_name_is_prefixed(self):
        self.assertEqual(self.snag._sanitize_filename("CON.mp3", "1", ""), "_Con.mp3")
        self.assertEqual(self.snag._sanitize_filename("PRN.mp3", "1", ""), "_Prn.mp3")

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
                snag.snag()
            # 5 downloadable sounds, but aborts after 2 consecutive failures
            self.assertEqual(m.call_count, 2)
        finally:
            shutil.rmtree(root, ignore_errors=True)

    def test_fetch_page_wraps_httperror_as_runtimeerror(self):
        def boom(url):
            raise URLError("dns fail")
        snag = sb.SoundboardSnag("https://www.soundboard.com/sb/test", fetcher=boom)
        with self.assertRaises(RuntimeError) as cm:
            snag._fetch_page()
        self.assertIn("Network error", str(cm.exception))


if __name__ == "__main__":
    unittest.main(verbosity=2)

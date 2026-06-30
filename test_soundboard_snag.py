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
import os
import unittest
from datetime import datetime, timezone


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


if __name__ == "__main__":
    unittest.main(verbosity=2)

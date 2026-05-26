"""
Tests for common.py — semver parsing and bump classification.

These tests cover only pure Python logic (no GitHub API calls, no file I/O).
They should run anywhere with no setup beyond installing pytest.

Run from the repo root:
    python -m pytest tests/test_common.py -v

Or run directly (no pytest needed):
    python tests/test_common.py
"""

import os
import sys
import unittest

# Make sure we can import from src/ regardless of how this test is invoked
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from common import parse_semver, classify_bump


class TestParseSemver(unittest.TestCase):
    """
    parse_semver turns a version string into a (major, minor, patch) tuple.

    We need it to handle the full range of quirks you'll find in real dependency
    update PR titles: v-prefixes, two-part versions, unparseable strings, etc.
    Returning None for bad input (instead of crashing or returning a fake zero tuple)
    lets classify_bump() distinguish "we couldn't parse it" from "it's actually 0.0.0".
    """

    # ── Happy path ────────────────────────────────────────────

    def test_standard_three_part_version(self):
        self.assertEqual(parse_semver("1.2.3"), (1, 2, 3))

    def test_v_prefix_is_stripped(self):
        """Dependabot often includes a 'v' prefix — we strip it automatically."""
        self.assertEqual(parse_semver("v2.5.1"), (2, 5, 1))

    def test_two_part_version_pads_patch_to_zero(self):
        """Some packages use major.minor only (no patch). Pad to zero."""
        self.assertEqual(parse_semver("3.1"), (3, 1, 0))

    def test_one_part_version_pads_minor_and_patch(self):
        self.assertEqual(parse_semver("4"), (4, 0, 0))

    def test_four_part_version_ignores_extra_component(self):
        """Calendar versioning like 2024.01.15.1 — we only care about the first three parts."""
        self.assertEqual(parse_semver("1.2.3.4"), (1, 2, 3))

    def test_zero_version_is_valid(self):
        """0.0.0 is a real version (pre-release packages), not an error."""
        self.assertEqual(parse_semver("0.0.0"), (0, 0, 0))

    def test_large_numbers(self):
        self.assertEqual(parse_semver("100.200.300"), (100, 200, 300))

    def test_leading_and_trailing_whitespace_stripped(self):
        self.assertEqual(parse_semver("  1.2.3  "), (1, 2, 3))

    def test_v_prefix_with_whitespace(self):
        self.assertEqual(parse_semver("  v1.0.0  "), (1, 0, 0))

    # ── Error / edge cases ────────────────────────────────────

    def test_unparseable_string_returns_none(self):
        """An unparseable string returns None, not a fake zero tuple or an exception."""
        self.assertIsNone(parse_semver("not-a-version"))

    def test_empty_string_returns_none(self):
        self.assertIsNone(parse_semver(""))

    def test_none_input_returns_none(self):
        """Callers may pass None if version extraction failed upstream."""
        self.assertIsNone(parse_semver(None))

    def test_only_dots_returns_none(self):
        self.assertIsNone(parse_semver("..."))

    def test_letters_mixed_in_returns_none(self):
        """Some pre-release tags like '1.0.0-beta.1' contain non-numeric parts."""
        # The part after the dash isn't part of semver for our purposes;
        # we try to parse what we can. But mixed letters inside a segment fail.
        self.assertIsNone(parse_semver("1.0.abc"))

    def test_integer_input_converted_to_string(self):
        """parse_semver coerces to str, so passing an int doesn't crash."""
        self.assertEqual(parse_semver(1), (1, 0, 0))


class TestClassifyBump(unittest.TestCase):
    """
    classify_bump() determines the tier of a dependency version change.
    This is the core decision logic for Layer 2 — getting it right matters.

    The four possible return values and when they apply:
      "patch"   — same major + minor, different patch  (or no change)
      "minor"   — same major, different minor
      "major"   — different major
      "unknown" — either version string couldn't be parsed
    """

    # ── Standard cases ────────────────────────────────────────

    def test_patch_bump(self):
        """1.2.3 → 1.2.4 is the most common dependency update."""
        self.assertEqual(classify_bump("1.2.3", "1.2.4"), "patch")

    def test_minor_bump(self):
        self.assertEqual(classify_bump("1.2.3", "1.3.0"), "minor")

    def test_major_bump(self):
        self.assertEqual(classify_bump("1.2.3", "2.0.0"), "major")

    def test_major_bump_with_minor_reset(self):
        """1.9.9 → 2.0.0 — major change takes precedence over minor/patch changes."""
        self.assertEqual(classify_bump("1.9.9", "2.0.0"), "major")

    def test_minor_bump_with_patch_reset(self):
        """1.2.3 → 1.3.0 is a minor bump even though patch went from 3 to 0."""
        self.assertEqual(classify_bump("1.2.3", "1.3.0"), "minor")

    def test_v_prefix_handled_transparently(self):
        """v-prefixed versions should produce the same classification as non-prefixed."""
        self.assertEqual(classify_bump("v1.2.3", "v1.2.4"), "patch")
        self.assertEqual(classify_bump("v1.2.3", "v2.0.0"), "major")

    def test_two_part_versions(self):
        self.assertEqual(classify_bump("1.2", "2.0"), "major")
        self.assertEqual(classify_bump("1.2", "1.3"), "minor")
        self.assertEqual(classify_bump("1.2", "1.2"), "patch")

    def test_zero_to_nonzero_major_is_major(self):
        """0.9.x → 1.0.0 is a major bump. Common with pre-release packages going stable."""
        self.assertEqual(classify_bump("0.9.9", "1.0.0"), "major")

    def test_zero_patch_to_nonzero_is_patch(self):
        self.assertEqual(classify_bump("1.2.0", "1.2.1"), "patch")

    # ── Same-version edge case ────────────────────────────────

    def test_no_version_change_is_patch(self):
        """
        If old and new versions are identical (re-pinning, re-running Dependabot, etc.)
        we treat it as patch — the lowest-risk bucket. No point blocking a no-op update.
        """
        self.assertEqual(classify_bump("1.2.3", "1.2.3"), "patch")

    def test_zero_version_no_change(self):
        self.assertEqual(classify_bump("0.0.0", "0.0.0"), "patch")

    # ── Downgrade cases ───────────────────────────────────────

    def test_major_downgrade_classified_as_major(self):
        """
        Downgrading a major version (e.g. rolling back a bad update) is still a major change.
        The != check catches both upgrades and downgrades at the major level.
        """
        self.assertEqual(classify_bump("2.0.0", "1.9.9"), "major")

    def test_minor_downgrade_classified_as_minor(self):
        self.assertEqual(classify_bump("1.3.0", "1.2.3"), "minor")

    # ── Error cases ───────────────────────────────────────────

    def test_unparseable_old_version_returns_unknown(self):
        self.assertEqual(classify_bump("not-a-version", "1.2.3"), "unknown")

    def test_unparseable_new_version_returns_unknown(self):
        self.assertEqual(classify_bump("1.2.3", "???"), "unknown")

    def test_both_unparseable_returns_unknown(self):
        self.assertEqual(classify_bump("bad", "also-bad"), "unknown")

    def test_none_old_version_returns_unknown(self):
        """extract_versions_from_title() can return None — we handle it gracefully."""
        self.assertEqual(classify_bump(None, "1.2.3"), "unknown")

    def test_none_new_version_returns_unknown(self):
        self.assertEqual(classify_bump("1.2.3", None), "unknown")

    def test_empty_string_returns_unknown(self):
        self.assertEqual(classify_bump("", "1.2.3"), "unknown")


if __name__ == "__main__":
    unittest.main(verbosity=2)

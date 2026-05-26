"""
Tests for freeze_check.py — freeze window logic and date arithmetic.

These tests cover only pure Python logic (no GitHub API calls).
They use timedelta to express "days from today" so they stay correct regardless
of when they're run — the tests never hardcode an absolute date.

Run from the repo root:
    python -m pytest tests/test_freeze_check.py -v

Or run directly:
    python tests/test_freeze_check.py
"""

import os
import sys
import unittest
from datetime import date, timedelta

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from freeze_check import (
    classify_freeze_status,
    days_until_ship,
    FREEZE_SAFE,
    FREEZE_TIGHT,
    FREEZE_INSIDE,
)


class TestDaysUntilShip(unittest.TestCase):
    """
    days_until_ship() computes how many calendar days remain until the ship date.

    Why calendar days and not business days?
      Freeze windows are about test coverage, not working hours. A dependency
      that lands on a Friday 8 days before ship is just as risky as one that
      lands on a Monday 8 days before ship. Calendar days keep the logic simple
      and the config value intuitive.
    """

    def test_thirty_days_in_future(self):
        future = (date.today() + timedelta(days=30)).isoformat()
        self.assertEqual(days_until_ship(future), 30)

    def test_seven_days_in_future(self):
        future = (date.today() + timedelta(days=7)).isoformat()
        self.assertEqual(days_until_ship(future), 7)

    def test_one_day_in_future(self):
        future = (date.today() + timedelta(days=1)).isoformat()
        self.assertEqual(days_until_ship(future), 1)

    def test_today_is_zero(self):
        """Shipping today — the freeze window should kick in."""
        self.assertEqual(days_until_ship(date.today().isoformat()), 0)

    def test_one_day_past(self):
        """Ship date already passed — returns a negative number."""
        past = (date.today() - timedelta(days=1)).isoformat()
        self.assertEqual(days_until_ship(past), -1)

    def test_far_past(self):
        past = (date.today() - timedelta(days=90)).isoformat()
        self.assertEqual(days_until_ship(past), -90)


class TestClassifyFreezeStatus(unittest.TestCase):
    """
    classify_freeze_status() is the core judgment call of Layer 1.

    Config values used in these tests:
      freeze_window_days = 10   (inside this → FREEZE_INSIDE)
      tight_window_days  = 21   (inside this → FREEZE_TIGHT)
      beyond tight       → FREEZE_SAFE

    The boundaries are inclusive: exactly 10 days left is inside-freeze,
    exactly 21 days left is tight (not safe).
    """

    FREEZE = 10
    TIGHT  = 21

    def _classify(self, days):
        """Shorthand to call the function with the test config values."""
        return classify_freeze_status(days, self.FREEZE, self.TIGHT)

    # ── Safe zone (> tight_window_days) ──────────────────────

    def test_safe_sixty_days(self):
        self.assertEqual(self._classify(60), FREEZE_SAFE)

    def test_safe_just_outside_tight_boundary(self):
        """22 days is one past the tight window — should be safe."""
        self.assertEqual(self._classify(22), FREEZE_SAFE)

    # ── Tight zone (freeze_window_days < days ≤ tight_window_days) ──

    def test_tight_at_exact_tight_boundary(self):
        """Exactly at the tight window boundary → tight (not safe)."""
        self.assertEqual(self._classify(21), FREEZE_TIGHT)

    def test_tight_midpoint(self):
        self.assertEqual(self._classify(15), FREEZE_TIGHT)

    def test_tight_just_outside_freeze(self):
        """11 days left — one past freeze, still in the tight window."""
        self.assertEqual(self._classify(11), FREEZE_TIGHT)

    # ── Inside-freeze zone (days ≤ freeze_window_days) ───────

    def test_inside_freeze_at_exact_boundary(self):
        """Exactly at the freeze window boundary → inside-freeze (not tight)."""
        self.assertEqual(self._classify(10), FREEZE_INSIDE)

    def test_inside_freeze_five_days(self):
        self.assertEqual(self._classify(5), FREEZE_INSIDE)

    def test_inside_freeze_one_day(self):
        self.assertEqual(self._classify(1), FREEZE_INSIDE)

    def test_inside_freeze_on_ship_day(self):
        """Day zero — shipping today — is inside freeze."""
        self.assertEqual(self._classify(0), FREEZE_INSIDE)

    def test_inside_freeze_after_ship_date(self):
        """
        Past the ship date → still inside-freeze.

        This handles the case where the ship_date wasn't updated in config.yml
        after a release. Conservative behavior: treat as frozen until the config
        is updated to a new date.
        """
        self.assertEqual(self._classify(-1), FREEZE_INSIDE)
        self.assertEqual(self._classify(-30), FREEZE_INSIDE)

    # ── Edge cases with unusual config values ─────────────────

    def test_freeze_and_tight_same_value(self):
        """
        If freeze_window_days == tight_window_days, there's no tight zone.
        Everything at or below that threshold goes straight to inside-freeze.
        """
        result = classify_freeze_status(10, 10, 10)
        self.assertEqual(result, FREEZE_INSIDE)
        result = classify_freeze_status(11, 10, 10)
        self.assertEqual(result, FREEZE_SAFE)

    def test_one_day_freeze_window(self):
        """A minimal freeze window: only the very last day is blocked."""
        self.assertEqual(classify_freeze_status(1, 1, 7), FREEZE_INSIDE)
        self.assertEqual(classify_freeze_status(2, 1, 7), FREEZE_TIGHT)
        self.assertEqual(classify_freeze_status(7, 1, 7), FREEZE_TIGHT)
        self.assertEqual(classify_freeze_status(8, 1, 7), FREEZE_SAFE)

    def test_zero_freeze_window(self):
        """
        A freeze_window_days of 0 means only the ship day itself is blocked.
        Days remaining of 1 or more would be tight (or safe).
        """
        self.assertEqual(classify_freeze_status(0, 0, 7), FREEZE_INSIDE)
        self.assertEqual(classify_freeze_status(1, 0, 7), FREEZE_TIGHT)
        self.assertEqual(classify_freeze_status(8, 0, 7), FREEZE_SAFE)

    def test_large_freeze_window(self):
        """
        A very conservative team might set a 30-day freeze.
        30 days left → inside-freeze; 31 → tight; 61+ → safe.
        """
        self.assertEqual(classify_freeze_status(30, 30, 60), FREEZE_INSIDE)
        self.assertEqual(classify_freeze_status(31, 30, 60), FREEZE_TIGHT)
        self.assertEqual(classify_freeze_status(61, 30, 60), FREEZE_SAFE)


if __name__ == "__main__":
    unittest.main(verbosity=2)

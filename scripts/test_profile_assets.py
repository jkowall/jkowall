#!/usr/bin/env python3
"""Unit tests for profile SVG rendering. No network."""

from __future__ import annotations

import sys
import unittest
import xml.etree.ElementTree as ET
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import generate_profile_assets as profile


class ProfileAssetTests(unittest.TestCase):
    def test_compact_metric_matches_shields_rounding(self) -> None:
        self.assertEqual(profile.compact_metric(8), "8")
        self.assertEqual(profile.compact_metric(1532), "1.5k")
        self.assertEqual(profile.compact_metric(7651), "7.7k")
        self.assertEqual(profile.compact_metric(13845), "14k")
        self.assertEqual(profile.compact_metric(23278), "23k")

    def test_rank_matches_previous_card_grade(self) -> None:
        level, percentile = profile.calculate_rank(
            commits=1333,
            prs=595,
            issues=165,
            reviews=269,
            stars=8,
            followers=91,
        )
        self.assertEqual(level, "A-")
        self.assertGreater(percentile, 25)
        self.assertLessEqual(percentile, 37.5)

    def test_current_streak_ignores_an_empty_today(self) -> None:
        today = date(2026, 10, 11)
        counts = {
            "2026-09-01": 2,
            "2026-10-09": 4,
            "2026-10-10": 3,
            "2026-10-11": 0,
        }
        result = profile.streaks(counts, date(2026, 9, 1), today)
        self.assertEqual(result["current_days"], 2)
        self.assertEqual(result["current_start"], date(2026, 10, 9))
        self.assertEqual(result["current_end"], date(2026, 10, 10))
        self.assertEqual(result["longest_days"], 2)
        self.assertEqual(result["total"], 9)

    def test_longest_streak_spans_a_gap(self) -> None:
        counts = {
            "2026-10-01": 1,
            "2026-10-02": 1,
            "2026-10-03": 1,
            "2026-10-04": 0,
            "2026-10-05": 1,
        }
        result = profile.streaks(counts, date(2026, 10, 1), date(2026, 10, 5))
        self.assertEqual(result["longest_days"], 3)
        self.assertEqual(result["longest_start"], date(2026, 10, 1))
        self.assertEqual(result["longest_end"], date(2026, 10, 3))
        self.assertEqual(result["current_days"], 1)

    def test_rendered_svgs_are_static_images(self) -> None:
        snapshot = {
            "name": "Jonah Kowall",
            "created": "2026-10-01",
            "today": "2026-10-05",
            "stars_earned": 8,
            "commits_last_year": 1333,
            "pull_requests": 595,
            "issues": 165,
            "contributed_to": 20,
            "reviews_last_year": 269,
            "followers": 91,
            "repo_stars": [
                {
                    "owner": "jaegertracing",
                    "repo": "jaeger",
                    "file": "stars-jaeger.svg",
                    "stars": 23278,
                }
            ],
            "languages": [
                {"name": "C", "bytes": 800, "color": "#555555"},
                {"name": "TypeScript", "bytes": 200, "color": "#3178c6"},
            ],
            "contributions": {
                "2026-10-01": 1,
                "2026-10-02": 2,
                "2026-10-03": 0,
                "2026-10-04": 4,
                "2026-10-05": 5,
            },
        }
        images = profile.render_all(snapshot)
        self.assertIn("github-stats.svg", images)
        self.assertIn("github-streak.svg", images)
        self.assertIn("top-languages.svg", images)
        joined = "\n".join(images.values())
        self.assertNotIn("<style", joined)
        self.assertNotIn("<script", joined)
        self.assertNotIn("opacity:0", joined.replace(" ", ""))
        badge = images["stars-jaeger.svg"]
        self.assertIn(">23k<", badge)
        self.assertIn("23,278 stars", badge)
        root = ET.fromstring(images["github-stats.svg"])
        self.assertEqual(root.tag, "{http://www.w3.org/2000/svg}svg")
        self.assertIn("A-", images["github-stats.svg"])
        self.assertIn("Total Contributions", images["github-streak.svg"])
        self.assertIn("TypeScript 20.00%", images["top-languages.svg"])
        self.assertIn("@JKOWALL", images["badge-x.svg"])
        self.assertIn("CONNECT", images["badge-linkedin.svg"])
        self.assertIn("JKOWALL@KOWALL.NET", images["badge-email.svg"])
        self.assertNotIn("data:image", images["badge-x.svg"])
        self.assertNotIn("data:image", images["badge-email.svg"])

    def test_windows_cover_the_range_without_overlap(self) -> None:
        windows = profile.contribution_windows(date(2012, 6, 17), date(2026, 10, 11))
        self.assertEqual(windows[0][0], date(2012, 6, 17))
        self.assertEqual(windows[-1][1], date(2026, 10, 12))
        for (start, end), (nxt, _) in zip(windows, windows[1:]):
            self.assertEqual(end, nxt)
            self.assertLess((end - start).days, 366)
            self.assertGreater((end - start).days, 0)


if __name__ == "__main__":
    unittest.main()

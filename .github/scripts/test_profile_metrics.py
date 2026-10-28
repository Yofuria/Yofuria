"""Regression checks for citation comparison, fallback, and the badge destination."""

import importlib.util
import json
import unittest
from pathlib import Path
from unittest.mock import patch
from urllib.error import URLError

spec = importlib.util.spec_from_file_location("metrics", Path(__file__).with_name("update_metrics.py"))
metrics = importlib.util.module_from_spec(spec)
spec.loader.exec_module(metrics)
CONFIG = json.loads((Path(__file__).resolve().parents[2] / "metrics/config.json").read_text())
NOW = "2026-10-28T10:00:00Z"


def google(value):
    return {"scholar_id": CONFIG["scholarId"], "citedby": value, "updated": "2026-10-27 15:51:51"}


def semantic(value):
    return {"authorId": CONFIG["semanticScholarId"], "citationCount": value}


class CitationComparisonTests(unittest.TestCase):
    def collect(self, responses, previous=None):
        with patch.object(metrics, "fetch_json", side_effect=responses):
            return metrics.collect_citations(CONFIG, previous or {}, NOW)

    def test_higher_count_and_click_destination_for_both_scholars(self):
        for first, second, expected in [(101, 92, "googleScholar"), (101, 130, "semanticScholar")]:
            with self.subTest(winner=expected):
                result = self.collect([google(first), semantic(second)])
                self.assertEqual(result["value"], max(first, second))
                self.assertEqual(result["url"], result["sources"][expected]["url"])
                self.assertEqual(len(result["sources"]), 2)

    def test_tie_and_zero_are_valid(self):
        result = self.collect([google(0), semantic(0)])
        self.assertEqual(result["value"], 0)
        self.assertIn("scholar.google.com", result["url"])

    def test_compare_current_counts_after_profile_corrections(self):
        previous = self.collect([google(101), semantic(200)])
        current = self.collect([google(101), semantic(92)], {"citations": previous})
        self.assertEqual(current["value"], 101)
        self.assertIn("scholar.google.com", current["url"])

    def test_failed_source_keeps_its_count_date_and_destination(self):
        previous = self.collect([google(101), semantic(130)])
        current = self.collect([google(102), URLError("offline")], {"citations": previous})
        self.assertEqual(current["value"], 130)
        self.assertEqual(current["sources"]["semanticScholar"], previous["sources"]["semanticScholar"])
        self.assertEqual(current["url"], CONFIG["semanticScholarUrl"])

    def test_original_google_only_snapshot_migrates(self):
        previous = {"citations": {"value": 101, "updatedAt": NOW, "url": "https://scholar.google.com/citations?user=GNO0HzAAAAAJ"}}
        current = self.collect([URLError("offline"), semantic(92)], previous)
        self.assertEqual(current["value"], 101)
        self.assertEqual(current["sources"]["googleScholar"]["updatedAt"], NOW)

    def test_one_available_source_and_total_first_time_failure(self):
        result = self.collect([URLError("offline"), semantic(92)])
        self.assertEqual(result["value"], 92)
        self.assertEqual(result["url"], CONFIG["semanticScholarUrl"])
        with self.assertRaises(RuntimeError):
            self.collect([URLError("offline"), URLError("offline")])

    def test_wrong_author_and_invalid_count_cannot_replace_cached_data(self):
        previous = self.collect([google(101), semantic(130)])
        bad_semantic = dict(semantic(1000), authorId="someone-else")
        result = self.collect([google(-1), bad_semantic], {"citations": previous})
        self.assertEqual(result["value"], 130)

    def test_readme_link_switches_without_source_annotation(self):
        original = (Path(__file__).resolve().parents[2] / "README.md").read_text()
        updated = metrics.update_readme_citation_link(original, CONFIG["semanticScholarUrl"])
        start = updated.index("<!-- citations-badge:start -->")
        end = updated.index("<!-- citations-badge:end -->")
        block = updated[start:end]
        self.assertIn(f'href="{CONFIG["semanticScholarUrl"]}"', block)
        self.assertIn('alt="Citations"', block)
        self.assertNotIn('alt="Google Scholar', block)
        self.assertEqual(original.split("<!-- citations-badge:end -->")[1], updated.split("<!-- citations-badge:end -->")[1])


if __name__ == "__main__":
    unittest.main()

#!/usr/bin/env python3
# /// script
# requires-python = ">=3.12"
# dependencies = ["pyyaml>=6", "jinja2>=3"]
# ///
"""#1365: the `[factory]` tag is on its own line, and the matchers accept the old one-line form and the new one.
Run: uv run factory/tests/test_tag.py"""
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import effects  # noqa: E402
import gate_port  # noqa: E402
import reconcile  # noqa: E402
from tag import has_tag, same_text, tagged  # noqa: E402


class TagTest(unittest.TestCase):
    def test_tagged_puts_the_tag_alone_on_the_first_line(self):
        self.assertEqual(tagged("## Summary\nx"), "[factory]\n## Summary\nx")

    def test_frame_uses_the_helper_and_leaves_both_forms_alone(self):
        self.assertEqual(effects.frame("hi"), "[factory]\nhi")
        self.assertEqual(effects.frame("[factory]\nhi"), "[factory]\nhi")
        self.assertEqual(effects.frame("[factory] hi"), "[factory] hi")

    def test_has_tag_table(self):
        for body, rest, want in [
            ("[factory] recover: stage=ready", "recover:", True),
            ("[factory]\nrecover: stage=ready", "recover:", True),
            ("[factory]\r\n\r\nrecover: x", "recover:", True),
            ("[factory]", "", True),
            ("[factory] other", "recover:", False),
            ("[factory]\nother", "recover:", False),
            ("[factory]recover:", "recover:", False),
            ("[factoryX] recover:", "recover:", False),
            ("a person: [factory] recover:", "recover:", False),
            ("recover:", "recover:", False),
            ("", "", False),
            (None, "", False),
        ]:
            self.assertEqual(has_tag(body, rest), want, (body, rest))

    def test_recover_comment_filter_accepts_both_forms(self):
        for body in ("[factory] recover: a", "[factory]\nrecover: a", "[factory] reconcile: b", "[factory]\nreconcile: b",
                     "[factory] end failed: c", "[factory]\nend failed: c"):
            self.assertTrue(any(has_tag(body, p) for p in ("recover:", "reconcile:", "end failed:")), body)

    def test_reconcile_prefix_comments_are_tagged_and_matched_in_both_forms(self):
        new = tagged(f"{reconcile.PREFIX} stage=ready")
        self.assertTrue(new.startswith("[factory]\nreconcile:"))
        for body in (new, "[factory] reconcile: stage=ready"):
            self.assertTrue(has_tag(body, reconcile.PREFIX))

    def test_same_text_ignores_the_break_after_the_tag(self):
        for a, b, want in [("[factory] hi", "[factory]\nhi", True), ("[factory]\nhi", "[factory]\nhi", True),
                           ("[factory] hi", "[factory]\nho", False), ("hi", "hi", True), ("hi", "[factory] hi", False)]:
            self.assertEqual(same_text(a, b), want, (a, b))

    def test_review_summary_heading_starts_its_own_line(self):
        self.assertEqual(tagged("## Summary").splitlines()[1], "## Summary")


if __name__ == "__main__":
    unittest.main()

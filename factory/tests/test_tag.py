#!/usr/bin/env python3
# /// script
# requires-python = ">=3.12"
# dependencies = ["pyyaml>=6", "jinja2>=3"]
# ///
"""#1365: the `[factory]` tag is on its own line, and the matchers accept the old one-line form and the new one.
Run: uv run factory/tests/test_tag.py"""
import sys
import unittest
from unittest.mock import patch
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import effects  # noqa: E402
import gate_port  # noqa: E402
import reconcile  # noqa: E402
import config  # noqa: E402
import tag  # noqa: E402
from tag import has_tag, same_text, tagged, untag  # noqa: E402


class TagTest(unittest.TestCase):
    def test_tagged_puts_the_tag_alone_on_the_first_line(self):
        self.assertEqual(tagged("## Summary\nx"), "[factory]\n## Summary\nx")

    def test_frame_uses_the_helper_and_leaves_both_forms_alone(self):
        self.assertEqual(effects.frame("hi"), "[factory]\nhi")
        self.assertEqual(effects.frame("[factory]\nhi"), "[factory]\nhi")
        for old in ("[factory] hi", "[factory]  hi", "[factory]\r\nhi", "[factory]\n\nhi"):
            self.assertEqual(effects.frame(old).splitlines()[0], "[factory]", old)
            self.assertEqual(effects.frame(old).split("\n", 1)[1].strip(), "hi", old)
        self.assertEqual(effects.frame("[factory] ## Summary"), "[factory]\n## Summary")

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
                           ("[factory] hi", "[factory]\nho", False), ("hi", "hi", True), ("hi", "ho", False)]:
            self.assertEqual(same_text(a, b), want, (a, b))

    def test_review_summary_heading_starts_its_own_line(self):
        self.assertEqual(tagged("## Summary").splitlines()[1], "## Summary")


class ConfiguredTagTest(unittest.TestCase):
    """#1366: `comment_tag` unset, a custom string or blank, on a comment, a review summary and a PR body."""

    def tearDown(self):
        tag.configure(None)

    def test_surfaces_by_setting(self):
        for setting, first in [(None, "[factory]"), ("[eval-bot]", "[eval-bot]"), ("", None), ("  ", None)]:
            tag.configure(setting)
            for surface, text in [("comment", "hi"), ("review", "## Summary\nok"), ("PR body", "## Summary\nx")]:
                body = effects.frame(text, "r1")
                lines = body.splitlines()
                self.assertEqual(lines[0], first if first else text.splitlines()[0], (setting, surface))
                self.assertEqual(lines[1 if first else 0:][:len(text.splitlines())], text.splitlines(), (setting, surface))
                self.assertTrue(body.endswith("Factory-Run: r1"), (setting, surface))
                self.assertEqual(effects.frame(body, "r2"), body, "framing twice changes nothing")

    def test_review_body_from_the_github_gate(self):
        from test_gate_github import FakeGh, eff, tr
        for setting, head in [(None, "[factory]\nClose."), ("[eval-bot]", "[eval-bot]\nClose."), ("", "Close.")]:
            real = config._load  # the gate loads the config, which sets the tag: give it the setting
            gh = FakeGh()
            with patch.object(config, "_load", lambda: {**real(), **({} if setting is None else {"comment_tag": setting})}):
                config.load()  # the gate's entry point loads the config, which sets the tag
                tr(gh).apply(eff("review", "request-changes-at-head", focus="full", round=1, summary="Close.", findings=[], run_id="r9"))
            self.assertEqual(gh.writes[0][-1].removeprefix("body=").split("\n\n")[0], head, setting)

    def test_own_comments_are_found_for_every_tag_and_every_form(self):
        kinds = ("recover:", "reconcile:", "end failed:")
        old = [f"[factory] {k} stage=ready" for k in kinds] + [f"[factory]\n{k} stage=ready" for k in kinds]
        for setting in (None, "[eval-bot]", ""):
            tag.configure(setting)
            new = [tagged(f"{k} stage=ready") for k in kinds]
            for body in new + old:
                self.assertTrue(any(has_tag(body, k) for k in kinds), (setting, body))
            for k in kinds:  # each kind finds its own and no other
                self.assertTrue(has_tag(tagged(f"{k} x"), k))
                self.assertFalse(any(has_tag(tagged(f"{k} x"), o) for o in kinds if o != k), (setting, k))
            for other in ("Stopped: x", "a person says recover: x", "", None):
                self.assertFalse(any(has_tag(other, k) for k in kinds), (setting, other))

    def test_a_comment_posted_under_another_tag_is_found_by_its_marker(self):
        tag.configure("[old-bot]")
        posted = tagged("end failed: node=a head=b")
        tag.configure("")
        self.assertTrue(has_tag(posted, "end failed:"))
        self.assertFalse(posted.startswith("end failed:") and "factory:kind end-failed" not in posted)

    def test_tag_equal_to_a_message_start_keeps_the_message(self):
        kinds = ("recover:", "reconcile:", "end failed:")
        for setting in kinds + ("recover", "end", "Stopped:"):
            tag.configure(setting)
            for k in kinds:
                body = tagged(f"{k} stage=ready")
                self.assertTrue(has_tag(body, k), (setting, k, body))
                self.assertIn(f"{k} stage=ready", untag(body), (setting, k))
                self.assertIn(f"factory:kind {k.rstrip(':').replace(' ', '-')}", body, (setting, k))
                self.assertFalse(has_tag(f"{k} stage=ready", k), (setting, k, "untagged person text"))
                self.assertEqual(tagged(body), body, (setting, k, "tagging twice changes nothing"))

    def test_blank_tag_matches_untagged_text_only_for_a_given_start(self):
        tag.configure("")
        self.assertTrue(has_tag("anything"))
        self.assertFalse(has_tag("anything", "recover:"))
        self.assertTrue(same_text(tagged("hi"), "[factory] hi"))

    def test_reconcile_end_failed_lookup_for_every_tag(self):
        for setting in (None, "[eval-bot]", ""):
            tag.configure(setting)
            facts = {"recover_comments": [tagged("end failed: node=dev head=abc outcome=done\n\n```\nboom\n```\n\n"
                                                 "The outcome did not land; retry."), "[factory] end failed: node=dev head=zzz x"]}
            self.assertIn("boom", reconcile.last_end_failure(facts, "dev", "abc") or "", setting)
            self.assertIsNone(reconcile.last_end_failure(facts, "dev", "nope"), setting)


if __name__ == "__main__":
    unittest.main()

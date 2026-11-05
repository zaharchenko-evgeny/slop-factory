#!/usr/bin/env python3
# /// script
# requires-python = ">=3.12"
# dependencies = ["pyyaml>=6"]
# ///
"""Tests for the `@factory:<persona>` parser (ADR-0020). Run: uv run factory/tests/test_mentions.py"""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from mentions import parse  # noqa: E402


class ParseTest(unittest.TestCase):
    def test_single_mention_takes_the_rest_of_the_comment(self):
        self.assertEqual(parse("@factory:dev please fix\nthe test"), [("dev", "please fix\nthe test")])

    def test_each_mention_takes_the_text_up_to_the_next(self):
        self.assertEqual(parse("hi @factory:dev do x\n@factory:rex-2 and review"), [("dev", "do x"), ("rex-2", "and review")])

    def test_code_span_and_fence_are_ignored(self):
        body = "write `@factory:dev` as is\n```\n@factory:rex run\n```\n~~~\n@factory:ria\n~~~\n@factory:dev real"
        self.assertEqual(parse(body), [("dev", "real")])

    def test_unclosed_fence_hides_the_rest(self):
        self.assertEqual(parse("```\n@factory:dev x"), [])

    def test_handle_inside_a_word_is_ignored(self):
        self.assertEqual(parse("mail foo@factory:dev now"), [])


if __name__ == "__main__":
    unittest.main()

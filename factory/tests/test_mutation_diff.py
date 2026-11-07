#!/usr/bin/env python3
# /// script
# requires-python = ">=3.12"
# ///
"""Tests for scripts/mutation-diff.py. Run: uv run factory/tests/test_mutation_diff.py"""
import ast
import importlib.util
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
SCRIPT = REPO / "scripts/mutation-diff.py"
spec = importlib.util.spec_from_file_location("mutation_diff", SCRIPT)
mutation_diff = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mutation_diff)

DIFF = """\
diff --git a/factory/a.py b/factory/a.py
--- a/factory/a.py
+++ b/factory/a.py
@@ -1,0 +2,2 @@
+x
+y
@@ -9 +11 @@
-old
+new
diff --git a/factory/tests/test_a.py b/factory/tests/test_a.py
--- a/factory/tests/test_a.py
+++ b/factory/tests/test_a.py
@@ -1 +1 @@
-a
+b
diff --git a/README.md b/README.md
--- a/README.md
+++ b/README.md
@@ -1 +1 @@
-a
+b
diff --git a/factory/gone.py b/factory/gone.py
--- a/factory/gone.py
+++ /dev/null
@@ -1,2 +0,0 @@
-a
-b
"""


def described(source, lines):
    return [(line, text) for _, _, text, line in mutation_diff.sites(ast.parse(source), lines)]


class ChangedLinesTest(unittest.TestCase):
    def test_selects_added_and_modified_lines_of_engine_sources_only(self):
        self.assertEqual(mutation_diff.changed_lines(DIFF), {"factory/a.py": {2, 3, 11}})


class OperatorTest(unittest.TestCase):
    def test_each_operator_on_its_line_only(self):
        source = "def f(a, b):\n    if a < 1 and b:\n        return True\n    return a + 0\n"
        self.assertEqual(described(source, {2}), [
            (2, "and -> or"), (2, "comparison Lt -> GtE"), (2, "1 -> 2"), (2, "1 -> 0")])
        self.assertEqual(described(source, {3}), [(3, "return value -> None"), (3, "True -> False")])
        self.assertEqual(described(source, {1}), [])

    def test_return_none_has_no_mutant(self):
        self.assertEqual(described("def f():\n    return None\n", {2}), [])

    def test_mutate_changes_only_the_chosen_site(self):
        tree = ast.parse("a = x == 1 or y == 2\n")
        found = mutation_diff.sites(tree, {1})
        by_text = {text: mutation_diff.mutate(tree, i, v) for i, v, text, _ in found}
        self.assertEqual(by_text["or -> and"], "a = x == 1 and y == 2")
        self.assertEqual(by_text["comparison Eq -> NotEq"].count("!="), 1)
        self.assertEqual(by_text["2 -> 3"], "a = x == 1 or y == 3")


class EndToEndTest(unittest.TestCase):
    def git(self, root, *args):
        subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@example.invalid", *args],
                       cwd=root, check=True, capture_output=True)

    def run_script(self, root, base, test):
        return subprocess.run([sys.executable, str(SCRIPT), "--base", base, "--test-cmd", test],
                              cwd=root, capture_output=True, text=True)

    def test_untested_if_survives_and_tested_one_is_caught(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "factory").mkdir()
            (root / "factory/m.py").write_text("def f(a):\n    return a\n")
            self.git(root, "init", "-q", "-b", "main")
            self.git(root, "add", "-A")
            self.git(root, "commit", "-q", "-m", "base")
            (root / "factory/m.py").write_text(
                "def f(a):\n    if a > 5:\n        a = 0\n    return a\n")
            self.git(root, "commit", "-qam", "change")
            # The check passes whatever the code does: every mutant on the new lines survives.
            result = self.run_script(root, "HEAD~1", f"{sys.executable} -c pass")
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("factory/m.py:2: comparison Gt -> LtE", result.stdout)
            # A check that reads the code catches the comparison mutant.
            check = f"{sys.executable} -c \"import sys; sys.path.insert(0, 'factory'); import m; assert m.f(9) == 0 and m.f(1) == 1\""
            result = self.run_script(root, "HEAD~1", check)
            self.assertNotIn("comparison", result.stdout)
            self.assertEqual((root / "factory/m.py").read_text().count("a > 5"), 1)

    def test_cannot_run_exits_non_zero(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.git(root, "init", "-q", "-b", "main")
            result = self.run_script(root, "no-such-ref", f"{sys.executable} -c pass")
            self.assertEqual(result.returncode, 1)
            self.git(root, "commit", "-q", "--allow-empty", "-m", "base")
            result = self.run_script(root, "HEAD", f"{sys.executable} -c 'raise SystemExit(1)'")
            self.assertEqual(result.returncode, 1)


if __name__ == "__main__":
    unittest.main()

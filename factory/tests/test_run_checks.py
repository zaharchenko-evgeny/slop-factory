#!/usr/bin/env python3
# /// script
# requires-python = ">=3.12"
# dependencies = ["pyyaml>=6"]
# ///
import contextlib
import io
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import run_checks

CFG = {"checks": [
    {"name": "a", "mode": "blocking", "run": "true"},
    {"name": "adv", "mode": "advisory", "run": "false"},
    {"name": "b", "mode": "blocking", "run": "false"},
    {"name": "c", "mode": "blocking", "run": "true"},
]}


class RunChecks(unittest.TestCase):
    def test_blocking_only_stops_at_first_failure(self):
        failed, rows, _ = run_checks.run_checks(CFG, ".", "main", "blocking", stop=True)
        self.assertTrue(failed)
        self.assertEqual([(r["name"], r["status"]) for r in rows], [("a", "pass"), ("b", "fail")])

    def test_all_modes_run_every_check(self):
        _, rows, _ = run_checks.run_checks(CFG, ".", "main")
        self.assertEqual([r["name"] for r in rows], ["a", "adv", "b", "c"])

    def main(self, checks, *args):
        import yaml
        with tempfile.TemporaryDirectory() as d:
            (Path(d) / "factory.yaml").write_text(yaml.safe_dump({"repositories": [{}], "checks": checks}))
            old, run_checks.ROOT = run_checks.ROOT, Path(d)
            out = io.StringIO()
            try:
                with contextlib.redirect_stdout(out), contextlib.redirect_stderr(io.StringIO()):
                    code = run_checks.main(list(args))
            finally:
                run_checks.ROOT = old
        return code, out.getvalue()

    def test_main_exit_code_and_rows(self):
        code, out = self.main(CFG["checks"], "--mode", "blocking")
        self.assertEqual((code, out), (1, "a: pass\nb: fail\n"))

    def test_main_strict_fails_a_skipped_blocking_check(self):
        checks = [{"name": "s", "mode": "blocking", "requires": "no-such-tool-xyz", "run": "true"}]
        self.assertEqual(self.main(checks, "--mode", "blocking")[0], 0)
        self.assertEqual(self.main(checks, "--mode", "blocking", "--strict")[0], 1)


if __name__ == "__main__":
    unittest.main()

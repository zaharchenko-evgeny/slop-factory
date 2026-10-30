#!/usr/bin/env python3
# /// script
# requires-python = ">=3.12"
# dependencies = ["pyyaml>=6"]
# ///
"""Run the `checks` of factory.yaml: the one list for the factory driver and GitHub CI.

Usage: run_checks.py [--mode blocking|advisory] [--only NAME ...] [--strict]
Prints each check's name and result; exits 1 at the first failing blocking check.
"""
import argparse
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent


def run_checks(config, cwd, base, mode=None, only=None, stop=False):
    """Run the pre-review checks in `cwd`: (a blocking one failed, rows, markdown report)."""
    rows, report, failed = [], [], False
    for c in config.get("checks") or []:
        if c.get("stage", "pre-review") != "pre-review" or (mode and c.get("mode") != mode) \
                or (only is not None and c["name"] not in only):
            continue
        if c.get("requires") and not shutil.which(c["requires"]):
            status, text = "skipped", f"{c['requires']} is not installed"
        else:
            r = subprocess.run(["bash", "-c", c["run"]], cwd=cwd, capture_output=True, text=True,
                               env={**os.environ, "FACTORY_BASE": base})
            status, text = ("pass" if r.returncode == 0 else "fail"), r.stdout + r.stderr
        failed |= status == "fail" and c.get("mode") == "blocking"
        skips = re.findall(r"skipped=(\d+)|^.*\.\.\. skipped '(.*)'$", text, re.M)
        if skips:  # a passing check that skipped tests (e.g. a missing tool) did not run them all
            text = f"NOTE: {len(skips)} skip report(s) in the output, those tests did not run\n" + text
        rows.append({"name": c["name"], "mode": c.get("mode"), "status": status})
        lines = text.rstrip().splitlines()
        tail = "\n".join(lines[:1] * bool(skips) + lines[-15:])
        report.append(f"## {c['name']} ({c.get('mode')}): {status}\n~~~~\n{tail}\n~~~~\n")
        if stop and failed:
            break
    return failed, rows, "\n".join(report)


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--mode", choices=["blocking", "advisory"])
    ap.add_argument("--only", nargs="*")
    ap.add_argument("--strict", action="store_true", help="a skipped blocking check fails too (CI)")
    a = ap.parse_args(argv)
    config = yaml.safe_load((ROOT / "factory.yaml").read_text())
    base = os.environ.get("FACTORY_BASE") or config["repositories"][0].get("default_branch", "main")
    failed, rows, report = run_checks(config, ROOT, base, a.mode, a.only, stop=True)
    for r in rows:
        print(f"{r['name']}: {r['status']}")
    failed |= a.strict and any(r["status"] == "skipped" and r["mode"] == "blocking" for r in rows)
    if failed:
        print("\n" + report, file=sys.stderr)
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())

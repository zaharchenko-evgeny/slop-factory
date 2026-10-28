#!/usr/bin/env python3
# /// script
# requires-python = ">=3.12"
# dependencies = ["pyyaml>=6"]
# ///
"""Model preflight: one short `pi -p` call per model must get a reply.

Usage: preflight.py [MODEL ...]. Without arguments it checks every model a role
of factory.yaml uses. A model passes on exit 0 with a non-empty reply that is
not an error ("OK", "OK." and "ok" all pass); exit 1 lists the failures.
"""
import os
import re
import subprocess
import sys
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent


def reply_ok(returncode, reply):
    reply = reply.strip()
    return returncode == 0 and bool(reply) and not re.match(r"(?i)error\b", reply)


def run_models(config):
    tiers = {r[k] for r in config["roles"].values() for k in ("tier", "tier_high_risk") if k in r}
    return sorted({config["models"][t] for t in tiers})


def check(model):
    r = subprocess.run([os.environ.get("FACTORY_AGENT", "pi"), "-p", "--no-mcp", "--no-session", "--no-tools",
                        "--model", model, "Reply with OK."], capture_output=True, text=True)
    return reply_ok(r.returncode, r.stdout), (r.stdout.strip() or r.stderr.strip())[:200]


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    models = argv or run_models(yaml.safe_load((ROOT / "factory.yaml").read_text()))
    failed = 0
    for model in models:
        ok, reply = check(model)
        failed += not ok
        print(f"{'ok' if ok else 'FAIL'} {model}: {reply!r}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())

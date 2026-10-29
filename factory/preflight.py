#!/usr/bin/env python3
# /// script
# requires-python = ">=3.12"
# dependencies = ["pyyaml>=6"]
# ///
"""Model preflight: one short call per (harness, model) of the roles must get a reply.

Usage: preflight.py [MODEL ...]. Without arguments it checks every harness and model a role
of factory.yaml uses; models given on the command line run on pi. A stub harness (junie) fails with one line. A model passes on exit 0 with a non-empty reply that is
not an error ("OK", "OK." and "ok" all pass), on the first call or one retry; exit 1 lists the failures.
With `targets.local.model_access: central` every call goes through the JetBrains Central proxy
(factory/model_access.py); a proxy that cannot be started fails with one line.
"""
import os
import re
import subprocess
import sys
from pathlib import Path

import yaml

import harnesses
import model_access

ROOT = Path(__file__).resolve().parent.parent


def reply_ok(returncode, reply):
    reply = reply.strip()
    return returncode == 0 and bool(reply) and not re.match(r"(?i)error\b", reply)


def run_pairs(config):
    """Sorted (harness, model) of every role and reviewer mode."""
    pairs = set()
    for role, r in config["roles"].items():
        for mode, spec in [(None, r), *(r.get("modes") or {}).items()]:
            h = harnesses.harness_of(config, role, mode)
            pairs |= {(h, config["models"][spec[k]]) for k in ("tier", "tier_high_risk", "fallback") if k in spec}
    return sorted(pairs)


def run_models(config):
    return sorted({m for _, m in run_pairs(config)})


def check(model, tries=2, env=None, harness="pi"):
    """(ok, reply); a failed call is retried once, since one empty reply can be transient."""
    if harness in harnesses.STUBS:
        return False, harnesses.STUBS[harness]
    cmd = harnesses.adapter(harness).probe(model)
    for _ in range(tries):
        r = subprocess.run(cmd, capture_output=True, text=True, env={**os.environ, **(env or {})})
        if reply_ok(r.returncode, r.stdout):
            break
    return reply_ok(r.returncode, r.stdout), (r.stdout.strip() or r.stderr.strip())[:200]


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    config = yaml.safe_load((ROOT / "factory.yaml").read_text())
    pairs = [("pi", m) for m in argv] or run_pairs(config)
    try:
        env = model_access.pi_env(config, ROOT)
    except model_access.ModelAccessError as e:
        print(f"FAIL model access: {e}")
        return 1
    failed = 0
    for harness, model in pairs:
        ok, reply = check(model, env=env, harness=harness)
        failed += not ok
        print(f"{'ok' if ok else 'FAIL'} {model}: {reply!r}" if harness == "pi" else
              f"{'ok' if ok else 'FAIL'} {harness} {model}: {reply!r}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())

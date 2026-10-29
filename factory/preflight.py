#!/usr/bin/env python3
# /// script
# requires-python = ">=3.12"
# dependencies = ["pyyaml>=6"]
# ///
"""Model preflight: one short `pi -p` call per model must get a reply.

Usage: preflight.py [MODEL ...]. Without arguments it checks every model a role
of factory.yaml uses. A model passes on exit 0 with a non-empty reply that is
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

import model_access

ROOT = Path(__file__).resolve().parent.parent


def reply_ok(returncode, reply):
    reply = reply.strip()
    return returncode == 0 and bool(reply) and not re.match(r"(?i)error\b", reply)


def run_models(config):
    specs = [s for r in config["roles"].values() for s in [r, *(r.get("modes") or {}).values()]]
    tiers = {s[k] for s in specs for k in ("tier", "tier_high_risk", "fallback") if k in s}
    return sorted({config["models"][t] for t in tiers})


def check(model, tries=2, env=None):
    """(ok, reply); a failed call is retried once, since one empty reply can be transient."""
    for _ in range(tries):
        r = subprocess.run([os.environ.get("FACTORY_AGENT", "pi"), "-p", "--no-mcp", "--no-session", "--no-tools",
                            "--model", model, "Reply with OK."], capture_output=True, text=True,
                           env={**os.environ, **(env or {})})
        if reply_ok(r.returncode, r.stdout):
            break
    return reply_ok(r.returncode, r.stdout), (r.stdout.strip() or r.stderr.strip())[:200]


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    config = yaml.safe_load((ROOT / "factory.yaml").read_text())
    models = argv or run_models(config)
    try:
        env = model_access.pi_env(config, ROOT)
    except model_access.ModelAccessError as e:
        print(f"FAIL model access: {e}")
        return 1
    failed = 0
    for model in models:
        ok, reply = check(model, env=env)
        failed += not ok
        print(f"{'ok' if ok else 'FAIL'} {model}: {reply!r}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())

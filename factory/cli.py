#!/usr/bin/env python3
# /// script
# requires-python = ">=3.12"
# dependencies = ["pyyaml>=6"]
# ///
"""The `factory` command: check, apply, sweep (the gate), deploy, chart, validate.

Usage: factory [--team DIR] <subcommand> [args]; `uv run factory/gate.py ...` works as before.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import config  # noqa: E402
import deploy  # noqa: E402
import gate  # noqa: E402
import nodes  # noqa: E402


def validate():
    errs = config.validate()
    if not errs:
        errs = nodes.validate(nodes.load(), config.load())
    print("\n".join(errs) or "factory: ok")
    return 1 if errs else 0


def main(argv=None):
    argv = config.pop_team(sys.argv[1:] if argv is None else argv)
    cmd, rest = (argv[0], argv[1:]) if argv else ("", [])
    if cmd in ("check", "apply", "sweep"):
        return gate.main([cmd, *rest])
    if cmd == "deploy":
        return deploy.main(rest)
    if cmd == "chart":
        return nodes.main(["chart", *rest])
    if cmd == "validate" and not rest:
        return validate()
    print(__doc__)
    return 2


if __name__ == "__main__":
    sys.exit(main())

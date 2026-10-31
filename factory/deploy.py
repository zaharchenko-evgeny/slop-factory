#!/usr/bin/env python3
# /// script
# requires-python = ">=3.12"
# dependencies = ["pyyaml>=6"]
# ///
"""Plan a deployment of the factory.

CLI: deploy.py plan --target local   (prints the launchd job for `loop.py --once` as JSON)
Apply it with `uv run factory/schedule_local.py install`."""
import json
import sys
from pathlib import Path

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent))
import schedule_local  # noqa: E402


def plan(config, target, root=schedule_local.ROOT):
    if target != "local":
        raise SystemExit(f"deploy.py: unsupported target {target!r}")
    return {"target": "local", "launchd": schedule_local.render(config, root)}


def main(argv):
    if argv[:1] != ["plan"] or argv[1:2] != ["--target"] or len(argv) != 3:
        print(__doc__.split("CLI: ")[1])
        return 2
    config = yaml.safe_load((schedule_local.ROOT / "factory.yaml").read_text())
    print(json.dumps(plan(config, argv[2]), indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))

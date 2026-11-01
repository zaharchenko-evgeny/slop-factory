#!/usr/bin/env python3
# /// script
# requires-python = ">=3.12"
# dependencies = ["pyyaml>=6"]
# ///
"""The `factory` command: begin, end (an agent node's run), check, apply, sweep (the gate), deploy, chart, validate.

Usage: factory [--team DIR] <subcommand> [args]; `uv run factory/gate.py ...` works as before.
       factory begin <automation> [payload file]   (payload on stdin by default; exit 10: the node must not act)
       factory end <handoff_path>
       factory sweep --main ...                    (the gate's sweep from main's graph)
"""
import argparse
import contextlib
import io
import json
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import config  # noqa: E402
import deploy  # noqa: E402
import gate  # noqa: E402
import nodes  # noqa: E402


STATE = Path(".factory/begin.json")


def from_main():
    """Use main's descriptor, graph and role overrides, never the pull request's (until #213 pins an engine)."""
    root = tempfile.mkdtemp(prefix="factory-main-") + "/main"
    for cmd in (["fetch", "origin", "main"], ["worktree", "add", "--detach", root, "origin/main"]):
        subprocess.run(["git", *cmd], check=True, capture_output=True)
    config.set_team(root)
    return root


def run_gate(engine, argv):
    """The gate, as main's own executable when `engine` is a main worktree, so a pull request cannot replace it."""
    if not engine:
        return gate.main(argv)
    done = subprocess.run([sys.executable, str(Path(engine) / "factory/gate.py"), "--team", engine, *argv],
                          stdout=subprocess.PIPE, text=True)
    print(done.stdout, end="")  # through sys.stdout, so callers can capture it
    return done.returncode


def gate_args(state, *more):
    sel = [x for k in ("focus", "tier") for x in (f"--{k}", state[k]) if state[k]]
    return [*sel, "--spec", state["spec"], *state["tracker"], *more]


def begin(argv):
    p = argparse.ArgumentParser(prog="factory begin")
    p.add_argument("automation")
    p.add_argument("payload", nargs="?")
    p.add_argument("--tracker")  # tests: a memory tracker
    a = p.parse_args(argv)
    engine = from_main() if a.automation.split("-")[0] in deploy.PR_TRIGGERED_NODES else None
    try:
        compiled = deploy.compile_automation(a.automation, nodes.load(), config.load())
    except KeyError as e:
        print(json.dumps({"error": e.args[0]}))
        return 2
    base = Path(".factory")
    (base / "event").mkdir(parents=True, exist_ok=True)
    (base / "spec").mkdir(exist_ok=True)
    event, spec = base / "event" / f"{a.automation}.json", base / "spec" / f"{a.automation}.json"
    event.write_text(Path(a.payload).read_text() if a.payload else sys.stdin.read())
    spec.write_text(json.dumps(compiled["spec"], sort_keys=True))
    state = {**{k: compiled[k] for k in ("node", "focus", "tier")}, "spec": str(spec), "event": str(event),
             "tracker": ["--tracker", a.tracker] if a.tracker else [], "team": str(config.team_root()),
             "engine": engine}
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        code = run_gate(engine, ["check", "--node", state["node"], "--event", state["event"], *gate_args(state)])
    print(out.getvalue(), end="")
    if code == 0:
        state["handoff_path"] = json.loads(out.getvalue())["handoff_path"]
        STATE.write_text(json.dumps(state))
    return code


def end(argv):
    if len(argv) != 1 or not STATE.exists():
        print(json.dumps({"error": "end needs <handoff_path> and a prior `factory begin`"}))
        return 2
    state = json.loads(STATE.read_text())
    if argv[0] != state["handoff_path"]:
        print(json.dumps({"error": f"handoff path must be {state['handoff_path']}"}))
        return 2
    config.set_team(state["team"])
    return run_gate(state["engine"], ["apply", "--node", state["node"], "--event", state["event"], "--handoff", argv[0],
                      *gate_args(state)])


def validate():
    errs = config.validate()
    if not errs:
        errs = nodes.validate(nodes.load(), config.load())
    print("\n".join(errs) or "factory: ok")
    return 1 if errs else 0


def main(argv=None):
    argv = config.pop_team(sys.argv[1:] if argv is None else argv)
    cmd, rest = (argv[0], argv[1:]) if argv else ("", [])
    if cmd == "begin":
        return begin(rest)
    if cmd == "end":
        return end(rest)
    if cmd == "sweep" and "--main" in rest:
        return run_gate(from_main(), ["sweep", *[x for x in rest if x != "--main"]])
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

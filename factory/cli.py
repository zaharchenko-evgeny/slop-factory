#!/usr/bin/env python3
# /// script
# requires-python = ">=3.12"
# dependencies = ["pyyaml>=6"]
# ///
"""The `factory` command: begin, end (an agent node's run), check, apply, sweep (the gate), deploy, chart, validate, doctor.

Usage: factory [--team DIR] <subcommand> [args]; `uv run factory/gate.py ...` works as before.
       factory begin <automation> --issue N | --pr N | [payload file]   (payload on stdin by default; exit 10: the node must not act)
       factory end <outcome> [--findings F.json] [--slices S.json] [--checks C.json] [--reason TEXT]
                   [--summary TEXT] [--size-estimate N] [--addressed A.json] [--disputed-focuses a,b]
                   [--notes notes.json]
                   (the outcome and the data its effects use)
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

HERE = Path(__file__).resolve().parent
# Engine modules (config, deploy, gate, nodes) are imported by load_engine(), never at module level: a pull-request
# node must import them from main's worktree, because importing runs module-level code with the node's credentials.
config = deploy = gate = nodes = None
# Mirrors deploy.PR_TRIGGERED_NODES (a test keeps them equal); it cannot be read from deploy before the engine is chosen.
PR_NODES = {"review", "join", "fix", "update", "finish", "reopen"}

STATE = Path(".factory/begin.json")


def load_engine(factory_dir):
    """Import the engine modules from `factory_dir` (the checkout's own, or main's worktree for a pull-request node)."""
    global config, deploy, gate, nodes
    sys.path.insert(0, str(factory_dir))
    import config as config_, deploy as deploy_, gate as gate_, nodes as nodes_
    config, deploy, gate, nodes = config_, deploy_, gate_, nodes_


def from_main():
    """Use main's descriptor, graph and role overrides, never the pull request's (until #213 pins an engine)."""
    shared = Path.home() / "factory-main"
    if shared.is_dir():
        return str(shared)
    root = tempfile.mkdtemp(prefix="factory-main-") + "/main"
    for cmd in (["fetch", "origin", "main"], ["worktree", "add", "--detach", root, "FETCH_HEAD"]):
        subprocess.run(["git", *cmd], check=True, capture_output=True)
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


def begin(argv, team=None):
    p = argparse.ArgumentParser(prog="factory begin")
    p.add_argument("automation")
    p.add_argument("payload", nargs="?")
    p.add_argument("--issue", type=int)
    p.add_argument("--pr", type=int)
    p.add_argument("--main", action="store_true")
    p.add_argument("--tracker")  # tests: a memory tracker
    a = p.parse_args(argv)
    engine = from_main() if a.main or a.automation.split("-")[0] in PR_NODES else None
    load_engine(Path(engine) / "factory" if engine else HERE)
    if engine or team:
        config.set_team(engine or team)
    try:
        compiled = deploy.compile_automation(a.automation, nodes.load(), config.load())
    except KeyError as e:
        print(json.dumps({"error": e.args[0]}))
        return 2
    base = Path(".factory")
    (base / "event").mkdir(parents=True, exist_ok=True)
    (base / "spec").mkdir(exist_ok=True)
    event, spec = base / "event" / f"{a.automation}.json", base / "spec" / f"{a.automation}.json"
    if a.issue or a.pr:  # a number is all the gate needs: it reads every fact from the tracker
        name, _, action = str(nodes.load()["nodes"][compiled["node"]]["triggers"][0]).split(" ")[0].partition(".")
        number = {"pull_request" if a.pr else "issue": {"number": a.pr or a.issue}}
        text = json.dumps({"eventType": f"{name}.{action}", "action": action, **number})
    else:
        text = Path(a.payload).read_text() if a.payload else sys.stdin.read()
    event.write_text(text)
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


def handoff_of(a):
    """The handoff the options name: the outcome, plus the data file or text each option carries."""
    data = {"outcome": a.outcome}
    for key, path in (("findings", a.findings), ("slices", a.slices), ("checks", a.checks)):
        if path:
            data[key] = json.loads(Path(path).read_text())
    data.update({k: v for k, v in (("reason", a.reason), ("summary", a.summary), ("size_estimate", a.size_estimate)) if v})
    if a.addressed:
        data["findings_addressed"] = json.loads(Path(a.addressed).read_text())
    if a.notes:
        data["agent_notes"] = json.loads(Path(a.notes).read_text())
    if a.disputed_focuses:
        data["disputed_focuses"] = a.disputed_focuses.split(",")
    return data


def end(argv, team=None):
    p = argparse.ArgumentParser(prog="factory end")
    p.add_argument("outcome")
    for opt in ("findings", "slices", "checks", "reason", "summary", "size-estimate", "addressed", "disputed-focuses", "notes"):
        p.add_argument(f"--{opt}")
    try:
        a = p.parse_args(argv)
    except SystemExit:
        return 2
    if not STATE.exists():
        print(json.dumps({"error": "end needs a prior `factory begin`"}))
        return 2
    state = json.loads(STATE.read_text())
    load_engine(Path(state["engine"]) / "factory" if state["engine"] else HERE)
    handoff = Path(state["handoff_path"])
    try:
        data = handoff_of(a)
    except (OSError, ValueError) as e:
        print(json.dumps({"error": f"malformed option file: {e}"}))
        return 2
    handoff.parent.mkdir(parents=True, exist_ok=True)
    handoff.write_text(json.dumps(data))
    config.set_team(state["team"])
    return run_gate(state["engine"], ["apply", "--node", state["node"], "--event", state["event"], "--handoff",
                                      str(handoff), *gate_args(state)])


def validate():
    errs = config.validate()
    if not errs:
        errs = nodes.validate(nodes.load(), config.load())
    print("\n".join(errs) or "factory: ok")
    return 1 if errs else 0


def pop_team(argv):
    """Strip `--team DIR` without the engine modules; returns (DIR or None, the remaining arguments)."""
    argv = list(argv)
    if "--team" not in argv:
        return None, argv
    i = argv.index("--team")
    team = argv[i + 1] if i + 1 < len(argv) else None
    del argv[i:i + 2]
    return team, argv


def main(argv=None):
    team, argv = pop_team(sys.argv[1:] if argv is None else argv)
    cmd, rest = (argv[0], argv[1:]) if argv else ("", [])
    if cmd == "sweep" and "--main" in rest:
        engine = from_main()
        load_engine(Path(engine) / "factory")
        config.set_team(engine)
        return run_gate(engine, ["sweep", *[x for x in rest if x != "--main"]])
    if cmd == "end":
        return end(rest, team)
    if cmd == "begin":
        return begin(rest, team)
    load_engine(HERE)
    if team:
        config.set_team(team)
    if cmd in ("check", "apply", "sweep"):
        return gate.main([cmd, *rest])
    if cmd == "deploy":
        return deploy.main(rest)
    if cmd == "chart":
        return nodes.main(["chart", *rest])
    if cmd == "validate" and not rest:
        return validate()
    if cmd == "doctor":
        from doctor import main as doctor_main
        return doctor_main(rest)
    if cmd == "reconcile":
        from reconcile import main as reconcile_main
        return reconcile_main(rest)
    if cmd == "notify":
        from notify import main as notify_main
        return notify_main(rest)
    print(__doc__)
    return 0 if cmd in ("-h", "--help") else 2


if __name__ == "__main__":
    sys.exit(main())

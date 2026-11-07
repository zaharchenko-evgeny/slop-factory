#!/usr/bin/env python3
# /// script
# requires-python = ">=3.12"
# dependencies = ["pyyaml>=6", "jinja2>=3"]
# ///
"""The `factory` command: begin, end (an agent node's run), check, apply, sweep (the gate), deploy, contracts (operator-only), chart, validate, setup, doctor.

Usage: factory [--team DIR] <subcommand> [args]; `uv run factory/gate.py ...` works as before.
       factory begin <automation> --issue N | --pr N | [payload file]   (payload on stdin by default; exit 10: the node must not act)
       factory end <outcome> [--reason TEXT] [--summary TEXT] [--reply TEXT] [--size-estimate N] [--disputed-focuses a,b]
                   [--pass CHECK] [--fail CHECK] [--not-run 'CHECK | REASON'] [--propose-check CHECK]
                   [--blocking 'ID FILE FINDING | case: REPRO'] [--minor 'ID FILE FINDING']
                   [--fixed 'ID SHA'] [--disputed 'ID REASON'] [--slice FILE.md]
                   [--problem TEXT] [--link 'TITLE | URL'] [--option TEXT] [--recommended INDEX]
                   [--note-for ROLE] [--note-hint TEXT] [--note-repro CMD] [--note-file PATH]
                   (the outcome and the data its effects use; repeat a list option once per item;
                    skills/factory-step/SKILL.md describes them per role)
       factory migrate project TEAM [--out DIR]    (copy a v2 team checkout's descriptor and workflow, with its commit in SOURCE)
       factory env roles ENV                       (the roles of the enabled agent nodes that run in ENV, one per line)
       factory sweep --main ...                    (the gate's sweep from main's graph)
       factory ci-sync [--dry-run]                 (the CI labels of the project repositories' factory PRs; reconcile runs it)
       factory deploy --target local [--apply] | factory run --local [--once] [--dry-run]   (the local target, ADR-0017)
"""
import argparse
import contextlib
import io
import json
import os
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


def run_gate(engine, argv, team=None):
    """The gate, as main's own executable when `engine` is a main worktree, so a pull request cannot replace it."""
    if not engine:
        return gate.main(argv)
    done = subprocess.run([sys.executable, str(Path(engine) / "factory/gate.py"), "--team", team or engine, *argv],
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
    # ADR-0021: the installed `factory` is the engine's main, never the pull request's code: no main worktree.
    # Checked by files, before any engine module is imported.
    root = Path(team or os.environ.get("FACTORY_TEAM") or Path.home() / "factory-team")
    definition_team = all((root / f).is_file() for f in ("factory.yaml", "workflow.yaml"))
    engine = None if definition_team else \
        from_main() if a.main or a.automation.split("-")[0] in PR_NODES else None
    load_engine(Path(engine) / "factory" if engine else HERE)
    if engine or team:
        config.set_team(team or os.environ.get("FACTORY_TEAM") or engine)
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
        code = run_gate(engine, ["check", "--node", state["node"], "--event", state["event"], *gate_args(state)],
                        str(config.team_root()))
    print(out.getvalue(), end="")
    if code == 0:
        state["handoff_path"] = json.loads(out.getvalue())["handoff_path"]
        STATE.write_text(json.dumps(state))
    return code


def parted(text, sep, what):
    """`<head><sep><tail>` split once; ValueError naming `what` when the separator is missing."""
    head, found, tail = text.partition(sep)
    if not found or not head.strip() or not tail.strip():
        raise ValueError(f"{what} needs `{sep.strip()}`: {text!r}")
    return head.strip(), tail.strip()


def id_and(text, form):
    """`<id> <rest>` as a pair; ValueError naming the option's `form` when either is missing."""
    parts = text.split(None, 1)
    if len(parts) < 2:
        raise ValueError(f"expected {form}: {text!r}")
    return parts[0], parts[1].strip()


def finding(text, severity):
    """`<id> <file> <finding> [| case: <repro>]`; a blocking finding needs its case."""
    text, case = parted(text, " | case:", "finding case") if " | case:" in text else (text, None)
    parts = text.split(None, 2)
    if len(parts) < 3:
        raise ValueError(f"a finding is `<id> <file> <finding>`: {text!r}")
    if severity == "blocking" and not case:
        raise ValueError(f"a blocking finding needs `| case: <repro>`: {text!r}")
    return {"id": parts[0], "file": parts[1], "finding": parts[2], "severity": severity, **({"case": case} if case else {})}


def slice_of(path):
    """A slice file: its first line is the title (a leading `#` is dropped), the rest is the ticket body."""
    title, _, body = Path(path).read_text().strip().partition("\n")
    title = title.lstrip("#").strip()
    if not title:
        raise ValueError(f"slice {path} has no title line")
    return {"title": title, "body": body.strip() or title}


def handoff_of(a):
    """The handoff the options name: the outcome, plus the data each option carries. Plain-text options repeat;
    the JSON-file options (`--checks`, `--findings`, `--slices`, `--addressed`, `--notes`) are the older form."""
    data = {"outcome": a.outcome}
    if a.outcome == "env-broken" and not a.failing_check:
        raise ValueError("env-broken requires --failing-check")
    if a.failing_check:
        data["failing_check"] = a.failing_check
    for key, path in (("findings", a.findings), ("slices", a.slices), ("checks", a.checks)):
        if path:
            data[key] = json.loads(Path(path).read_text())
    checks = [{"text": t, "result": "pass"} for t in a.passed] + [{"text": t, "result": "fail"} for t in a.failed]
    checks += [dict(zip(("text", "reason"), parted(t, " | ", "a not-run check's reason")), result="not-run")
               for t in a.not_run]
    found = [finding(t, "blocking") for t in a.blocking] + [finding(t, "minor") for t in a.minor]
    for key, items in (("checks", checks), ("findings", found), ("slices", [slice_of(f) for f in a.slice]),
                       ("proposed_checks", a.propose_check)):
        if items:
            data[key] = data.get(key, []) + items
    data.update({k: v for k, v in (("reason", a.reason), ("summary", a.summary), ("size_estimate", a.size_estimate),
                                   ("reply", a.reply), ("to", a.to), ("text", a.text), ("problem", a.problem),
                                   ("recommended", a.recommended)) if v is not None and v != ""})
    for key, items in (("links", [dict(zip(("title", "url"), parted(t, " | ", "--link 'TITLE | URL'"))) for t in a.link]),
                       ("options", a.option)):
        if items:
            data[key] = items
    addressed = json.loads(Path(a.addressed).read_text()) if a.addressed else []
    addressed += ["{}: fixed in {}".format(*id_and(t, "--fixed 'ID SHA'")) for t in a.fixed]
    addressed += ["{}: disputed — {}".format(*id_and(t, "--disputed 'ID REASON'")) for t in a.disputed]
    if addressed:
        data["findings_addressed"] = addressed
    if a.notes:
        data["agent_notes"] = json.loads(Path(a.notes).read_text())
    elif a.note_hint or a.note_repro or a.note_file:
        data["agent_notes"] = {"for": a.note_for or "any", **({"files": a.note_file} if a.note_file else {}),
                               **{k: v for k, v in (("repro", a.note_repro), ("hint", a.note_hint)) if v}}
    if a.disputed_focuses:
        data["disputed_focuses"] = a.disputed_focuses.split(",")
    return data


def end_parser():
    p = argparse.ArgumentParser(prog="factory end", allow_abbrev=False)
    p.add_argument("outcome")
    for opt in ("findings", "slices", "checks", "reason", "summary", "size-estimate", "reply", "addressed", "disputed-focuses", "notes",
                "note-for", "note-hint", "note-repro", "to", "text", "problem"):
        p.add_argument(f"--{opt}")
    p.add_argument("--recommended", type=int)
    p.add_argument("--failing-check")
    for opt, dest in (("pass", "passed"), ("fail", "failed"), ("not-run", "not_run"), ("blocking", None), ("minor", None),
                      ("fixed", None), ("disputed", None), ("slice", None), ("propose-check", None), ("note-file", None), ("link", None), ("option", None)):
        p.add_argument(f"--{opt}", action="append", default=[], **({"dest": dest} if dest else {}))
    return p


def end(argv, team=None):
    try:
        a = end_parser().parse_args(argv)
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
        print(json.dumps({"error": f"malformed option: {e}"}))
        return 2
    handoff.parent.mkdir(parents=True, exist_ok=True)
    handoff.write_text(json.dumps(data))
    config.set_team(state["team"])
    return run_gate(state["engine"], ["apply", "--node", state["node"], "--event", state["event"], "--handoff",
                                      str(handoff), *gate_args(state)], state["team"])


def validate():
    import definition
    if definition.present(config.team_root()):  # ADR-0021: factory.yaml + workflow.yaml + roles/
        defn = definition.load(config.team_root())
        errs = definition.errors(defn)
        if not errs:  # the compiled view must pass the engine's own checks too
            import project
            import workflow
            errs = [f"compiled: {e}" for e in config.validate() or
                    workflow.validate(project.load(config.team_root()), workflow.load(config.team_root()))]
        print("\n".join([*errs, *(f"warning: {w}" for w in definition.warnings(defn)), *([] if errs else ["factory: ok"])]))
        return 1 if errs else 0
    project_mode = config.is_project()
    errs = config.validate()
    if not errs and project_mode:
        import project
        import workflow
        descriptor = project.load(config.team_root())
        errs = workflow.validate(descriptor, workflow.load(config.team_root(), descriptor))
    elif not errs:
        errs = nodes.validate(nodes.load(), config.load())
    print("\n".join(errs) or "factory: ok")
    return 1 if errs else 0


def env_roles(env):
    """Sorted roles of the enabled agent nodes (not human or script) whose `env` is `env`, from the project's workflow."""
    import project
    import workflow
    root = config.team_root()
    wf = workflow.load(root, project.load(root))
    has_role_files = (root / "factory.yaml").is_file()
    roles = {n["role"] for n in (wf.get("nodes") or {}).values()
             if n.get("env") == env and n.get("enabled") is not False
             and (not has_role_files or n.get("role") == "clerk"
                  or (root / "roles" / n.get("role", "") / "role.md").is_file())
             and n.get("role") not in (None, "human", "script")}
    return sorted(roles)


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
        config.set_team(team or os.environ.get("FACTORY_TEAM") or engine)
        return run_gate(engine, ["sweep", *[x for x in rest if x != "--main"]],
                        str(config.team_root()))
    if cmd == "end":
        return end(rest, team)
    if cmd == "begin":
        return begin(rest, team)
    load_engine(HERE)
    if team:
        config.set_team(team)
    if cmd in ("check", "apply", "sweep", "ci-sync", "decide", "ci"):
        return gate.main([cmd, *rest])
    if cmd == "deploy" and rest[:2] == ["--target", "local"]:
        from local import deploy as local_deploy
        return local_deploy(rest[2:])
    if cmd == "run":
        from local import main as local_main
        return local_main(rest)
    if cmd == "deploy":
        return deploy.main(rest)
    if cmd == "contracts":
        from contracts import main as contracts_main
        return contracts_main(rest)
    if cmd == "chart":
        return nodes.main(["chart", *rest])
    if cmd == "migrate" and rest[:1] == ["workflow"]:
        from workflow import main as workflow_main
        return workflow_main(["migrate", *rest[1:]])
    if cmd == "migrate" and rest[:1] == ["project"]:
        from workflow import project_main
        return project_main(rest[1:])
    if cmd == "upgrade":
        from upgrade import main as upgrade_main
        return upgrade_main(rest)
    if cmd == "validate" and not rest:
        return validate()
    if cmd == "retro":
        from retro import main as retro_main
        return retro_main(rest)
    if cmd == "setup":
        from team_setup import main as setup_main
        return setup_main(rest)
    if cmd == "env" and len(rest) == 2 and rest[0] == "roles":
        print("\n".join(env_roles(rest[1])))
        return 0
    if cmd == "doctor":
        from doctor import main as doctor_main
        return doctor_main(rest)
    if cmd == "reconcile":
        from reconcile import main as reconcile_main
        return reconcile_main(rest)
    print(__doc__)
    return 0 if cmd in ("-h", "--help") else 2


if __name__ == "__main__":
    sys.exit(main())

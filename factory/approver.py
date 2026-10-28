#!/usr/bin/env python3
# /// script
# requires-python = ">=3.12"
# dependencies = ["pyyaml>=6"]
# ///
"""Approver (roles/approver/role.md): may the branch merge without a human?

`decide()` builds a JSON summary of the branch against its base (changed paths and lines, CI,
reviewer verdicts, risk, dependency changes), checks the hard rules (`policies/auto-approve.rego`)
with Conftest, and only when they pass asks the soft questions (`judgments/auto-approve.yaml`) with
decisions-kit `decide`, engine and model from factory.yaml `roles.approver`. Every soft question
must resolve to `decided` with `yes`. In dry-run (the only mode for now) the verdict is
`would-auto-approve` or `needs-human`, with reasons; a missing tool is a `needs-human` reason.
`conftest` and `decide` can be replaced with $FACTORY_CONFTEST and $FACTORY_DECIDE.
"""
import json
import os
import re
import shutil
import subprocess
import tempfile
from pathlib import Path

# Manifests and lock files whose change is a dependency change.
DEP_FILES = re.compile(r"(^|/)(pyproject\.toml|uv\.lock|requirements[^/]*\.txt|package(-lock)?\.json|"
                       r"pnpm-lock\.yaml|yarn\.lock|go\.(mod|sum)|Cargo\.(toml|lock)|pom\.xml|build\.gradle(\.kts)?)$")
# shortcut: catches a changed `dependencies =` line of a PEP 723 header, not an edit inside a multi-line list.
INLINE_DEPS = re.compile(r"^[+-]#\s*dependencies\s*=", re.M)


def git(cwd, *args):
    return subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True).stdout


def summarize(wt, base, reviews, ci, risk):
    """The PR summary the hard rules read. `reviews` maps a reviewer to its handoff."""
    rng = f"origin/{base}...HEAD"
    paths, lines = [], 0
    for row in git(wt, "diff", "--numstat", rng).splitlines():
        added, removed, path = row.split("\t", 2)
        paths.append(path)
        lines += sum(int(n) for n in (added, removed) if n.isdigit())  # `-` for a binary file
    deps = [p for p in paths if DEP_FILES.search(p)]
    for path in paths:
        if path.endswith(".py") and path not in deps and INLINE_DEPS.search(git(wt, "diff", rng, "--", path)):
            deps.append(path)
    return {"paths": paths, "lines_changed": lines, "ci": ci, "risk": risk,
            "reviews": [{"reviewer": a, "verdict": r.get("verdict")} for a, r in reviews.items()],
            "dependency_changes": deps}


def hard(root, summary):
    """Reasons the hard rules reject `summary`; empty when they pass."""
    exe = os.environ.get("FACTORY_CONFTEST", "conftest")
    if not shutil.which(exe):
        return [f"hard rules not checked: {exe} is not installed"]
    with tempfile.TemporaryDirectory() as d:
        f = Path(d) / "summary.json"
        f.write_text(json.dumps(summary))
        r = subprocess.run([exe, "test", "--no-color", "--output", "json", "--policy",
                            str(Path(root) / "policies"), str(f)], capture_output=True, text=True)
    try:
        return [x["msg"] for res in json.loads(r.stdout) for x in res.get("failures") or []] or (
            [f"conftest exited {r.returncode}: {r.stderr.strip()[:200]}"] if r.returncode else [])
    except (ValueError, KeyError, TypeError):
        return [f"conftest output unreadable (exit {r.returncode}): {(r.stderr or r.stdout).strip()[:200]}"]


def soft(root, text, engine, model):
    """Reasons the soft judgment does not approve `text`; empty when every question is `decided` `yes`."""
    exe = os.environ.get("FACTORY_DECIDE", "decide")
    if not shutil.which(exe):
        return [f"soft rules not checked: {exe} is not installed"]
    with tempfile.TemporaryDirectory() as d:
        f = Path(d) / "input.md"
        f.write_text(text)
        # agent engines take the bare model name, factory.yaml keeps the provider prefix
        env = {**os.environ, "DECISIONS_MODEL": model.rpartition("/")[2]}
        r = subprocess.run([exe, "run", str(Path(root) / "judgments/auto-approve.yaml"), "--input", str(f),
                            "--json", "--engine", engine], capture_output=True, text=True, env=env)
    if r.returncode:
        return [f"decide exited {r.returncode} (3: engine unavailable): {r.stderr.strip()[:200]}"]
    try:
        questions = [q for res in json.loads(r.stdout) for q in res["questions"]]
    except (ValueError, KeyError, TypeError):
        return ["decide output unreadable"]
    return [f"{q.get('id')}: {q.get('resolution')} {q.get('answer')}" for q in questions
            if q.get("resolution") != "decided" or q.get("answer") != "yes"] or (
        [] if questions else ["decide returned no answers"])


def decide(root, config, wt, base, reviews, ci, risk, ticket):
    """{verdict, reasons, summary} for the branch in `wt`; `ticket` is the ticket's title and body."""
    cfg = config["roles"]["approver"]
    if not cfg.get("dry_run", True):
        raise ValueError("roles.approver.dry_run: false is not supported until the judgment is calibrated")
    summary = summarize(wt, base, reviews, ci, risk)
    reasons = hard(root, summary)
    if not reasons:
        diff = git(wt, "diff", f"origin/{base}...HEAD")
        text = f"## Ticket\n\n{ticket}\n\n## PR summary\n\n{json.dumps(summary, indent=2)}\n\n## Diff\n\n{diff}"
        reasons = soft(root, text, cfg["engine"], config["models"][cfg["tier"]])
    return {"verdict": "needs-human" if reasons else "would-auto-approve", "reasons": reasons, "summary": summary}

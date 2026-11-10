#!/usr/bin/env python3
"""Fill the setup skill's templates: render.py ANSWERS.json OUT_DIR.
Fills `template/` (a team definition in the layout of the engine's ADR-0021: factory.yaml, workflow.yaml,
roles/, .air/cloud/startup.sh, README.md). The README gets the workflow's state chart, and a rule broken in
the workflow (statechart.py, the pinned statechart skill) stops the run before any file is written. With --extras, fill `templates/` (the AGENTS.md workflow
section, docs) instead.

ANSWERS.json maps each `{{NAME}}` placeholder to a string, or to a list: a line with a list placeholder is repeated
once per item (an empty list drops the line). `CODE_REPOSITORIES` is such a list. The optional answers `TRACKER_URL` and
`TRACKER_PROJECTS` ({YouTrack project: owner/repo}) make the tracker YouTrack. Jinja's `{{ name }}` (lowercase,
with spaces) in role bodies is not a placeholder: setup renders it. A placeholder without an answer is an error.
"""
import json
import re
import sys
from pathlib import Path

import yaml

SKILL = Path(__file__).resolve().parent
T = SKILL / "templates"
PROJECT_T = SKILL / "template"
PH = re.compile(r"\{\{([A-Z_]+)\}\}")


def fill(text, a):
    def one(m, item=None):
        v = a[m[1]]  # KeyError: missing answer
        return v if item is None else item
    out = []
    for line in text.split("\n"):
        lists = [m for m in PH.finditer(line) if isinstance(a.get(m[1]), list)]
        for item in (a[lists[0][1]] if lists else [None]):
            out.append(PH.sub(lambda m: one(m, item) if lists and m[1] == lists[0][1] else one(m), line))
    return "\n".join(out)


def code_host_block(a):
    """The top-level `code_host:` lines: YouTrack keeps no pull requests, so they go to the first code repository on
    GitHub. Empty on GitHub Issues, which holds both."""
    return ["code_host:", "  adapter: github", f"  repository: {a['CODE_REPOSITORIES'][0]}"] if a.get("TRACKER_PROJECTS") else []


def tracker_block(a):
    """The lines under `tracker:` (indented): GitHub Issues keyed by the code repositories (the first is the tracker; a
    list of repositories only when there are several), or YouTrack with one linked project per repository."""
    repos = a["CODE_REPOSITORIES"]
    if a.get("TRACKER_PROJECTS"):
        projects = a["TRACKER_PROJECTS"]
        unknown = sorted(set(projects.values()) - set(repos))
        if unknown:
            raise ValueError(f"TRACKER_PROJECTS names repositories not in CODE_REPOSITORIES: {', '.join(unknown)}")
        return (["  adapter: youtrack", f"  url: {a['TRACKER_URL']}", f"  project: {next(iter(projects))}", "  projects:"]
                + [f"    {p}: {r}" for p, r in projects.items()])
    lines = ["  adapter: github-issues", f"  repository: {repos[0]}"]
    return lines + (["  repositories:"] + [f"    - {r}" for r in repos] if len(repos) > 1 else [])


def _render(root, a):
    return {f.relative_to(root).as_posix(): fill(f.read_text(), a)
            for f in sorted(p for p in root.rglob("*") if p.is_file() and "__pycache__" not in p.parts)}


def render(a):
    """{relative path: text} of the extras (`templates/`)."""
    return _render(T, a)


def automations_by_repository(factory, workflow):
    """{repository: [automation names]} of the nodes a deploy plans (ADR-0024): a node listening to several ticket
    repositories runs once per repository as `<node> <repo-name>`; a node with no ticket trigger runs in the repository
    it writes, else in the tracker repository. Human nodes are not automations."""
    tracker = factory["tracker"]
    tickets = list(tracker["projects"].values()) if tracker.get("projects") else tracker.get("repositories") or [tracker["repository"]]
    agents, envs = factory["agents"], factory["environments"]
    default_env = (workflow.get("settings") or {}).get("environment")
    out = {r: [] for e in envs.values() for r in (x["repo"] for x in e["repositories"])}
    for name, node in workflow["nodes"].items():
        if (agents.get(node.get("agent")) or {}).get("harness") == "human":
            continue
        repos = [x["repo"] for x in envs[node.get("environment") or default_env]["repositories"]]
        writes = repos if node.get("writes") == "all" else node.get("writes") or []
        listens = node.get("listens", "tickets")
        heard = [] if not any(isinstance(t, dict) and not {"schedule", "webhook"} & t.keys() for t in node.get("triggers") or []) \
            else tickets[:1] if listens == "tracker" else tickets if listens == "tickets" else listens
        for repo in heard or writes[:1] or tickets[:1]:
            out[repo].append(f"{name} {repo.split('/')[-1]}" if len(heard) > 1 else name)
    return out


def repositories_section(files):
    """README text: each repository and the automations that run in it."""
    by_repo = automations_by_repository(yaml.safe_load(files["factory.yaml"]), yaml.safe_load(files["workflow.yaml"]))
    return "\n## Repositories\n\nAutomations that run in each repository (a node that listens to several repositories runs once in each):\n\n" \
        + "".join(f"- `{r}`: {', '.join(f'`{n}`' for n in names) or 'none'}\n" for r, names in by_repo.items())


def render_project(a):
    """{relative path: text} of a new team's definition (`template/`)."""
    engine = a["ENGINE"]
    match = re.fullmatch(r"git\+https://github\.com/([^/]+/[^/@]+)@(.+)", engine)
    if not match:
        raise ValueError("ENGINE must be a pinned GitHub URL to include factory-deploy")
    if not a["CODE_REPOSITORIES"]:
        raise ValueError("CODE_REPOSITORIES needs at least one repository")
    files = _render(PROJECT_T, {**a, "ENGINE_SKILL_SOURCE": f"github:{match[1]}@{match[2]}", "TRACKER": tracker_block(a),
                                      "CODE_HOST": code_host_block(a)})
    return {**files, "README.md": files["README.md"].rstrip("\n") + "\n" + repositories_section(files)}


def with_chart(files):
    """`files` of a new team with its workflow's state chart in README.md. The pinned statechart skill checks the
    workflow first (statechart.py); a broken rule raises, so no file is written."""
    import statechart
    broken, chart = statechart.check(yaml.safe_load(files["workflow.yaml"]))
    if broken:
        raise SystemExit("workflow.yaml breaks statechart rules: " + ", ".join(broken)
                         + ". Fix the answers or the template; ask the user when the rule needs a decision.")
    return {**files, "README.md": files["README.md"].rstrip("\n") + f"\n\n## Workflow state chart\n\n```mermaid\n{chart.rstrip()}\n```\n"}


if __name__ == "__main__":
    args = sys.argv[1:]
    extras = args[:1] == ["--extras"]
    answers = json.loads(Path(args[extras]).read_text())
    for rel, text in (render if extras else lambda a: with_chart(render_project(a)))(answers).items():
        dest = Path(args[extras + 1]) / rel
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text(text)
        if rel.endswith(".sh"):
            dest.chmod(0o755)

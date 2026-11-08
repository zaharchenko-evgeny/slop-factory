#!/usr/bin/env python3
"""Fill the setup skill's templates: render.py ANSWERS.json OUT_DIR.
Fills `template/` (a team definition in the layout of the engine's ADR-0021: factory.yaml, workflow.yaml,
roles/, .air/cloud/startup.sh, README.md). The README gets the workflow's state chart, and a rule broken in
the workflow (statechart.py, the pinned statechart skill) stops the run before any file is written. With --extras, fill `templates/` (the AGENTS.md workflow
section, docs) instead.

ANSWERS.json maps each `{{NAME}}` placeholder to a string (or a list of lines). Jinja's `{{ name }}` (lowercase,
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
    def one(m):
        v = a[m[1]]  # KeyError: missing answer
        return "\n".join(v) if isinstance(v, list) else v
    return PH.sub(one, text)


def _render(root, a):
    return {f.relative_to(root).as_posix(): fill(f.read_text(), a)
            for f in sorted(p for p in root.rglob("*") if p.is_file() and "__pycache__" not in p.parts)}


def render(a):
    """{relative path: text} of the extras (`templates/`)."""
    return _render(T, a)


DOMAINS = ("[github.com, api.github.com, objects.githubusercontent.com, release-assets.githubusercontent.com,\n"
           "              \"*.blob.core.windows.net\", pypi.org, files.pythonhosted.org]")


def _environment(name, comment, repos, a):
    """One code environment of `factory.yaml`: the first repository is `main`, the others are read-only."""
    branch = {r["repo"]: r.get("branch", "main") for r in a["CODE_REPOSITORIES"]}
    lines = [f"  {name}:{comment}", "    repositories:"]
    for i, repo in enumerate(repos):
        lines += [f"      - repo: {repo}", "        main: true                # branches, PRs and the node's output apply here"] if i == 0 \
            else [f"      - repo: {repo}                # read-only here"]
        if branch.get(repo, "main") != "main":
            lines.append(f"        default_branch: {branch[repo]}")
    lines += [f"      - repo: {a['TEAM_REPOSITORY']} # the knowledge base, read-only",
              "    ci: { workflow: " + a["CI_WORKFLOW"] + " }   # `ci: failed` = Air workflow_run.failed for this workflow",
              f"    domains: {DOMAINS}", "    vm: default",
              "    setup: null                   # optional script path, run after the engine's bootstrap; installs the environment's tools"]
    return "\n".join(lines)


def code_repositories(a):
    """The code repositories of the answers: `CODE_REPOSITORIES`, a list of {name, repo, branch, reads}."""
    repos = a["CODE_REPOSITORIES"]
    if not repos:
        raise ValueError("CODE_REPOSITORIES: list at least one code repository")
    names = [r["name"] for r in repos]
    if len(set(names)) != len(names) or len({r["repo"] for r in repos}) != len(repos):
        raise ValueError("CODE_REPOSITORIES: each repository needs its own name and `owner/repo`")
    for r in repos:
        if set(r.get("reads") or []) - set(names) - {r["name"]}:
            raise ValueError(f"CODE_REPOSITORIES: {r['name']} reads an unknown repository")
    return repos


def derive(a):
    """Placeholders `render.py` computes from the answers: environments, tracker block, default environment.
    One code repository renders the single `main` environment; several render one environment per repository plus `hub`."""
    repos = code_repositories(a)
    by_name = {r["name"]: r["repo"] for r in repos}
    tracker = a.get("TRACKER") or {"adapter": "github-issues"}
    if len(repos) == 1:
        envs = [_environment("main", "                           # tickets and PRs of the project", [repos[0]["repo"]], a)]
    else:
        envs = [_environment(r["name"], f"  # tickets and PRs of {r['repo']}",
                             [r["repo"], *(by_name[n] for n in r.get("reads") or [] if n != r["name"])], a)
                for r in repos]
        hub_main = a["TRACKER_REPOSITORY"] if tracker["adapter"] == "github-issues" else repos[0]["repo"]
        envs.append(_environment("hub", "  # shared nodes: intake, dispatch, automerge, ...",
                                 [hub_main, *(r["repo"] for r in repos if r["repo"] != hub_main)], a))
    if tracker["adapter"] == "github-issues":
        conf = ["  adapter: github-issues", f"  repository: {a['TRACKER_REPOSITORY']}"]
    else:
        conf = [f"  adapter: {tracker['adapter']}", f"  url: {tracker['url']}", f"  project: {tracker['project']}"]
        if tracker.get("repository_field"):
            conf.append(f"  repository_field: {tracker['repository_field']}   # one value per code repository")
    return {"CODE_ENVIRONMENTS": "\n".join(envs), "TRACKER_CONFIG": "\n".join(conf),
            "CODE_REPOSITORIES_EVIDENCE": "\n".join(f"      - repo: {r['repo']}" for r in repos)}


def _options(files, a):
    """The optional answers that add a line: `BLOCKERS` (`merge`), `ISSUES_TOKEN_ENV`, and a repositories section in the README."""
    wf, fy = files["workflow.yaml"], files["factory.yaml"]
    if len(code_repositories(a)) > 1:  # the shared nodes run in the hub
        wf = wf.replace("  environment: main               #", "  environment: hub                #", 1)
    if a.get("BLOCKERS", "start") == "merge":
        wf = wf.replace("\nsettings:", "\nblockers: merge             # start the developer at once; the merge waits for every blocker\n\nsettings:", 1)
    if token := a.get("ISSUES_TOKEN_ENV"):
        gh = '    GH_TOKEN: { required: true, secret: true, description: "gh as jetbrains-air[bot], set by Air" }\n'
        fy = fy.replace(gh, gh + f'    {token}: {{ required: true, secret: true, description: "Issues read/write on every code repository" }}\n', 1)
        fy = fy.replace("  settings:\n    branch_pattern", f"  settings:\n    issues_token_env: {token}\n    branch_pattern", 1)
    repos = code_repositories(a)
    readme = files["README.md"]
    if len(repos) > 1:
        rows = "".join(f"- `{r['name']}` (`{r['repo']}`): `implement`, `fix`, `update`, `review`, `merge`, `finish`"
                       f" (automation `<node> {r['name']}`)" + (f"; reads {', '.join(f'`{n}`' for n in r['reads'])}" if r.get("reads") else "") + "\n"
                       for r in repos)
        readme = readme.rstrip("\n") + ("\n\n## Repositories\n\n" + rows + "\nThe other nodes (intake, dispatch, automerge, notify, ...) run once in the `hub` environment.\n")
    return {**files, "workflow.yaml": wf, "factory.yaml": fy, "README.md": readme}


def render_project(a):
    """{relative path: text} of a new team's definition (`template/`)."""
    engine = a["ENGINE"]
    match = re.fullmatch(r"git\+https://github\.com/([^/]+/[^/@]+)@(.+)", engine)
    if not match:
        raise ValueError("ENGINE must be a pinned GitHub URL to include factory-deploy")
    return _options(_render(PROJECT_T, {**a, **derive(a), "ENGINE_SKILL_SOURCE": f"github:{match[1]}@{match[2]}"}), a)


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

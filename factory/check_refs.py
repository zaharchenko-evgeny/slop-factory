#!/usr/bin/env python3
"""Report broken repository paths and issue references in project docs."""
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "factory"))
import config

REPO = config.load()["tracker"]["repository"]
ALLOW = {
    # Illustrative names used by the superseded state chart and old design docs.
    "factory.yaml", "setup.sh", "CLAUDE.md", "skills.txt", "startup.sh", "role.md", "docs/automations/",
    ".factory/",
    "knowledge/<repo>/", ".factory/runs/<n>/report.md", "<dir>/SKILL.md", "deploy/", "secrets/",
    "policies", "summary.json", "conftest", "~/Downloads/factory.md", "cursor/plugins", "mattpocock/skills",
    "repos/{owner}/{repo}/issues/{n}/dependencies/blocked_by", "refs/factory/<n>/lease",
    "refs/factory/_frontier/lease", "POST git/refs", ".claude/skills/", "JetBrains/jbeval",
    "app/jetbrains-air", "origin/main", "anthropic/claude-haiku-5-5", "context.md", "run_role.py",
    "routine.sm.yaml", "workflows/delivery.sm.yaml", "workflows/routine.sm.yaml", ".factory/runs/",
    ".factory/handoff/<node>.json", "<dir>/<repo>/lessons.md", "<dir>/_shared/lessons.md",
    "workflows/*.md", "Design: docs/<file>.md#<section>", "drive.py", "sm.py", "delivery.sm.yaml",
    "git/refs", "github/", "claude/skills/", "agents/skills/", "ez/", "ez/152-probes", "ez/152-probe-p5",
    "AGENTS.md:ez/<issue>-*", "AGENTS.md:roles/*/setup.sh",
    "docs/design.md:roles/<role>/", "docs/plan.md:roles/{router,developer,reviewer}/",
    "docs/recipes/auto-approve.md:conftest test --policy policies summary.json",
    "docs/statechart-proposed.md:#669", "docs/statechart-proposed.md:#333", "docs/statechart-proposed.md:#999",
    "docs/statechart-proposed.md:#666", "docs/statechart-proposed.md:#393", "docs/statechart.md:#669",
    "docs/statechart.md:#333", "docs/statechart.md:#999", "docs/statechart.md:#666", "docs/statechart.md:#393",
}
ISSUES = None
CODE = re.compile(r"(?<!`)`([^`\n]+)`(?!`)")
LINK = re.compile(r"\[[^]]*\]\(([^)\s]+)(?:\s+[^)]*)?\)")
ISSUE = re.compile(r"(?<![\w/])#(\d+)\b")


def docs(root=ROOT):
    paths = [root / "README.md", root / "AGENTS.md"]
    paths += sorted((root / "docs").glob("**/*.md"))
    paths += sorted((root / "roles").glob("**/*.md"))
    return [p for p in paths if p.is_file() and "docs/adr/" not in p.relative_to(root).as_posix()
            and "knowledge/" not in p.relative_to(root).as_posix()]


def references(text, source=""):
    fence_char, fence_size, skip_fence = None, 0, False
    for line_no, line in enumerate(text.splitlines(), 1):
        fence = re.match(r"^\s*(`{3,}|~{3,})(.*)$", line)
        if fence:
            marker, info = fence.groups()
            if fence_char is None:
                fence_char, fence_size = marker[0], len(marker)
                skip_fence = info.strip().lower() in {"text", "example"}
            elif marker[0] == fence_char and len(marker) >= fence_size and not info.strip():
                fence_char, fence_size, skip_fence = None, 0, False
            continue
        if skip_fence:
            continue
        found = [(token.rstrip(".,:;()[]{}\"'"), False) for match in CODE.finditer(line)
                 for token in match.group(1).split()]
        found += [(m.group(1), True) for m in LINK.finditer(line)]
        for ref, is_link in found:
            if (ref not in ALLOW and f"{source}:{ref}" not in ALLOW
                    and (is_path(ref) or is_link and is_local_link(ref))):
                yield line_no, ref, is_link
        for match in ISSUE.finditer(line):
            ref = "#" + match.group(1)
            if ref not in ALLOW and f"{source}:{ref}" not in ALLOW:
                yield line_no, ref, False


def is_path(ref):
    return (ref not in {"/", "./", "../"} and "://" not in ref and not ref.startswith("#") and "*" not in ref
            and "<" not in ref and "{" not in ref and
            ("/" in ref or bool(re.search(r"\.(?:md|py|sh|ya?ml|json|toml|txt|html)$", ref))
             or ref in {"Makefile", "Dockerfile", "AGENTS.md"}))


def is_local_link(ref):
    return (ref not in {"/", "./", "../"} and "://" not in ref and not ref.startswith("#")
            and "*" not in ref and "<" not in ref and "{" not in ref)


def issue_numbers():
    global ISSUES
    if ISSUES is None:
        result = subprocess.run(["gh", "api", "--paginate", "--slurp",
                                 f"repos/{REPO}/issues?state=all&per_page=100"],
                                check=True, capture_output=True, text=True)
        import json
        ISSUES = {str(item["number"]) for page in json.loads(result.stdout) for item in page}
    return ISSUES


def exists_in_repo(path, root):
    try:
        path.resolve().relative_to(root.resolve())
    except ValueError:
        return False
    return path.exists()


def check(root=ROOT):
    known = issue_numbers()
    errors = []
    for path in docs(root):
        rel = path.relative_to(root).as_posix()
        for line_no, ref, is_link in references(path.read_text(), rel):
            if ref.startswith("#"):
                valid = ref[1:] in known
            else:
                target = ref.split("#", 1)[0]
                if is_link:
                    valid = exists_in_repo(path.parent / target, root)
                else:
                    valid = exists_in_repo(root / target, root) or exists_in_repo(path.parent / target, root)
                    if not valid and "/" not in target and target:
                        valid = any(exists_in_repo(match, root) for match in root.rglob(target))
            if not valid:
                errors.append(f"{rel}:{line_no}: {ref}")
    return errors


def main():
    try:
        errors = check()
    except (OSError, subprocess.CalledProcessError) as exc:
        print(f"check_refs: {exc}", file=sys.stderr)
        return 2
    for error in errors:
        print(error)
    return 1 if errors else 0


if __name__ == "__main__":
    raise SystemExit(main())

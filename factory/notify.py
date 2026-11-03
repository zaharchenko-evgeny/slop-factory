#!/usr/bin/env python3
"""Notify the configured GitHub user once when a ticket needs a decision."""
import argparse
import json
import subprocess
import sys

import config
import vocab


def gh(args):
    result = subprocess.run(["gh", *args], capture_output=True, text=True)
    if result.returncode:
        raise RuntimeError(result.stderr.strip() or f"gh {' '.join(args[:3])} failed")
    return result.stdout


def paged(run, endpoint):
    pages = json.loads(run(["api", "--paginate", "--slurp", endpoint]))
    return [item for page in pages for item in page]


def notify(issue_number, run=gh):
    settings = config.load()
    repo = settings["tracker"]["repository"]
    issue = json.loads(run(["api", f"repos/{repo}/issues/{issue_number}"]))
    if vocab.needs_human() in [label["name"] for label in issue.get("labels", [])]:
        return False  # the gate already assigned and notified this human-wait state
    stages = settings["tracker"]["stages"]
    stage = next((name for name in ("blocked", "needs-answers")
                  if stages[name] in [label["name"] for label in issue.get("labels", [])]), None)
    if stage is None:
        raise ValueError(f"issue {issue_number} is not blocked or waiting for answers")

    comments = paged(run, f"repos/{repo}/issues/{issue_number}/comments")
    marker = f"<!-- factory-notify:{stage} -->"
    if any(marker in comment["body"] for comment in comments):
        return False
    reason = next((c["body"] for c in reversed(comments) if c["body"].startswith("[factory]")), "No reason was recorded.")
    timeline = paged(run, f"repos/{repo}/issues/{issue_number}/timeline")
    pr = next((event.get("source", {}).get("issue", {}).get("html_url") for event in reversed(timeline)
               if event.get("source", {}).get("issue", {}).get("pull_request")), None)
    body = (f"[factory] Ticket needs attention: `{stage}`.\n\n{reason}\n\n"
            f"PR: {pr or 'none'}\n\n{marker}\n\nFactory-Run: local")
    user = settings["notify"]["github_user"]
    run(["issue", "edit", str(issue_number), "-R", repo, "--add-assignee", user])
    run(["issue", "comment", str(issue_number), "-R", repo, "--body", body])
    return True


def main(argv=None):
    parser = argparse.ArgumentParser(prog="factory notify")
    parser.add_argument("--issue", required=True, type=int)
    args = parser.parse_args(argv)
    try:
        print(json.dumps({"notified": notify(args.issue)}))
        return 0
    except (RuntimeError, ValueError, KeyError) as e:
        print(json.dumps({"error": str(e)}))
        return 1


if __name__ == "__main__":
    sys.exit(main())

"""The factory inbox: build a message from a request and deliver it to every configured provider."""
import argparse
import json
import os
import re
import sys
from pathlib import Path

import config
import vocab
from github_tracker import gh
from inbox import request as req
from inbox import providers


def _paged(run, path):
    return [c for page in json.loads(run(["api", "--paginate", "--slurp", path])) for c in page]


def latest_comment(settings, issue, run=gh):
    """Text of the latest `[factory]` comment on `issue`, or None."""
    comments = _paged(run, f"repos/{settings['tracker']['repository']}/issues/{issue}/comments")
    return next((c["body"] for c in reversed(comments) if c["body"].startswith("[factory]")), None)


def facts(request, settings, run=gh, summary=None):
    """The message for `request`: its fields, the dedupe key, and the ticket and PR facts."""
    repo, message = settings["tracker"]["repository"], dict(request, key=req.key(request))
    if summary:
        message["summary"] = summary
    issue, pr = request.get("issue"), request.get("pr")
    if issue:
        view = json.loads(run(["issue", "view", str(issue), "-R", repo, "--json", "title,url,labels"]))
        labels = {lb["name"] for lb in view["labels"]}
        message.update(title=view["title"], url=view["url"], labels=sorted(labels),
                       stage=next((s for s, lb in settings["tracker"]["stages"].items() if lb in labels), None),
                       latest=latest_comment(settings, issue, run))
        if not pr:
            pr = next((i["number"] for e in reversed(_paged(run, f"repos/{repo}/issues/{issue}/timeline"))
                       if (i := e.get("source", {}).get("issue", {})).get("pull_request")
                       and i.get("repository", {}).get("full_name", repo) == repo), None)
    if pr:
        view = json.loads(run(["pr", "view", str(pr), "-R", repo, "--json", "url,state,headRefName"]))
        message.update(pr=pr, pr_url=view["url"], pr_state=view["state"], pr_branch=view["headRefName"])
    return message


def skip_reason(message, settings):
    """Why the inbox ignores `message` (the factory's own comment, its inbox issue, a foreign branch), or None."""
    if message["kind"] == "pr-comment" and (message.get("author") == settings["targets"]["air"]["bot_login"]
                                            or message.get("body", "").startswith("[factory]")):
        return "factory comment"
    if vocab.inbox() in message.get("labels", ()):
        return "event on the inbox issue"
    pattern = re.escape(settings["branch_pattern"]).replace(r"\{ticket\}", r"\d+").replace(r"\{slug\}", ".+")
    if message.get("pr_branch") is not None and not re.fullmatch(pattern, message["pr_branch"]):
        return f"branch {message['pr_branch']} is not a factory branch"
    return None


def check(request, settings, run=gh):
    """The facts plus `skip` (the reason to ignore the request) or `duplicate`: true when every provider already
    has the key. Writes nothing."""
    message = facts(request, settings, run)
    if reason := skip_reason(message, settings):
        return dict(message, skip=reason)
    pending = providers.build(settings["notify"]["providers"], settings, run)
    message["duplicate"] = all(p.seen(message["key"]) for _, p in pending)
    return message


def deliver(request, settings, run=gh, summary=None):
    """Send to every provider; one that fails does not stop the others."""
    message = facts(request, settings, run, summary)
    out = {"sent": [], "skipped": [], "failed": []}
    for kind, provider in providers.build(settings["notify"]["providers"], settings, run):
        try:
            out["sent" if provider.send(message) else "skipped"].append(kind)
        except Exception as e:  # noqa: BLE001 - any provider error is reported, not raised
            out["failed"].append({"kind": kind, "error": str(e)})
    return out


def notify_request(issue, settings, run=gh):
    """The `decision-needed` request `factory notify --issue` stands for."""
    request = {"kind": "decision-needed", "issue": issue, "source": "gate"}
    result = latest_comment(settings, issue, run)
    if result:
        request["result"] = result
    if os.environ.get("FACTORY_RUN_ID"):
        request["run"] = os.environ["FACTORY_RUN_ID"]
    return request


def main(argv=None):
    parser = argparse.ArgumentParser(prog="factory inbox")
    sub = parser.add_subparsers(dest="cmd", required=True)
    for name in ("check", "deliver"):
        p = sub.add_parser(name)
        p.add_argument("--request", required=True, help="request JSON file, or - for stdin")
    sub.choices["deliver"].add_argument("--summary", help="file with the summary text")
    args = parser.parse_args(argv)
    try:
        settings, request = config.load(), req.load(args.request)
        if args.cmd == "check":
            print(json.dumps(check(request, settings)))
            return 0
        out = deliver(request, settings, summary=Path(args.summary).read_text() if args.summary else None)
    except (RuntimeError, ValueError, OSError) as e:
        print(json.dumps({"error": str(e)}))
        return 1
    print(json.dumps(out if out["failed"] else {k: out[k] for k in ("sent", "skipped")}))
    return 1 if out["failed"] else 0


def notify_main(argv=None):
    """`factory notify --issue N`: an alias for `factory inbox deliver`."""
    parser = argparse.ArgumentParser(prog="factory notify")
    parser.add_argument("--issue", required=True, type=int)
    args = parser.parse_args(argv)
    try:
        settings = config.load()
        out = deliver(notify_request(args.issue, settings), settings)
    except (RuntimeError, ValueError, OSError) as e:
        print(json.dumps({"error": str(e)}))
        return 1
    print(json.dumps(out if out["failed"] else {k: out[k] for k in ("sent", "skipped")}))
    return 1 if out["failed"] else 0

"""Send a notification request: POST it to the inbox webhook, or deliver it in-process without one."""
import json
import os
import sys
import urllib.request

import config
from inbox import request as req


def post(webhook, request, opener=urllib.request.urlopen):
    """POST `request` to the URL in env var `webhook['url_secret']` with `Authorization: ApiKey <key>`."""
    url, key = os.environ.get(webhook["url_secret"]), os.environ.get(webhook["key_secret"])
    if not url or not key:
        raise RuntimeError(f"{webhook['url_secret']} or {webhook['key_secret']} is not set")
    http = urllib.request.Request(url, json.dumps(request).encode(), method="POST", headers={
        "Content-Type": "application/json", "Authorization": f"ApiKey {key}"})
    with opener(http, timeout=15):
        pass


def send(request, settings, opener=urllib.request.urlopen, deliver=None):
    """Validate and send `request`; a failure prints a warning and returns False, never raises."""
    try:
        errs = req.validate(request)
        if errs:
            raise ValueError("; ".join(errs))
        webhook = settings.get("notify", {}).get("webhook")
        if webhook:
            post(webhook, request, opener)
        else:
            import inbox
            out = (deliver or inbox.deliver)(request, settings)
            if out["failed"]:
                raise RuntimeError(json.dumps(out["failed"]))
        return True
    except Exception as e:  # noqa: BLE001 - a notification never fails a factory step
        print(f"warning: notification {request.get('kind')} not sent: {e}", file=sys.stderr)
        return False


def build(kind, issue=None, pr=None, result=None, run=None, source="gate"):
    return {k: v for k, v in (("kind", kind), ("issue", issue), ("pr", pr), ("result", result),
                              ("run", run or os.environ.get("FACTORY_RUN_ID")), ("source", source)) if v is not None}


def main(argv):
    import argparse
    p = argparse.ArgumentParser(prog="factory inbox send")
    p.add_argument("--kind", required=True)
    p.add_argument("--issue", type=int)
    p.add_argument("--pr", type=int)
    p.add_argument("--result")
    p.add_argument("--run")
    a = p.parse_args(argv)
    send(build(a.kind, a.issue, a.pr, a.result, a.run), config.load())
    return 0

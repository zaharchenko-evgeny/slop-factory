"""A notification request: parse, validate against engine/inbox.schema.json, and key for deduplication."""
import json
import sys
from pathlib import Path

import config

NO_ISSUE = ("retro-result", "automation-error")  # the kinds whose `issue` is optional


def validate(request):
    """Error lines for a request dict."""
    schema = json.loads((config.engine_root() / "engine/inbox.schema.json").read_text())
    errs = []
    config._check(schema, request, "request", errs)
    if not errs and "issue" not in request and request["kind"] not in NO_ISSUE:
        errs.append("request.issue: required")
    return errs


def parse(text):
    """The validated request in JSON `text`; ValueError lists every problem."""
    try:
        request = json.loads(text)
    except json.JSONDecodeError as e:
        raise ValueError(f"request: not JSON: {e}")
    errs = validate(request)
    if errs:
        raise ValueError("; ".join(errs))
    return request


def load(path):
    """The request in file `path`, or on stdin when `path` is `-`."""
    return parse(sys.stdin.read() if path == "-" else Path(path).read_text())


def key(request):
    return ":".join(str(request.get(f, "")) for f in ("kind", "issue", "pr", "run"))

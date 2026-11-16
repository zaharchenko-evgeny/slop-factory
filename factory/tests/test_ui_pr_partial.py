"""Partial GitHub PR reads keep raw reviewer requests and expose missing evidence."""

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "factory"))

from factory_plugins.github.gate import GitHubGateTracker  # noqa: E402
from ports.records import PrRecord  # noqa: E402


PR = {
    "number": 19,
    "head": {"sha": "sha-19", "ref": "ez/7-progress-ui"},
    "base": {"ref": "main"},
    "state": "open",
    "merged": False,
    "mergeable": True,
    "labels": [{"name": "review-needed"}],
    "requested_reviewers": [{"login": "alice"}, {"login": "factory[bot]"}],
    "requested_teams": [{"slug": "design"}],
    "user": {"login": "author", "type": "User"},
    "body": "progress PR",
    "updated_at": "2026-11-13T10:00:00Z",
}


def tracker_and_reads(monkeypatch, fail=()):
    tracker = GitHubGateTracker.for_repository("owner/project", run=lambda args: "")
    fail = set(fail)

    def value(name, result):
        def read(*args, **kwargs):
            if name in fail:
                raise OSError(f"{name} unavailable")
            return result
        return read

    monkeypatch.setattr(tracker, "_list", value("comments", []))
    monkeypatch.setattr(tracker, "_reviews", value("reviews", []))
    monkeypatch.setattr(tracker, "_pr_events", value("events", []))
    monkeypatch.setattr(tracker, "_ci", value("ci", "green"))

    def api(path, repo=None):
        if path.startswith("compare/"):
            if "behind" in fail:
                raise OSError("behind unavailable")
            return {"behind_by": 0}
        if path.startswith("commits/"):
            if "commit_date" in fail:
                raise OSError("commit_date unavailable")
            return {"commit": {"committer": {"date": "2026-11-13T09:00:00Z"}}}
        raise AssertionError(f"unexpected API read: {path}")

    monkeypatch.setattr(tracker, "_api", api)
    return tracker


def test_partial_read_keeps_human_and_team_review_requests_when_ci_fails(monkeypatch):
    tracker = tracker_and_reads(monkeypatch, fail={"ci"})

    record = tracker._pr_record(PR, "project", frozenset({"partial"}))

    assert record.requested_reviewers == ["alice", "factory[bot]", "team:design"]
    assert record.ci == "unknown"
    assert record.unavailable == ("ci",)
    assert record.state == "open"
    assert record.head == "sha-19"
    assert record.labels == ["review-needed"]
    assert record.behind is False
    assert record.commit_date == "2026-11-13T09:00:00Z"


@pytest.mark.parametrize(
    ("field", "expected"),
    [("behind", None), ("comments", []), ("reviews", []), ("events", []),
     ("ci", "unknown"), ("commit_date", "")],
)
def test_partial_read_isolates_each_unavailable_field(monkeypatch, field, expected):
    tracker = tracker_and_reads(monkeypatch, fail={field})

    record = tracker._pr_record(PR, "project", frozenset({"partial"}))

    assert getattr(record, field) == expected
    assert record.unavailable == (field,)
    assert record.requested_reviewers == ["alice", "factory[bot]", "team:design"]


@pytest.mark.parametrize("field", ["behind", "comments", "reviews", "events", "ci", "commit_date"])
def test_normal_gate_read_still_raises_for_failed_costly_field(monkeypatch, field):
    tracker = tracker_and_reads(monkeypatch, fail={field})
    want = frozenset({"behind"}) if field == "behind" else frozenset()

    with pytest.raises(OSError, match=f"{field} unavailable"):
        tracker._pr_record(PR, "project", want)


def test_partial_mode_does_not_swallow_malformed_basic_pr_fields(monkeypatch):
    tracker = tracker_and_reads(monkeypatch)
    malformed = {**PR, "head": {"ref": "ez/7-progress-ui"}}

    with pytest.raises(KeyError, match="sha"):
        tracker._pr_record(malformed, "project", frozenset({"partial"}))


def test_existing_positional_pr_record_construction_keeps_empty_unavailable_default():
    record = PrRecord("project", 19, "sha-19", "main", "ez/7-progress-ui", "open", False, True,
                      [], [], "", "", "", None, [], [], [], "none", "", {}, {}, None, None)

    assert record.unavailable == ()

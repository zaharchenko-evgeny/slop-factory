"""Read-only progress data projections; all tracker fakes refuse mutations."""

from datetime import datetime, timedelta, timezone
from pathlib import Path
import sys

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "factory"))

import derive  # noqa: E402
import gate  # noqa: E402
from factory_ui import data  # noqa: E402
from factory_ui import server as ui_server  # noqa: E402
from ports.records import PrRecord, TicketRecord  # noqa: E402


NOW = datetime(2026, 11, 13, 12, 0, tzinfo=timezone.utc)
STAGES = {"ready": "factory:ready", "approved": "factory:approved",
          "waiting": "factory:waiting", "done": "factory:done", "in-review": "factory:in-review"}


def config_for(block=None, environments=None):
    block = dict(block or {"adapter": "github-issues", "repository": "owner/project"})
    return {
        "project": {"name": "UI test factory"},
        "tracker": {"adapter": block.get("adapter", "github-issues"),
                    "repository": block.get("repository"), "url": block.get("url"),
                    "project": block.get("project"), "stages": STAGES,
                    "stage_kinds": {"ready": "active", "approved": "active", "in-review": "active",
                                    "waiting": "waiting", "done": "final"},
                    "risk_labels": {"low": "risk:low", "high": "risk:high"},
                    "needs_human_label": "factory:needs-human", "bot_logins": ["factory[bot]"]},
        "reviews": {"required": {"low": [], "high": []}, "trusted_bots": []},
        "limits": {"fix_rounds": 3, "agent_runs": 8},
        "environments": environments or {},
    }


def ticket_record(ticket_id=7, stage="ready", entered_at=None, *, attrs=None, updated_at=None,
                  assignees=None, blockers=None, events=None):
    labels = [STAGES[stage], "risk:low", *(attrs or [])]
    history = list(events or [])
    if not history:
        history = [{"kind": "labeled", "value": STAGES[stage], "at": entered_at, "actor": "human"}]
    return TicketRecord(id=ticket_id, title=f"Ticket {ticket_id}", updated_at=updated_at,
                        attrs=labels, assignees=list(assignees or []), events=history,
                        blockers_open=list(blockers or []))


class ReadOnlyTracker:
    """Minimal tracker port used by the projection; any write fails the test."""

    def __init__(self, cfg, record=None, prs=None, ids=None, repo_name="project"):
        self.cfg = cfg
        self.record = record or ticket_record()
        self.prs = dict(prs or {})
        self.ids = list(ids if ids is not None else [self.record.id])
        block = cfg.get("tracker") or {}
        self.repo = block.get("repository") or ""
        self.scope = None
        self.repositories = {repo_name: self.repo or repo_name}

    def rules(self):
        block = self.cfg["tracker"]
        return derive.Rules(stages=block["stages"], risk_labels=block["risk_labels"],
                            required=self.cfg["reviews"]["required"],
                            fix_cap=self.cfg["limits"]["fix_rounds"],
                            run_cap=self.cfg["limits"]["agent_runs"],
                            trusted=set(block.get("bot_logins") or []))

    def ticket_repositories(self, ticket, body=""):
        return list(self.repositories)

    def read(self, subject, want=frozenset()):
        if subject.kind == "ticket":
            return self.record
        if subject.kind == "pr":
            return self.prs.get(subject.repo)
        raise AssertionError(f"unexpected read subject: {subject}")

    def find(self, query):
        if query.kind == "stage":
            return list(self.ids)
        if query.kind == "children":
            return []
        # Local trigger polling may query these; this fake contains no event rows.
        if query.kind in ("labeled", "prs", "closed"):
            return []
        raise AssertionError(f"unexpected query: {query}")

    def apply(self, *args, **kwargs):
        raise AssertionError("the UI must not call tracker.apply")

    def write(self, *args, **kwargs):
        raise AssertionError("the UI must not call tracker.write")


def source_for(cfg, tracker, *, source_id="source", name="project", block=None, environments=()):
    block = block or cfg["tracker"]
    return data.Source(source_id, name, block, tuple(environments), lambda: (tracker, None))


def project(cfg, tracker, record=None, graph=None, now=NOW, events=None):
    return data.project_ticket(source_for(cfg, tracker), tracker, record or tracker.record, cfg,
                               graph or {"nodes": {}}, [] if events is None else events, now)


@pytest.mark.parametrize(("seconds", "warning"), [(3600, False), (3601, True)])
def test_active_stage_warning_changes_after_one_hour(seconds, warning):
    cfg = config_for()
    entered = (NOW - timedelta(seconds=seconds)).isoformat()
    record = ticket_record(entered_at=entered)
    tracker = ReadOnlyTracker(cfg, record)

    projected, errors = project(cfg, tracker)

    assert not errors
    assert projected["ageSeconds"] == seconds
    assert projected["warning"] is warning


@pytest.mark.parametrize(("stage", "kind"), [("waiting", "waiting"), ("done", "final")])
def test_waiting_and_final_stages_do_not_show_age_warning(stage, kind):
    cfg = config_for()
    cfg["tracker"]["stage_kinds"][stage] = kind
    record = ticket_record(stage=stage, entered_at=(NOW - timedelta(hours=5)).isoformat())
    tracker = ReadOnlyTracker(cfg, record)

    projected, errors = project(cfg, tracker)

    assert not errors
    assert projected["ageSeconds"] == 5 * 3600
    assert projected["warning"] is False


def test_stage_clock_uses_latest_reentry_not_updated_at_or_comment_time():
    cfg = config_for()
    first = (NOW - timedelta(hours=4)).isoformat()
    middle = (NOW - timedelta(hours=2)).isoformat()
    latest = (NOW - timedelta(minutes=15)).isoformat()
    record = ticket_record(stage="ready", entered_at=None, updated_at=NOW.isoformat(),
                           events=[{"kind": "labeled", "value": STAGES["ready"], "at": first, "actor": "a"},
                                   {"kind": "labeled", "value": STAGES["approved"], "at": middle, "actor": "b"},
                                   {"kind": "labeled", "value": STAGES["ready"], "at": latest, "actor": "c"}])
    record.comments = [{"at": NOW.isoformat(), "author": "human", "author_is_bot": False,
                        "author_is_factory": False, "body": "recent comment"}]
    tracker = ReadOnlyTracker(cfg, record)

    projected, errors = project(cfg, tracker)

    assert not errors
    assert projected["stageEnteredAt"] == latest
    assert projected["ageSeconds"] == 15 * 60
    assert [entry["stage"] for entry in projected["history"]] == ["ready", "approved", "ready"]


def test_unknown_stage_timestamp_stays_unknown_instead_of_using_updated_at():
    cfg = config_for()
    record = ticket_record(entered_at="not-a-time", updated_at=NOW.isoformat())
    tracker = ReadOnlyTracker(cfg, record)

    projected, errors = project(cfg, tracker)

    assert not errors
    assert projected["stageEnteredAt"] is None
    assert projected["ageSeconds"] is None
    assert projected["warning"] is False


@pytest.mark.parametrize(
    ("requested", "attrs", "expected_kind", "expected_reviewers"),
    [( ["alice"], [], "review", ["alice"]),
     (["factory[bot]"], [], "none", []),
     ([], ["factory:needs-human"], "attention", [])],
)
def test_attention_distinguishes_human_review_bot_review_and_generic_marker(
        requested, attrs, expected_kind, expected_reviewers):
    cfg = config_for()
    record = ticket_record(entered_at=NOW.isoformat(), attrs=attrs, assignees=["owner"])
    pr = PrRecord(repo="project", number=9, head="sha-9", requested_reviewers=requested,
                  state="open", ci="pending")
    tracker = ReadOnlyTracker(cfg, record, prs={"project": pr})

    projected, errors = project(cfg, tracker)

    assert not errors
    assert projected["attention"]["kind"] == expected_kind
    if expected_kind == "review":
        assert projected["attention"]["reviewers"] == expected_reviewers
        assert "marker" not in projected["attention"]["reason"].lower()
    elif expected_kind == "attention":
        assert "reason is unknown" in projected["attention"]["reason"]
        assert projected["attention"]["reviewers"] == ["owner"]
    else:
        assert projected["attention"]["reviewers"] == []


@pytest.mark.parametrize(
    ("node", "expected"),
    [({"role": "human", "when": {"stage": "approved"},
       "outcomes": {"merge": {"effects": [{"merge": "match-head"}]}}}, "review"),
     ({"role": "human", "when": {"actor": "human"},
       "outcomes": {"merge": {"from": ["approved"], "effects": [{"merge": "match-head"}]}}}, "review"),
     ({"role": "human", "when": {"actor": "human"},
       "outcomes": {"resume": {"effects": [{"stage": "ready"}]}}}, "none")],
)
def test_attention_only_marks_human_merge_gates_guarded_by_stage(node, expected):
    cfg = config_for()
    record = ticket_record(stage="approved", entered_at=NOW.isoformat())
    tracker = ReadOnlyTracker(cfg, record)

    projected, errors = project(cfg, tracker, graph={"nodes": {"human-check": node}})

    assert not errors
    assert projected["attention"]["kind"] == expected
    if expected == "review":
        assert projected["attention"]["reason"] == "Manual approval gate is unresolved"


def test_agent_review_in_review_stage_is_not_a_human_attention_request():
    cfg = config_for()
    cfg["reviews"]["required"]["low"] = ["full"]
    record = ticket_record(stage="in-review", entered_at=NOW.isoformat())
    pr = PrRecord(repo="project", number=9, head="sha-9", state="open", ci="green",
                  reviews=[{"author": "review-agent", "author_type": "Bot", "bot": True,
                            "agent": True, "focus": "full", "verdict": "approve", "commit_id": "sha-9",
                            "submitted_at": NOW.isoformat(), "round": 1, "findings": [], "body": ""}])
    tracker = ReadOnlyTracker(cfg, record, prs={"project": pr})

    projected, errors = project(cfg, tracker)

    assert not errors
    assert projected["stage"] == "in-review"
    assert projected["prs"][0]["requestedReviewers"] == []
    assert projected["attention"]["kind"] == "none"


def test_missing_pr_facts_do_not_become_negative_gate_results():
    cfg = config_for()
    record = ticket_record(entered_at=NOW.isoformat())

    class PrReadFails(ReadOnlyTracker):
        def read(self, subject, want=frozenset()):
            if subject.kind == "pr":
                raise RuntimeError("PR evidence unavailable")
            return super().read(subject, want)

    tracker = PrReadFails(cfg, record)
    graph = {"nodes": {"reviewer": {"role": "reviewer", "triggers": ["issues.labeled factory:ready"],
                                      "when": {"pr-state": "open"}, "outcomes": {}}}}
    events = [("labeled:7", {"node": "reviewer"},
               {"repository": {"full_name": "owner/project"}, "issue": {"number": 7}})]

    projected, errors = project(cfg, tracker, graph=graph, events=events)

    assert errors == ["PR evidence unavailable (RuntimeError)"]
    [eligibility] = projected["nodes"]
    assert eligibility["trigger"] == "matched", eligibility
    assert eligibility["conditions"][0]["result"] == "unknown"
    assert eligibility["eligibility"] == "unknown"


def test_ticket_identity_deduplicates_same_github_ticket_but_keeps_other_places_distinct():
    gh_a = config_for({"adapter": "github-issues", "repository": "owner/first"})
    gh_b = config_for({"adapter": "github-issues", "repository": "owner/second"})
    yt_a = config_for({"adapter": "youtrack", "url": "https://yt.example/one", "project": "ABC"})
    yt_b = config_for({"adapter": "youtrack", "url": "https://yt.example/two", "project": "ABC"})
    github_record = ticket_record(7, entered_at=NOW.isoformat())
    opaque_record = ticket_record("ABC-1", entered_at=NOW.isoformat())

    same_a, _ = project(gh_a, ReadOnlyTracker(gh_a, github_record), github_record)
    same_b, _ = project(gh_a, ReadOnlyTracker(gh_a, github_record), github_record)
    other_repo, _ = project(gh_b, ReadOnlyTracker(gh_b, github_record), github_record)
    youtrack_a, _ = project(yt_a, ReadOnlyTracker(yt_a, opaque_record), opaque_record)
    youtrack_b, _ = project(yt_b, ReadOnlyTracker(yt_b, opaque_record), opaque_record)

    assert same_a["key"] == same_b["key"]
    assert same_a["key"] != other_repo["key"]
    assert youtrack_a["key"] != youtrack_b["key"]


def test_snapshot_deduplicates_overlapping_environments_and_keeps_distinct_ticket_places(monkeypatch):
    cfg_base = config_for()
    same_record = ticket_record(7, entered_at=NOW.isoformat())
    first_cfg = config_for({"adapter": "github-issues", "repository": "owner/shared"})
    second_cfg = config_for({"adapter": "github-issues", "repository": "owner/shared"})
    other_cfg = config_for({"adapter": "github-issues", "repository": "owner/other"})
    yt_first_cfg = config_for({"adapter": "youtrack", "url": "https://yt.example/one", "project": "ABC"})
    yt_second_cfg = config_for({"adapter": "youtrack", "url": "https://yt.example/two", "project": "ABC"})
    first_tracker = ReadOnlyTracker(first_cfg, same_record)
    second_tracker = ReadOnlyTracker(second_cfg, same_record)
    other_tracker = ReadOnlyTracker(other_cfg, same_record)
    opaque_record = ticket_record("ABC-1", entered_at=NOW.isoformat())
    yt_first_tracker = ReadOnlyTracker(yt_first_cfg, opaque_record)
    yt_second_tracker = ReadOnlyTracker(yt_second_cfg, opaque_record)
    sources = [source_for(first_cfg, first_tracker, source_id="dev", environments=("dev",)),
               source_for(second_cfg, second_tracker, source_id="prod", environments=("prod",)),
               source_for(other_cfg, other_tracker, source_id="other", environments=("other",)),
               source_for(yt_first_cfg, yt_first_tracker, source_id="yt-one", environments=("yt-one",)),
               source_for(yt_second_cfg, yt_second_tracker, source_id="yt-two", environments=("yt-two",))]
    monkeypatch.setattr(data, "plan_local", lambda *args: [])
    monkeypatch.setattr(data, "poll", lambda *args: [])
    snapshot = data.SnapshotStore(config=cfg_base, graph={"nodes": {}}, sources=sources,
                                  now=lambda: NOW).snapshot()

    assert len(snapshot["tickets"]) == 4
    shared = next(ticket for ticket in snapshot["tickets"] if ticket["environments"] == ["dev", "prod"])
    assert shared["environments"] == ["dev", "prod"]
    assert {ticket["url"] for ticket in snapshot["tickets"]} >= {
        "https://github.com/owner/shared/issues/7", "https://github.com/owner/other/issues/7",
        "https://yt.example/one/issue/ABC-1", "https://yt.example/two/issue/ABC-1"}


def test_snapshot_retains_failed_source_and_shows_partial_success(monkeypatch):
    cfg = config_for()
    tracker = ReadOnlyTracker(cfg, ticket_record(8, entered_at=NOW.isoformat()))
    broken = data.Source("broken", "Broken", cfg["tracker"], (),
                         lambda: (_ for _ in ()).throw(RuntimeError("offline")))
    working = source_for(cfg, tracker, source_id="working", environments=("read",))
    old = {"key": "old-key", "id": "#7", "title": "Cached", "repository": "project",
           "stage": "ready", "ageSeconds": 12, "environments": [], "prs": []}
    monkeypatch.setattr(data, "plan_local", lambda *args: [])
    monkeypatch.setattr(data, "poll", lambda *args: [])
    store = data.SnapshotStore(config=cfg, graph={"nodes": {}}, sources=[broken, working], now=lambda: NOW)
    store.cache["broken"] = {"tickets": {"old-key": old}, "updatedAt": "2026-11-12T10:00:00+00:00"}

    snapshot = store.snapshot()

    assert snapshot["stale"] is True
    assert {source["id"]: source["ok"] for source in snapshot["sources"]} == {"broken": False, "working": True}
    assert {ticket["key"] for ticket in snapshot["tickets"]} == {"old-key", data.project_ticket(
        working, tracker, tracker.record, cfg, {"nodes": {}}, [], NOW)[0]["key"]}


@pytest.mark.parametrize(("age", "visible"), [(7 * 86400, True), (7 * 86400 + 1, False)])
def test_final_ticket_visibility_uses_seven_day_boundary(age, visible):
    store = data.SnapshotStore(config=config_for(), graph={"nodes": {}}, sources=[], now=lambda: NOW)
    ticket = {"stage": "done", "ageSeconds": age}
    snapshot = {"stages": [{"id": "done", "kind": "final"}], "tickets": [ticket]}

    current = store.visible(snapshot, older=False)
    history = store.visible(snapshot, older=True)

    assert bool(current["tickets"]) is visible
    assert history["tickets"] == [ticket]


def test_data_projection_never_calls_gate_mutators_or_execution(monkeypatch):
    cfg = config_for()
    tracker = ReadOnlyTracker(cfg, ticket_record(entered_at=NOW.isoformat()))
    source = source_for(cfg, tracker)

    def forbidden(*args, **kwargs):
        raise AssertionError("the UI called a gate execution or mutation method")

    for name in ("check", "begin", "apply", "run_event", "sweep"):
        monkeypatch.setattr(gate, name, forbidden, raising=False)
    monkeypatch.setattr(data, "plan_local", lambda *args: [])
    monkeypatch.setattr(data, "poll", lambda *args: [])

    snapshot = data.SnapshotStore(config=cfg, graph={"nodes": {}}, sources=[source], now=lambda: NOW).snapshot()

    assert snapshot["tickets"][0]["stage"] == "ready"


def test_partial_ci_failure_keeps_human_review_and_unknown_ci_condition():
    cfg = config_for()
    record = ticket_record(stage="in-review", entered_at=NOW.isoformat())
    pr = PrRecord(repo="project", number=9, head="sha-9", state="open", ci="unknown",
                  requested_reviewers=["alice"], unavailable=("ci",))
    tracker = ReadOnlyTracker(cfg, record, prs={"project": pr})
    graph = {"nodes": {"reviewer": {"role": "reviewer", "triggers": ["manual"],
                                      "when": {"stage": "in-review", "ci": ["green"]}, "outcomes": {}}}}

    projected, errors = project(cfg, tracker, graph=graph)

    assert errors == ["PR 9: ci evidence unavailable"]
    assert projected["attention"]["kind"] == "review"
    reviewer = projected["nodes"][0]
    assert reviewer["eligibility"] == "unknown"
    assert {condition["key"]: condition["result"] for condition in reviewer["conditions"]} == {
        "stage": "matched", "ci": "unknown"}


def test_ticket_key_is_stable_across_healthy_partial_and_failed_pr_reads():
    cfg = config_for()
    record = ticket_record(7, entered_at=NOW.isoformat())
    healthy_tracker = ReadOnlyTracker(cfg, record, prs={"project": PrRecord(
        repo="project", number=19, head="sha-19", ci="green")})
    partial_tracker = ReadOnlyTracker(cfg, record, prs={"project": PrRecord(
        repo="project", number=19, head="sha-19", ci="unknown", unavailable=("ci",))})

    class PrReadFails(ReadOnlyTracker):
        def read(self, subject, want=frozenset()):
            if subject.kind == "pr":
                raise RuntimeError("PR endpoint unavailable")
            return super().read(subject, want)

    failed_tracker = PrReadFails(cfg, record)
    healthy, healthy_errors = project(cfg, healthy_tracker, record)
    partial, partial_errors = project(cfg, partial_tracker, record)
    failed, failed_errors = project(cfg, failed_tracker, record)
    other_record = ticket_record(8, entered_at=NOW.isoformat())
    other, other_errors = project(cfg, PrReadFails(cfg, other_record), other_record)

    assert not healthy_errors
    assert partial_errors == ["PR 19: ci evidence unavailable"]
    assert failed_errors == ["PR evidence unavailable (RuntimeError)"]
    assert failed_errors == other_errors
    assert healthy["key"] == partial["key"] == failed["key"]
    assert other["key"] != failed["key"]


def test_pr_poll_event_uses_canonical_repository_qualified_ticket_id():
    from types import SimpleNamespace

    tracker = SimpleNamespace(repo="owner/project", ticket_repos=["owner/project"])
    event = ("labeled:19", {"node": "reviewer"}, {
        "repository": {"full_name": "owner/project"},
        "pull_request": {"number": 19, "head": {"ref": "ez/7-progress-ui"}},
    })

    assert data.event_ticket(event, tracker) == "owner/project#7"


def test_copy_matches_tracker_and_scope_namespace():
    source = {"adapter": "github-issues", "repository": "owner/project", "scope": {"label": "team:red"}}

    assert data.copy_matches(source, {"tracker": {**source}}) is True
    assert data.copy_matches(source, {"tracker": {"adapter": "github-issues", "repository": "owner/project",
                                                    "scope": {"label": "team:blue"}}}) is False
    assert data.copy_matches({"adapter": "github-issues", "repository": "owner/project"},
                             {"tracker": source}) is None
    assert data.copy_matches(source, {"tracker": {"adapter": "github-issues", "repository": "owner/other",
                                                   "scope": {"label": "team:red"}}}) is False


def test_project_ticket_uses_qualified_issue_repository_for_prs_and_copy_environment():
    cfg = config_for({"adapter": "github-issues", "repository": "owner/first"})
    cfg["repositories"] = [{"name": "first", "url": "https://github.com/owner/first"},
                           {"name": "second", "url": "https://github.com/owner/second"}]
    cfg["environments"] = {
        "first-env": {"repositories": ["first"], "writable": ["first"],
                      "ci": {"checks": ["first-check"]}},
        "second-env": {"repositories": ["second"], "writable": ["second"],
                       "ci": {"checks": ["second-check"]}},
    }
    record = ticket_record("owner/second#7", entered_at=NOW.isoformat())
    second_cfg = config_for({"adapter": "github-issues", "repository": "owner/second"})
    pr = PrRecord(repo="second", number=19, head="sha-19", requested_reviewers=["alice"])
    second = ReadOnlyTracker(second_cfg, record, prs={"second": pr}, repo_name="second")
    second.pr_repo = "owner/second"
    second.ci_checks = ["stale-check"]
    first = ReadOnlyTracker(cfg, record)
    first.repo = "owner/first"
    first.ticket_repos = ["owner/first", "owner/second"]
    first.pr_repo = "owner/first"
    first.ci_checks = ["stale-check"]
    visited = []
    first.at = lambda repo: visited.append(repo) or second
    source = source_for(cfg, first, environments=("first-env",))
    graph = {"nodes": {"review": {"role": "reviewer", "triggers": ["manual"],
                                    "when": {"always": True}, "outcomes": {},
                                    "_ui_copies": [{"env": "second-env", "tracker": {
                                        "adapter": "github-issues", "repository": "owner/second"}}]}}}

    projected, errors = data.project_ticket(source, first, record, cfg, graph, [], NOW)

    assert not errors
    assert visited == ["owner/second"]
    assert projected["repository"] == "second"
    assert projected["prs"][0]["repository"] == "second"
    assert projected["attention"]["kind"] == "review"
    assert projected["environments"] == ["first-env", "second-env"]
    assert second.ci_checks == ["second-check"]


def test_configure_ui_ci_preserves_legacy_checks_without_environment_match():
    cfg = config_for()
    tracker = ReadOnlyTracker(cfg)
    tracker.pr_repo = "owner/project"
    tracker.ci_checks = ["legacy-check"]

    data.configure_ui_ci(tracker, cfg)

    assert tracker.ci_checks == ["legacy-check"]


@pytest.mark.parametrize(
    ("copy_repository", "expected_environments", "copy_matches_source"),
    [("owner/project", ["source-env", "compiled-env"], True),
     ("owner/other", ["source-env"], False)],
)
def test_snapshot_store_keeps_source_environment_provenance_without_widening_copy_eligibility(
        monkeypatch, copy_repository, expected_environments, copy_matches_source):
    import definition

    cfg = config_for()
    tracker = ReadOnlyTracker(cfg)
    source = source_for(cfg, tracker, environments=("source-env",))
    graph = {"nodes": {"review": {"role": "reviewer", "triggers": ["manual"],
                                   "when": {"always": True}, "outcomes": {}}}}
    copies = [{"env": "compiled-env", "tracker": {
        "adapter": "github-issues", "repository": copy_repository}}]
    monkeypatch.setattr(data.nodes_mod, "load", lambda: graph)
    monkeypatch.setattr(data.config_mod, "team_root", lambda: "/fake/team")
    monkeypatch.setattr(definition, "present", lambda _root: True)
    monkeypatch.setattr(definition, "load", lambda _root: {})
    monkeypatch.setattr(definition, "copies", lambda _definition, _name: copies)

    store = data.SnapshotStore(config=cfg, sources=[source], now=lambda: NOW)

    assert store.sources[0].environments == tuple(expected_environments)
    projection = data.node_projection("review", store.graph["nodes"]["review"],
                                      store.sources[0], {}, [])
    assert (projection["trigger"] != "not-matched") is copy_matches_source


def test_whole_pr_read_failure_keeps_cached_pr_and_marks_it_stale(monkeypatch):
    cfg = config_for()
    record = ticket_record(7, entered_at=NOW.isoformat())

    class PrReadFails(ReadOnlyTracker):
        def read(self, subject, want=frozenset()):
            if subject.kind == "pr":
                raise RuntimeError("PR endpoint unavailable")
            return super().read(subject, want)

    tracker = PrReadFails(cfg, record)
    source = source_for(cfg, tracker)
    current, _ = project(cfg, tracker, record)
    old_pr = {"number": "19", "url": "https://github.com/owner/project/pull/19",
              "repository": "project", "head": "sha-old", "state": "open", "ci": "green",
              "requestedReviewers": ["alice"]}
    previous = {**current, "prs": [old_pr], "evidenceErrors": []}
    store = data.SnapshotStore(config=cfg, graph={"nodes": {}}, sources=[source], now=lambda: NOW)
    store.cache[source.id] = {"tickets": {current["key"]: previous}, "updatedAt": "2026-11-12T10:00:00+00:00"}
    monkeypatch.setattr(data, "plan_local", lambda *args: [])
    monkeypatch.setattr(data, "poll", lambda *args: [])

    snapshot = store.snapshot()

    ticket = snapshot["tickets"][0]
    assert ticket["prs"] == [old_pr]
    assert "Previous PR details may be stale" in ticket["evidenceErrors"]
    assert any("PR evidence unavailable" in error for error in ticket["evidenceErrors"])
    assert snapshot["sources"][0]["ok"] is False


def test_concurrent_snapshot_returns_previous_snapshot_marked_stale():
    store = data.SnapshotStore(config=config_for(), graph={"nodes": {}}, sources=[], now=lambda: NOW)
    store.latest = {"stale": False, "stages": [{"id": "ready", "kind": "active"}],
                    "tickets": [{"id": "#7", "stage": "ready"}]}
    store.lock.acquire()
    try:
        snapshot = store.snapshot()
    finally:
        store.lock.release()

    assert snapshot["stale"] is True
    assert snapshot["tickets"] == store.latest["tickets"]


@pytest.mark.parametrize(("argv", "expected_port"), [([], 8765), (["--port", "43210"], 43210)])
def test_server_opens_browser_to_selected_loopback_url(monkeypatch, argv, expected_port):
    opened = []
    closed = []
    requested_ports = []

    class FakeServer:
        def __init__(self, port):
            self.server_port = port
        def serve_forever(self):
            raise KeyboardInterrupt
        def server_close(self):
            closed.append(True)

    class FakeTimer:
        def __init__(self, delay, function, args=()):
            assert delay == 0.1
            self.function, self.args = function, args
            self.daemon = False
        def start(self):
            self.function(*self.args)

    monkeypatch.setattr(ui_server, "SnapshotStore", lambda: object())
    def fake_make_server(_store, port):
        requested_ports.append(port)
        return FakeServer(port)
    monkeypatch.setattr(ui_server, "make_server", fake_make_server)
    monkeypatch.setattr(ui_server.threading, "Timer", FakeTimer)
    monkeypatch.setattr(ui_server.webbrowser, "open", lambda url: opened.append(url) or True)

    assert ui_server.main(argv) == 0
    assert requested_ports == [expected_port]
    assert opened == [f"http://127.0.0.1:{expected_port}/"]
    assert closed == [True]

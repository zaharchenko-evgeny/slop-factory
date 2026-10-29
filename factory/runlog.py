"""The timestamped run log (`<run dir>/<ticket>/events.log`, one JSON object per line) and `drive.py report`.

Every line has `ts` (ISO 8601 UTC), `run` (the run id) and `event`:
  usage       step, role, model, input, output, cacheRead, cacheWrite, cost   (one per model call)
  step.start  step, role, model, attempt
  step.end    step, role, model, attempt, duration_s, outcome (ok | empty | bad-handoff)
  fallback    step, role, from, to
  retry       action, count
  transition  from, on (the fired event), to, note
  wait        what (ci | merge), duration_s
  human       what (merge), decision (merged), approver (the approver's verdict)
Old logs hold `usage` lines only: their cost counts, their durations are unknown (None), never 0.
"""
import json
from datetime import datetime, timezone
from pathlib import Path


def emit(path, run, event, **fields):
    """Append one event line to the events.log at `path` (short appends, safe from parallel steps)."""
    ts = datetime.now(timezone.utc).isoformat(timespec="seconds")
    with Path(path).open("a") as f:
        f.write(json.dumps({"ts": ts, "run": run, "event": event, **fields}) + "\n")


def _secs(a, b):
    return (datetime.fromisoformat(b) - datetime.fromisoformat(a)).total_seconds()


def summarize(ticket_dir):
    """One ticket's numbers from its run dir; durations are None when the log has no timestamps."""
    d = Path(ticket_dir)
    log = d / "events.log"
    ev = [json.loads(x) for x in log.read_text().splitlines() if x.strip()] if log.exists() else []
    sm_file = d / "sm.json"
    t = json.loads(sm_file.read_text()) if sm_file.exists() else {}
    stamped = [e["ts"] for e in ev if e.get("ts")]
    ends = [e for e in ev if e["event"] == "step.end"]
    by = {}
    for e in ev:
        if e["event"] == "usage":
            k = f"{e['role']}/{e['model']}"
            by[k] = by.get(k, 0) + (e.get("cost") or 0)
    out = t.get("out") or {}
    start = min(stamped) if stamped else None
    if start is None and ev:  # no timestamps (old log): the file's mtime
        start = datetime.fromtimestamp(log.stat().st_mtime, timezone.utc).isoformat(timespec="seconds")
    reasons = [str(h.get(k)) for h in out.values() if isinstance(h, dict) for k in ("blocked_reason", "error")
               if h.get(k) and h.get(k) != "none"]
    reasons += [e["note"] for e in ev if e["event"] == "transition" and e.get("note") and e["to"] in ("blocked", "failed")]
    return {
        "ticket": d.name, "state": t.get("state") or "unknown",
        "start": start,
        "wall_s": _secs(min(stamped), max(stamped)) if len(stamped) > 1 else None,
        "active_s": sum(e["duration_s"] for e in ends) if ends else None,
        "wait_s": sum(e["duration_s"] for e in ev if e["event"] == "wait") if stamped else None,
        "cost": by, "cost_total": sum(by.values()) if by else None,
        "fix_rounds": (t.get("vars") or {}).get("fix_round", 0),
        "retries": sum(e["event"] == "retry" for e in ev),
        "fallbacks": sum(e["event"] == "fallback" for e in ev),
        "reasons": reasons,
    }


def collect(run_dir, since=None, ticket=None):
    rows = [summarize(p) for p in sorted((q for q in Path(run_dir).glob("[0-9]*") if q.name.isdigit()), key=lambda q: int(q.name))
            if p.is_dir() and (ticket is None or int(p.name) == int(ticket))]
    return [r for r in rows if since is None or (r["start"] or "") >= since]


def _t(s):
    return "?" if s is None else f"{int(s) // 60}m{int(s) % 60:02d}s"


def table(rows):
    """The report as text: one line per ticket, its cost split and reasons below, a total line."""
    head = f"{'ticket':<8}{'state':<20}{'wall':>9}{'active':>9}{'wait':>9}{'cost $':>9}{'fix':>5}{'retry':>6}{'fallb':>6}"
    lines = [head]
    for r in rows:
        c = "?" if r["cost_total"] is None else f"{r['cost_total']:.3f}"
        lines.append(f"{r['ticket']:<8}{r['state']:<20}{_t(r['wall_s']):>9}{_t(r['active_s']):>9}{_t(r['wait_s']):>9}"
                     f"{c:>9}{r['fix_rounds']:>5}{r['retries']:>6}{r['fallbacks']:>6}")
        lines += [f"        {k}: ${v:.3f}" for k, v in r["cost"].items()]
        lines += [f"        reason: {x}" for x in r["reasons"]]
    def known(k):  # unknown values count as nothing in the total
        return sum(r[k] for r in rows if r[k] is not None)
    return "\n".join(lines + [f"{'total':<28}{_t(known('wall_s')):>9}{_t(known('active_s')):>9}{_t(known('wait_s')):>9}"
                              f"{known('cost_total'):>9.3f}{known('fix_rounds'):>5}{known('retries'):>6}{known('fallbacks'):>6}"])

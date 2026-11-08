"""Batched GitHub GraphQL reads for the gate, returned in the REST shapes the gate already reads.

`search_query` lists the factory's tickets in one paged search: open tickets with a stage label, plus tickets closed in
the last `gate_port.CLOSED_HOURS`. `batch_query` reads, for up to `BATCH` tickets at once, each ticket's issue, events, comments
and native blockers; `to_rest` turns one ticket of its answer into the bodies of `issues/{n}`, `issues/{n}/events`,
`issues/{n}/comments` and `issues/{n}/dependencies/blocked_by`, or leaves out a part GraphQL did not return in full.
"""
import base64
import re
from datetime import datetime, timedelta, timezone

from gate_port import CLOSED_HOURS

BATCH = 10  # 5 Nov: a 38-ticket query answered every timeline with totalCount 0 and no error; 10 matched REST
# The timeline item types REST /issues/{n}/events also returns: comments and cross-references are not events there.
# Project events are left out: reading them needs the read:project scope, and the factory uses no GitHub project.
EVENT_TYPES = ("ASSIGNED_EVENT CLOSED_EVENT COMMENT_DELETED_EVENT "
               "CONNECTED_EVENT CONVERTED_TO_DISCUSSION_EVENT "
               "DEMILESTONED_EVENT DISCONNECTED_EVENT LABELED_EVENT LOCKED_EVENT MARKED_AS_DUPLICATE_EVENT MENTIONED_EVENT "
               "MILESTONED_EVENT PINNED_EVENT "
               "REFERENCED_EVENT RENAMED_TITLE_EVENT REOPENED_EVENT "
               "SUBSCRIBED_EVENT TRANSFERRED_EVENT UNASSIGNED_EVENT UNLABELED_EVENT UNLOCKED_EVENT USER_BLOCKED_EVENT "
               "UNMARKED_AS_DUPLICATE_EVENT UNPINNED_EVENT UNSUBSCRIBED_EVENT ISSUE_COMMENT_PINNED_EVENT "
               "ISSUE_COMMENT_UNPINNED_EVENT ISSUE_TYPE_ADDED_EVENT ISSUE_TYPE_REMOVED_EVENT ISSUE_TYPE_CHANGED_EVENT "
               "ISSUE_FIELD_ADDED_EVENT ISSUE_FIELD_REMOVED_EVENT ISSUE_FIELD_CHANGED_EVENT SUB_ISSUE_ADDED_EVENT "
               "SUB_ISSUE_REMOVED_EVENT PARENT_ISSUE_ADDED_EVENT PARENT_ISSUE_REMOVED_EVENT BLOCKED_BY_ADDED_EVENT "
               "BLOCKING_ADDED_EVENT BLOCKED_BY_REMOVED_EVENT BLOCKING_REMOVED_EVENT").split()
ACTOR_TYPES = {"LabeledEvent", "UnlabeledEvent", "ClosedEvent", "ReopenedEvent"}  # the events whose actor the gate reads
REST_NAMES = {"RenamedTitleEvent": "renamed"}
ACTOR = "actor{login __typename}"


def type_name(enum):
    """`ADDED_TO_PROJECT_V2_EVENT` → `AddedToProjectV2Event`."""
    return "".join(p.capitalize() for p in enum.split("_"))


def rest_event(typename):
    """`LabeledEvent` → `labeled`, `RenamedTitleEvent` → `renamed`: the REST /events name of a timeline item."""
    return REST_NAMES.get(typename) or re.sub(r"(?<!^)(?=[A-Z])", "_", typename.removesuffix("Event")).lower()


def search_query(repo, labels, now=None, closed_hours=CLOSED_HOURS):
    """The two search strings (open, recently closed) for issues of `repo` with any of `labels`."""
    any_label = ",".join(f'"{x}"' for x in labels)
    since = ((now or datetime.now(timezone.utc)) - timedelta(hours=closed_hours)).strftime("%Y-%m-%dT%H:%M:%SZ")
    return [f"repo:{repo} is:issue is:open label:{any_label}",
            f"repo:{repo} is:issue is:closed closed:>={since} label:{any_label}"]


SEARCH = ("query($q:String!,$after:String){search(query:$q,type:ISSUE,first:100,after:$after){"
          "pageInfo{hasNextPage endCursor} nodes{... on Issue{number}}}}")


def batch_query(owner, name, numbers):
    """One query reading, for each of `numbers`, everything `to_rest` needs."""
    fragments = " ".join(f"... on {t}{{createdAt{' ' + ACTOR if t in ACTOR_TYPES else ''}"
                         f"{' label{name}' if t in ('LabeledEvent', 'UnlabeledEvent') else ''}}}"
                         for t in map(type_name, EVENT_TYPES))
    item = (f"number databaseId id state stateReason title body updatedAt closedAt author{{login __typename}} "
            f"assignees(first:100){{nodes{{login}}}} labels(first:100){{pageInfo{{hasNextPage}} nodes{{name}}}} "
            f"blockedBy(first:100){{pageInfo{{hasNextPage}} nodes{{number databaseId state}}}} "
            f"comments(last:100){{pageInfo{{hasPreviousPage}} nodes{{databaseId body createdAt author{{login __typename}}}}}} "
            f"timelineItems(first:100,itemTypes:[{','.join(EVENT_TYPES)}]){{pageInfo{{hasNextPage}} "
            f"nodes{{__typename {fragments}}}}}")
    return (f'query{{repository(owner:"{owner}",name:"{name}"){{'
            + "".join(f"i{n}:issue(number:{n}){{{item}}}" for n in numbers) + "}}")


def user(a):
    """A GraphQL author or actor as REST's `user`: a bot's login ends with `[bot]` there."""
    if not a:
        return None
    bot = a.get("__typename") == "Bot"
    return {"login": a["login"] + ("[bot]" if bot and not a["login"].endswith("[bot]") else ""),
            "type": "Bot" if bot else "User"}


def to_rest(node, stage_labels=()):
    """{path: REST body} of one ticket of a batch answer. A part GraphQL paged or answered empty is left out, so
    the gate reads it over REST: a ticket with a stage label but no labeled event is an empty answer (see BATCH)."""
    n, out = node["number"], {}
    labels = [x["name"] for x in node["labels"]["nodes"]]
    issue = {"number": n, "id": node["databaseId"], "node_id": node["id"], "state": node["state"].lower(),
             "state_reason": (node.get("stateReason") or "").lower() or None, "title": node["title"], "body": node["body"],
             "updated_at": node["updatedAt"], "closed_at": node.get("closedAt"), "labels": [{"name": x} for x in labels],
             "assignees": [{"login": a["login"]} for a in node["assignees"]["nodes"]], "user": user(node.get("author"))}
    if not node["labels"]["pageInfo"]["hasNextPage"]:
        out[f"issues/{n}"] = issue
    events = []
    for e in node["timelineItems"]["nodes"]:
        r = {"event": rest_event(e["__typename"]), "created_at": e.get("createdAt")}
        if "actor" in e:
            r["actor"] = user(e["actor"])
        if "label" in e:
            r["label"] = {"name": e["label"]["name"]}
        events.append(r)
    empty = any(x in stage_labels for x in labels) and not any(e["event"] == "labeled" for e in events)
    if not node["timelineItems"]["pageInfo"]["hasNextPage"] and not empty:
        out[f"issues/{n}/events"] = events
    if not node["comments"]["pageInfo"]["hasPreviousPage"]:
        out[f"issues/{n}/comments"] = [{"id": c["databaseId"], "body": c["body"], "created_at": c["createdAt"],
                                        "user": user(c.get("author"))} for c in node["comments"]["nodes"]]
    if not node["blockedBy"]["pageInfo"]["hasNextPage"]:
        out[f"issues/{n}/dependencies/blocked_by"] = [{"number": b["number"], "id": b["databaseId"],
                                                       "state": b["state"].lower()} for b in node["blockedBy"]["nodes"]]
    return out


SIZES = {0xcc: 1, 0xcd: 2, 0xce: 4, 0xcf: 8}  # msgpack unsigned int formats, big-endian after the tag
POLL_ITEM = ("number __typename labels(first:100){nodes{name}} "
             "timelineItems(last:100,itemTypes:[LABELED_EVENT]){pageInfo{hasPreviousPage} nodes{... on LabeledEvent{id label{name}}}}")
POLL = ("query($q:String!,$after:String){search(query:$q,type:ISSUE,first:10,after:$after){"
        "pageInfo{hasNextPage endCursor} nodes{... on Issue{" + POLL_ITEM + "} ... on PullRequest{headRefName " + POLL_ITEM + "}}}}")


def rest_id(node_id):
    """The REST id of a GraphQL node id: `LE_lADO…` → 35260417313 is the last integer of the msgpack fixarray that
    follows the `_`, base64url-encoded. None when the id does not decode."""
    try:
        body = node_id.split("_", 1)[1]
        raw = base64.urlsafe_b64decode(body + "=" * (-len(body) % 4))
        count, i, values = raw[0] - 0x90, 1, []
        if not 0 <= count <= 15:
            return None
        for _ in range(count):
            tag = raw[i]
            if tag <= 0x7f:
                values.append(tag)
                i += 1
            elif tag in SIZES:
                values.append(int.from_bytes(raw[i + 1:i + 1 + SIZES[tag]], "big"))
                i += 1 + SIZES[tag]
            else:
                return None
        return values[-1] if values and i == len(raw) else None
    except Exception:  # not base64, too short, or not a msgpack array: the caller falls back to REST
        return None


def poll_query(repo, labels):
    """The search string of open issues and PRs of `repo` with any of `labels`."""
    return f"repo:{repo} is:open label:{','.join(f'\"{x}\"' for x in labels)}"


def labeled_events(node):
    """{label: REST id of its latest labeled event} of one poll node; None when its timeline has more labeled events
    than were read. A label whose node id does not decode maps to None."""
    tl = node["timelineItems"]
    if tl["pageInfo"]["hasPreviousPage"]:
        return None
    return {e["label"]["name"]: rest_id(e["id"]) for e in tl["nodes"]}  # oldest first: the latest one wins

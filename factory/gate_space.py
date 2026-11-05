"""Read side of the Space code host: the merge request of a ticket and its facts (the keys of gate_port.PR_DEFAULTS).

`api(path)` returns the decoded JSON of `GET <path>` on the Space instance (project `code_host.project`).
A merge request is `{number, title, state: open|merged|closed, branch, head, ci: success|failed|running|none,
labels: [str], reviews: [{author, verdict: approve|changes, commit_id, submitted_at}]}`.
"""
from gate_port import PR_DEFAULTS, ticket_from_ref

CI = {"success": "green", "failed": "red", "running": "pending"}


class SpaceCodeHost:
    def __init__(self, project, api):
        self.project, self.api = project, api

    def _merge_requests(self):
        return self.api(f"projects/key:{self.project}/code-reviews")["data"]

    def ticket_for_pr(self, number, repository=None):
        mr = self.api(f"projects/key:{repository or self.project}/code-reviews/number:{number}")
        return ticket_from_ref(mr.get("branch")) or ticket_from_ref(mr.get("title"))

    def merge_request(self, ticket):
        """The ticket's merge request: the open one with the highest number, else the latest closed or merged."""
        mine = [m for m in self._merge_requests() if (ticket_from_ref(m.get("branch")) or ticket_from_ref(m.get("title"))) == ticket]
        return max(mine, key=lambda m: (m["state"] == "open", m["number"]), default=None)

    def facts(self, ticket):
        mr = self.merge_request(ticket)
        if mr is None:
            return dict(PR_DEFAULTS)
        reviews = [{"author": r["author"], "verdict": r["verdict"], "commit_id": r["commit_id"],
                    "submitted_at": r.get("submitted_at")} for r in mr.get("reviews") or []]
        return {**PR_DEFAULTS, "pr": mr["number"], "head": mr["head"], "branch": mr.get("branch"), "pr_state": mr["state"],
                "ci": CI.get(mr.get("ci"), "none"), "pr_labels": list(mr.get("labels") or []), "reviews": reviews}

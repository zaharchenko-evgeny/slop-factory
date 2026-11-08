# Air service contracts

Recorded from the live Air services on 3 November 2026 by the operator (#330, #329). Tokens, emails and secret
values are removed.

- `air-mcp.json`: the tool lists with input schemas of the `air-automations` and `air-cloud` MCP servers, and one
  real response for every tool the factory calls (`get_environment` for each of the three environments).
  `create_automation`, `run_automation` and `delete_automation` responses come from a throwaway probe automation.
- `github-issues.json`: live GitHub REST responses for the issue endpoints the adapter calls (lookup, listing,
  comments, events, parent, sub-issues, dependencies); list endpoints keep their first item. `FakeGh` in
  `test_gate_github.py` shapes its answers on them and fails on an issue endpoint or field without a recording.
- `github-pulls.json`: live GitHub REST responses for the pull request endpoints the adapter calls (lookup, listing,
  reviews, commits, compare, collaborators, git/ref); list endpoints keep their first item, `commits` and `compare`
  keep one file, and the committer email is removed. `FakeGh` in `test_gate_github.py` shapes its answers on them and
  fails on a covered endpoint or field without a recording.
- `github-actions.json`: live GitHub REST responses for the label list and the Actions endpoints the adapter calls
  (workflow runs of a head, jobs of a run, job log); list endpoints keep their first item and the log keeps its last
  lines with terminal escapes removed. `FakeGh` in `test_gate_github.py` shapes its answers on them and fails on a
  covered endpoint or field without a recording. To match a new adapter call, add it under its path template
  (`{n}`, `{id}`, `{sha}`) to the file and to `ACTION_KEYS`.
- `payloads/<eventType>.json`: real Air trigger payloads, as an Air run received them. Recorded:
  `issues.labeled` and `pull_request_review.submitted`.

Air wraps every GitHub event the same way: `source`, `eventType`, `repository_host`, `repository`, the filter
fields of that event (for example `label`, `pr_author`, `review_state`, `existing_labels`), and `data`, which is
GitHub's webhook payload unchanged. The other trigger events (`pull_request.labeled`, `pull_request.merged`,
`issues.closed`) could not be recorded: an agent can't copy a large payload byte for byte, so fixtures for them
are derived from this wrapper and GitHub's documented webhook payloads, and say so.

`pull_request.labeled`, `pull_request.merged` and `issues.closed` are derived (built from the recorded `issues.labeled`
and `pull_request_review.submitted` wrappers). `uv run --quiet factory/tests/test_contracts.py` checks them, and that the fake MCP
server (`factory/factory_plugins/air/contracts.py serve`) rejects any tool or argument absent from `air-mcp.json`.

`factory contracts refresh` is **operator-only**: it needs your Air OAuth (`FACTORY_MCP_AIR_AUTOMATIONS`,
`FACTORY_MCP_AIR_CLOUD`), never run it from an agent or CI. It re-records the tool lists and the read-only responses into
`air-mcp.json`, strips tokens, emails and secret-like values, and leaves the other recorded responses as they are.

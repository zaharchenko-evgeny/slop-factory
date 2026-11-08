# ADR-0024: Nodes declare the repositories they write

Status: accepted, requested by #975 (slice 1 of 6, #976). Supersedes in part
ADR-0017 and ADR-0021. Replaces the open design in #327.

## Context

A factory can write only to the `main` repository of an environment. The
other repositories are read-only checkouts. To write several repositories,
a team needs:

- one definition environment per repository (`per-repo-env`);
- a hard-coded engine node list (`definition.PER_REPO`);
- a `write_mode` that repeats `write` and `token_env`.

Labels go to every label repository, including read-only ones. Branches
and PRs of one ticket in several repositories depend on a token that Air
does not give to the run.

## Options

1. **A per-run write token for the other repositories.** The node pushes to
   each repository itself with `FACTORY_WRITE_TOKEN`. Rejected: the run
   holds a credential for every repository, the push bypasses Air's
   platform push, and a failed push leaves a half-delivered ticket.
2. **One definition environment per repository (today's `per-repo`).**
   Rejected: the team repeats each environment, and the engine keeps a
   fixed list of nodes that need the copies.
3. **Nodes declare the repositories they write.** The environment is only a
   set of checkouts, and each node says which of them it writes. Chosen.

## Decision

1. An environment is only the set of checkouts: its repositories, with no
   `main`.
2. A node declares the repositories it writes: `writes: [owner/repo, ...]`
   or `writes: all`.
   - A node with no `writes` runs as one automation. Its main is the
     tracker repository, and every other repository is read-only.
   - A node with `writes` runs as one Air automation per written
     repository. That repository is the automation's main, and Air's
     platform push writes to it. Every other repository is read-only.
     Each copy has its own trigger label `<label>/<repo-name>`.
3. The gate adds the suffixed label for the ticket's repository: its
   `repo:<name>` label on GitHub, or its linked project on YouTrack.
   `factory begin` refuses a copy whose label does not match its
   repository.
4. Each label exists only where its event happens:
   - ticket labels in the tracker (on YouTrack, in the linked projects);
   - PR labels in the written repositories;
   - no labels in a read-only repository.
5. Examples: triage and reviewers write nothing. Developers write every
   project repository. The knowledge nodes write only the team repository.

## Consequences

- No node holds a write token. Air's platform push is the only writer.
- `per-repo-env`, `definition.PER_REPO` and `write_mode` can go.
- A ticket that changes several repositories starts one developer run per
  repository. Each run opens the PR of its own repository.
- Each copy of a node needs its own trigger label, so deploy creates more
  automations.
- Earlier text changes in part: ADR-0017 "Air repositories" and "One branch
  name across repositories", and ADR-0021 point 5. Each now carries a
  `Superseded in part by ADR-0024` line.
- Implementation slices (#975): the definition's `writes`; deploy copies,
  GitHub label places and the gate's suffix and begin check; YouTrack label
  places; the team adopts `writes`; engine cleanup. The Air probes (#459)
  belong to slice 1 and report on the ticket.

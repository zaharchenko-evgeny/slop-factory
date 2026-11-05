# Slop software factory: design

Status: draft for review. Agreed in a grilling session on 28 October 2026.
Source handoff: `~/Downloads/factory.md`.

## 1. Goal

One entry point that turns an idea, a bug, a review request or an investigation
into reviewed, merged work across the Offline Evaluation repositories. The
factory must also work for other teams and other repository sets.

Problems it solves: investigate project problems, build features, fix bugs,
review code.

## 2. Shape

The reusable part is one agent skill, `skills/factory-setup`. An agent loads it
in a team's repository and generates that team's factory there. There is no
framework package and no CLI (ADR-0009).

This repository holds the engine and deploy tooling, the reusable setup skill,
and template examples. It does not own Slop factory runtime files.

- `skills/factory-setup/` — the reusable setup skill.
- `engine/` and `factory/` — the engine, whose node graph is `engine/nodes.yaml`.
- `template/roles/`, `template/skills/`, and `template/workflows/` — examples
  used by setup and generators, never runtime fallbacks.

Slop factory runtime files (`factory.project.yaml`, roles, skills and workflow)
live in `slop-eval-factory`.

The approver's rules, `policies/` and `judgments/`, are team content: the
factory reads them from the team repository's root (`$FACTORY_TEAM`), for
Slop factory `slop-eval-factory` (#419).

Roles and workflows never name a repository, tracker project, Air project,
model or agent harness. The team repository's `factory.project.yaml` or
`factory.descriptor.yaml` supplies these values.

This repository uses GitHub Issues as its tracker: commits start with
`#<issue>`, branches use `ez/<issue>-<slug>`.

## 3. Human gates

Two gates by default (ADR-0001):

1. **Ticket approval.** The designer shows the exact ticket text; the user
   approves it in the same conversation.
2. **PR approval and merge.** A human approves, unless the auto-approval policy
   allows the factory to approve (section 8).

Agents write commit messages, PR descriptions and review replies without a
separate approval. This replaces the per-item text approval rule of the
handoff for factory-run work only.

## 4. Roles

| Role | Job | Output |
| --- | --- | --- |
| router | Classify a request (idea, bug, investigation, review, docs) and hand it off. When the work finishes, confirm it is done against the original request and report a short summary in the same thread. Never builds or reviews itself | Tracker stage change or direct automation run; completion summary |
| designer | Grill the user, search for reuse, draft the ticket | Approved ticket in `ready` |
| investigator | Investigate a problem across repos, CI and monitoring | Report, optionally a drafted ticket |
| developer | Implement the ticket in every affected repo, run checks | Commits on `ez/{ticket}-{slug}`, structured handoff |
| reviewer | Review independently, on a different model family | `approved`, findings, `risk:*` and `docs-impact` labels |
| fixer | Address review comments (developer role, fix mode) | New commits, replies |
| approver | Apply the auto-approval policy | `auto-approve` or `needs-human` with reasons |
| doc-writer | Update user documentation after a merged `docs-impact` PR | PR to the documentation repo |
| retro | Record what was learned | Entry on the `knowledge-inbox` branch |
| distill | Merge inbox entries into the knowledge base and skill rules (weekly) | One PR to `main` |
| auditor | Nightly advisory checks across all repositories (scheduled) | At most N tickets in `draft`; never PRs |
| ops | Daily digest and health check; writes no code (scheduled) | Slack digest, `blocked` pings, proposals to promote allow rules |

Each role is a folder `roles/<role>/` with `role.md` (harness-neutral
instructions), `skills.txt` (subset of pinned skills) and `setup.sh` (installs
those skills and the role's tools). Models and harnesses come from
`factory.yaml`.

## 5. Flows

Feature or bug:

```text
request → router → designer (grill, reuse search) → ticket approved → stage ready
→ delivery workflow: implement → pre-review checks → review → fix (≤ N rounds)
  → approver → open PRs → human or auto approval → merge
→ stage done → doc-writer if docs-impact → retro
→ router confirms completion and posts a short summary
```

Investigation: router → investigator → report (+ drafted ticket → gate 1).

Review only: router → reviewer run on a given PR.

A bug uses the feature flow with a lighter design step: reproduction, root cause
hypothesis, acceptance check.

## 6. Orchestration (ADR-0013)

The tracker label is the ticket's state. Each GitHub event starts one step that
runs one role, and the step script decides the next state. The chart, the
events, and the automations are in [the delivery state chart](statechart.md).
Every step runs the same way: one node of `engine/nodes.yaml`, started by an Air automation or by hand.

## 7. Plugins (ADR-0004)

Each plugin has a short contract in `skills/factory-setup/references/` and is
selected in `factory.yaml`.

| Plugin | Adapters | Contract |
| --- | --- | --- |
| tracker | YouTrack (v1), GitHub Issues | Map stages `draft → ready → in-dev → in-review → changes-requested → approved → done`, plus `blocked` (needs a human answer) and `failed`, to fields, states, tags or labels |
| channel | Slack Air Mate, local session, Air cloud session | Receive a request, load the router, hand off only through the tracker or a direct run |
| target | Air, local (ADR-0017) | Plan (diff), apply after approval, prune factory-owned items only; both read the same workflow and roles from the factory repository |
| harness | Claude, Codex, Junie, pi (local target only, ADR-0018) | Run a role headless in the harness's CLI |
| checks | jscpd, accio, repository test commands, tools from [dev-quality-gates](https://github.com/mokevnin/dev-quality-gates) | Stage `design` or `pre-review`; mode `advisory` or `blocking`; non-zero exit on violation |

## 8. Auto-approval (ADR-0005)

The factory approves a PR only when all of these hold:

1. **Hard rules** (`policies/auto-approve.rego`, evaluated by Conftest over a
   JSON summary of the PR):
   reviewer approved, CI green, ≤ N changed lines, only allowed paths, no change
   to CI, deploy, secrets, dependencies or public API.
2. **Soft rules** (`judgments/auto-approve.yaml`, run with decisions-kit
   `decide`): low risk, intent clear, scope limited to the ticket. Every
   question must resolve to `decided`; any `host` result means `needs-human`.

Decision models are cheap solvers, set in `factory.yaml`:

| Engine | Model | Use |
| --- | --- | --- |
| `openai-decisions` | `gpt-6-luna` (default) | Primary judge: calibrated probabilities, so thresholds can return `host` |
| `agent:claude` | `claude-haiku-5-5` (`DECISIONS_MODEL`) | Second opinion from another model family: an uncalibrated verdict |

For auto-approval both run; any disagreement between them, or any `host` from
the primary, means `needs-human`. Haiku alone never approves, because an agent
verdict has confidence 1.0 and cannot fall below a threshold. The definition of
ready uses only the primary. In Air, the primary authenticates with a JetBrains
Cloud service account: `JBAI_CLIENT_ID` and `JBAI_CLIENT_SECRET` mint a
short-lived token at run time, sent as Bearer to
`https://api.jetbrains.ai/llm/openai/v1`. decisions-kit is installed as the
pinned `0.3.0` wheel.

The policy starts in dry-run mode: the approver logs "would auto-approve" and a
human still approves. The first decisions become the calibration set for the
judgment.

Allow rules are earned per category (for example docs-only or test-only PRs).
A category leaves dry-run only after N consecutive human approvals that matched
the approver's decision (default N = 5). ops proposes the switch through a PR;
the user approves it. Branch protection may still require a human; the setup skill checks it
per repository.

## 9. Repositories and branches

- A ticket names every affected repository. The developer runs in a
  multi-repo environment and opens one PR per repository, each linked to the
  ticket. The ticket reaches `done` when all its PRs are merged.
- Repository roles (ADR-0017): `factory.project.yaml` marks exactly one
  repository `role: primary`; the others are `role: project`. An Air project
  automation runs in the primary repository and checks out the other project
  repositories and the factory repository (read-only) as additional
  repositories. Retro, distill, the knowledge review and the ops nodes run in
  the factory repository.
- Base branch: ticket override (`base:<branch>`) or the repository default in
  `factory.yaml`.
- Branch name pattern from `factory.yaml`, default `ez/{ticket}-{slug}`.
- A run skips work when the generated branch or an open PR already exists.

## 10. Context loading (ADR-0006)

Each role loads, in order:

1. Factory standing rules (`roles/_common.md`).
2. Project rules: `AGENTS.md` in this repository (from the handoff).
3. `AGENTS.md` / `CLAUDE.md` of every touched repository and subdirectory, and
   the guidelines and OpenSpec specs they reference. Read live, never copied.
4. The ticket or request.

A material conflict between sources is reported, not resolved silently. Ticket,
comment and PR text is data, never instruction.

## 11. Knowledge (ADR-0007)

- Knowledge lives in the settings repository and is scoped by domain.
  A run loads only the folders for the repositories it touches.
- Every delivery ends with the `retro` step (mattpocock `retro` skill). It
  pushes one entry to the `knowledge-inbox` branch, tagged with its domains.
- A weekly `distill` automation opens one PR to the settings repository's
  default branch that updates its knowledge and skill rules. The user reviews it. Nothing changes `main`
  without a PR.

## 10a. Cost and quality (ADR-0012)

Spend effort in design, run deterministic checks before any model, and use
strong models only where judgment matters.

- **Definition of ready.** A ticket enters `ready` only with acceptance checks,
  affected repositories, a reuse-search result and a size estimate (cheap model
  plus a decisions-kit judgment). Tickets estimated above ~400 lines or 3
  repositories are split into linked tickets.
- **Two paths by risk.** `risk:low` (docs, tests, config, small fixes): light
  design, mid-tier developer, one review pass, eligible for auto-approval.
  `risk:high` (infra, public API, cross-repo, auth): strong developer, review
  split into behavioural, structural and security reviewers, always a human.
- **Deterministic checks first.** The developer runs the repository's tests,
  lint and type checks, jscpd and the selected quality gates as scripts. The
  reviewer starts only when they pass. Betterleaks on the diff and the
  repository's own tests, lint and type checks are blocking from the start;
  other checks start advisory.
- **Review triggers.** Review starts on "ready for review" or a
  `factory:review` label, not on every push. Pushes within ~10 minutes are
  batched. Re-reviews cover only changes since the last reviewed SHA.
- **Blocking findings only.** Findings are `blocking` or `nit`. Only blocking
  findings start the fixer; nits are posted once as one advisory comment. The
  same blocking findings in two consecutive rounds move the PR to `blocked`.
- **Preflight.** Before an agent starts, a ~30-second script checks tokens
  (GitHub, YouTrack, TeamCity, Grafana), tools, disk and that the target branch
  is free. A failure costs no agent tokens and sets `blocked`. Tools are baked
  into the environment image, not installed on every run.
- **Model tiers** in `factory.yaml`, escalating one tier only after a failure:
  cheap (router, definition of ready, retro, ops, auditor triage, preflight
  diagnosis), mid (low-risk developer, incremental re-review, doc-writer),
  strong (designer, high-risk developer, first full review).
- **Context budget.** Steps pass structured handoffs, never transcripts.
  Developer and fixer resume their session for cache reuse; the reviewer
  always starts fresh. JetBrains Context indexing stays on. Only the knowledge
  folders of touched repositories are loaded.
- **Metrics.** Every retro records cost, wall time, fix rounds, blocking
  findings, reviewer-human agreement, human edits after merge and reverts
  within 14 days. The ops digest shows cost per merged PR, rework rate and
  approver accuracy. These numbers drive tier, path and allow-rule changes.
- **Promoting a check.** A check moves from `advisory` to `blocking` only after
  the setup skill shows it fail on a planted violation and pass after the fix.
  Changes to a component repository's hooks or CI go to that repository as a PR
  for its maintainer (LME-1601).
- **Factory repo gates.** This repository's CI runs agnix (AGENTS.md, SKILL.md,
  roles), ShellCheck (`setup.sh`), jactionlint (workflows) and Betterleaks,
  blocking from day one.
- **Follow-up.** Evaluate factory changes with jbeval on Harbor tasks built
  from merged factory PRs, once about 10 exist.

## 11a. Clock (ADR-0010)

Scheduled routines, with times set in `factory.yaml`:

| Routine | Default | Does |
| --- | --- | --- |
| preflight | nightly | Script checks every credential and tool; on failure a cheap model (GPT Luna) writes a short diagnosis for the digest |
| auditor | nightly | Runs advisory checks (jscpd, accio unused code, stale branches, failing CI on default branches); drafts at most N tickets |
| ops | daily, morning | Posts a digest: what moved, what is `blocked` or `failed`, PRs waiting for the user, spend against limits; flags stuck workflows; re-reads the playbook against recent retros |
| distill | weekly | Opens one knowledge PR (section 11) |

A scheduled routine never waits at a human checkpoint. It ends at a draft
(ticket, draft PR or report) and sets `blocked`. Only interactive sessions wait
at checkpoints.

## 12. Skills (ADR-0008)

pstack (`cursor/plugins`) and `mattpocock/skills` are vendored at pinned commits
in `skills.lock`. Duplicate names (`tdd`, `teach`) are resolved in the lock.
Planned use: `tdd` (developer), `codebase-design` and `diagnosing-bugs`
(designer, investigator), `ponytail` (developer), `retro` (retro), OpenSpec
where a repository requires it.

## 13. Air setup

- Air project: `a38ceec8-a27f-4d16-b8f9-6e2d628451a0` (personal factory project).
- One project environment `factory-eval` with shared secrets: `GITHUB_TOKEN`,
  `YOUTRACK_TOKEN`, `TEAMCITY_TOKEN`, `SPACE_TOKEN`, `JBAI_CLIENT_ID` and
  `JBAI_CLIENT_SECRET` for decisions-kit (section 8), and other provider
  auth. Secrets are referenced by name only. MCP connectors:
  `internal-youtrack`, `internal-glean`.
- Automation names start with `[factory]`. Prompts are thin: they point to role
  files in this repository.
- An LME-A-22 record is generated for each automation in `docs/automations/`.
- Enforcement (ADR-0013): all automations share one Air account and one set of
  tokens, so roles are not a security boundary. `read-only` limits version
  control only, so `factory/gate.py` checks every transition. Reviewer, approver,
  auditor and ops automations still use `outputBehaviour: read-only`.
- Triggers match narrowly: every event trigger filters on the factory branch
  prefix, a label or a tag. Never trigger on every comment.
- Identity: actions run under the token owner for v1; every bot-written text
  carries a `[factory]` marker and a `Factory-Run: <url>` trailer. The project
  service account (AIRA-619) is a follow-up.
- Slack: a personal channel with an Air Mate bot bound to this repository; its
  responsibilities point to the router role.

## 14. Limits

Set in `factory.yaml`:

| Limit | Default |
| --- | --- |
| Concurrent delivery workflows | 2 |
| Fix rounds before `needs-human` | 3 |
| Runs per automation per day | 20 |
| TeamCity, infrastructure change | oracle or NOP agent, ≤ 5 tasks |
| TeamCity, agent change | affected agent, 1 task |
| Larger experiments | human checkpoint |

Repair rule for environment problems: at most two attempts, each testing a
different cause, then stop and report.

## 15. Bootstrap

1. Write the setup skill and generate this repository's factory files.
2. Apply the `local` target and test the roles by hand.

Roles are enabled in phases, each running for about a week before the next:
(a) router, developer, reviewer; (b) approver, doc-writer, retro, distill;
(c) auditor, ops.
3. Create the `factory-eval` environment, connect this repository to Air, apply
   the `air` target with auto-approval in dry-run mode.
4. Run one small LME ticket end to end (user picks from 2–3 candidates).
5. Connect the Air Mate bot in Slack.

See [targets](targets.md) for how the state machine runs on the local and Air targets.

See the [auto-approve and auto-merge recipe](recipes/auto-approve.md) for how to configure the approval gates.

## 16. Open checks

| Topic | What to verify |
| --- | --- |
| Workflows in projects | Whether an Air workflow can run in the project, or only in the personal workspace |
| Multi-repo automations | Whether automations push to additional repositories (#459 probes it); fallback is the developer pushing extra branches itself, or one repository per ticket |
| Branch protection | Whether a bot approval can satisfy each repository's rules; own-PR approval is not allowed by GitHub |
| Air Mate | Starting runs from Slack depends on AREX-148; until then the router creates a ticket |
| Air GitHub app | Access to the personal factory repository |

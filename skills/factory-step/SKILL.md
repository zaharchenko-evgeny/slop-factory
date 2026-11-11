---
name: factory-step
description: How any factory role does what its role file asks through the `factory` command — start the step, wait for CI, read run facts, collect lessons, run a routine, and report the outcome with its data. Use whenever a role file names one of these intentions or tells you to report an outcome.
---

# Factory step

You never change labels, stages, reviews or comments yourself. You report one
outcome; the gate checks it and moves the work on. Role files say what to do,
never which command does it; this table is how. Run the installed `factory`
command (`command -v factory`), never `uv run` or a path into the engine.

| The role asks you to | Run |
| --- | --- |
| start the step and see its facts, branch and outcomes | `factory begin` with the arguments the prompt gives |
| wait until CI at the PR head has finished | `factory ci --wait --issue <ticket>`: prints `green`, or `red` with the failing checks |
| read the run facts of recently finished tickets | `factory facts --since <ISO time>` |
| collect the lessons agents reported | `factory retro collect`: the uncollected lesson comments as `[{ticket, url, id, body}]`; it writes nothing |
| mark collected lessons, after the PR that publishes them is open at the pushed commit | `factory retro mark <comment id>...`: marks those comments collected and unlabels a ticket once all its lessons are collected |
| run the routine named in your settings | `factory <routine>` (`reconcile`, `doctor`) with no other arguments |
| answer the mention that woke you (`mention` in the step's facts) | `factory end replied --reply "…"`: the gate posts it where the question was, to whoever asked |
| ask another agent a question in the thread (only when the step lists `ask`) | `factory end ask --to <persona> --text "…"`: the gate posts it with `@factory:<persona>` and your run ends; the answer wakes your node again, with the answer in `agent_notes`; a reply cannot ask; each ask counts against `limits.mention_hops` |
| report your outcome | `factory end <outcome>`, as below |

1. `factory begin` (the prompt gives its arguments) prints the facts, the
   branch and `outcomes`, the outcomes this step may report. Exit 10: do not
   act; report its `reason` and stop.
   When it prints `pr_create`, you open the pull request yourself: push the
   branch, write its body with the `pr` skill to `.factory/pr-body.md`, and
   run the `pr_create` command of the outcome you will report (with the
   subject in place of `<subject>`). `factory end` refuses that outcome
   while the branch has no PR.
2. As the last thing you do, run `factory end <outcome>` once, with the data
   the role file asks for. Quote each value; repeat a list option per item.
   Each lesson for later runs goes in its own `--lesson`:

| Data | Option |
| --- | --- |
| reason, summary, size estimate, reply, message, assignee | `--reason "…"`, `--summary "…"`, `--size-estimate "~120"`, `--reply "…"`, `--message "…"`, `--assignee <login>` |
| failing check on `origin/main` | `--failing-check "<check>"` with `env-broken` |
| acceptance check result | `--pass "<check>"`, `--fail "<check>"`, `--not-run "<check> \| <why>"` |
| finding | `--blocking "<id> <file:line> <finding> \| case: <repro>"`, `--minor "<id> <file:line> <finding>"` |
| re-review: new blocking finding on a file the fix did not change | append `\| unseen: <why the earlier round could not see it>`, or report it `--minor`; the gate refuses it otherwise |
| any other payload field | `--field KEY=VALUE` (repeatable) |
| finding fixed or disputed | `--fixed "<id> <sha>"`, `--disputed "<id> <reason>"`, `--disputed-focuses a,b` |
| new ticket (slice) | `--slice <file.md>`: first line the title, the rest the body; a `repo: <repository>` line under the title creates it in that repository, an `after: <repository or slice number>` line makes it wait for those slices |
| proposed acceptance check | `--propose-check "<check>"` |
| note for a later agent | `--note-for <fixer\|reviewer\|developer\|any>` with `--note-hint "…"`, `--note-repro "<cmd>"`, `--note-file <path>` (≤2,000 chars, no secrets) |
| lesson for later runs | `--lesson "<project\|factory\|engine> <about> \| <text> \| evidence: <x>"` (evidence optional) |

Example: `factory end <outcome> --summary "…" --blocking "B1 src/x.py:10 crashes on '' | case: x('') raises"`

If `factory end` refuses (non-zero exit, error printed), fix the data and run
it again. Never change labels instead.

When a required check fails the same way on `origin/main`, report
`factory end env-broken --failing-check <check>`.

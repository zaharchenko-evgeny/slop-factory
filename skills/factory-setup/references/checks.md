# Checks

Declared in `checks` in `factory.yaml`, per repository.

## Contract

Each entry has `name`, `stage` (`design` or `pre-review`), `mode` (`advisory`
or `blocking`) and `run` (a shell command run from the ticket worktree). A
non-zero exit is a violation.

- New checks start `advisory`.
- A check becomes `blocking` only after it has been shown to fail on a planted
  violation and pass after the fix; keep that evidence in the PR.
- A check that needs a tool the machine lacks must say so, not pass silently.
- Changes to a component repository's hooks or CI go to that repository as a PR.

## dev-quality-gates catalog

- Use https://github.com/mokevnin/dev-quality-gates as the tool catalog.
- For each repository, follow its `docs/adopt.md` in plan mode and propose only
  the entries that fit the repository's languages and tooling.
- Each adopted entry becomes a `checks` item with the catalog's command.

## jscpd

- Copy and paste detector. Command: `npx jscpd --threshold <percent> <paths>`.
- Config: paths and threshold come from the repository; set `.jscpd.json` there.
- Suits stage `pre-review`; start advisory because the baseline is not zero.

## accio

- Dependency and code-hygiene checker run through its CLI; use the repository's
  accio configuration and its documented exit codes.
- Config: the accio config path in the repository, and which rules are blocking.
- Suits stage `pre-review`; start advisory until the baseline is clean.

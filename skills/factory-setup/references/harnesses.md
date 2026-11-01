# Harnesses

Selected per role by `roles.<role>.harness` (a reviewer mode may set its own) in
`factory.descriptor.yaml`, defaulting to `pi`. Values: `pi`,
`claude-code`, `codex`, `junie`; an unknown value fails `sm.py validate`. Model ids
stay per role. Adapters live in `factory/harnesses.py`; the local role runner and
preflight dispatch through them.

## Adapter contract (local target)

- `command`: argv from model, tools, session, skills, role files and prompt.
- `parse`: final text plus usage (`input`, `output`, `cacheRead`, `cacheWrite`,
  `cost`) written to the run's `events.log`; a value the harness does not report
  is `null` (unknown), never 0.
- `probe`: the preflight call per (harness, model).
- A role without edit/write tools is read-only: no bash, write or edit tool on
  `claude-code` and `codex` (`--allowedTools` read tools, `--sandbox read-only`).
- The developer session resumes across fix rounds (`pi --session-id`,
  `claude --session-id` then `--resume`, `codex exec resume <thread>`).
- `claude-code` and `codex` get the role files and each skill's `SKILL.md` as
  text (system prompt, resp. head of the prompt).
- `junie`: stub. Preflight fails with one line until it is implemented.

## Role contract

- The role files stay harness-neutral; only the adapter knows the format.
- Model comes from the role's tier in `factory.descriptor.yaml`; tools come from the role.
- A run takes a prompt, returns the final text, and writes no state outside
  its worktree and run directory.
- Skills in `skills.txt` must be installed; setup reports missing ones.
- `targets.local.model_access: central` applies to `pi` only (projects that keep the local target).
- A read-only reviewer on `claude-code` runs `git diff|log|show|status` only;
  on `codex` it has a read-only sandbox, so the prompt must carry what it needs.

## Claude (`claude-code`)

- Headless run: `claude -p --output-format json` with the role's model,
  `--allowedTools` and the role files and skills as `--append-system-prompt`.
- Read-only roles get read and search tools and read-only git; write tools are
  disallowed.

## Codex (`codex`)

- Headless run: `codex exec --json` with the role's model and a read-only
  sandbox for read-only roles. The developer gets `workspace-write` plus the
  worktree's git dir as a writable root, and resumes with `codex exec resume`.
- Role files and skills are the head of the prompt.

## pi (`pi`)

- No rendering step: pass the role files as system-prompt additions.
- Headless run: `pi -p --model <model> --tools <tools>` with
  `--append-system-prompt roles/_common.md` and
  `--append-system-prompt roles/<role>/role.md`.
- Skills are discovered from `.agents/skills`, the user's `~/.agents/skills` and
  pi's own skill directories.
- Read-only roles get read, search and list tools only.

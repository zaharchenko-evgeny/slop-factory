# Harnesses

Selected per role by `roles.<role>.harness` (a reviewer mode may set its own) in
`factory.yaml`, defaulting to `targets.local.harness`, then `pi`. Values: `pi`,
`claude-code`, `codex`, `junie`; an unknown value fails `sm.py validate`. Model ids
stay per role. Adapters live in `factory/harnesses.py`; `run_role.py` and
`preflight.py` dispatch through them.

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

## Contract

Render a role (`roles/_common.md`, `roles/<role>/role.md`, `skills.txt`) into
the harness's agent or skill format, and run it headless.

- The role files stay harness-neutral; only the adapter knows the format.
- Model comes from the role's tier in `factory.yaml`; tools come from the
  role's `tools` setting (`read-only` means no write or shell tools).
- A run takes a prompt, returns the final text, and writes no state outside
  its worktree and run directory.
- Skills in `skills.txt` must be installed; setup reports missing ones.

## Claude (`claude-code`)

- Render each role as a subagent file in `.claude/agents/<role>.md`: front
  matter with name, description, model and tools; body is the common rules plus
  the role file.
- Skills live in `.claude/skills/<name>/`.
- Headless run: `claude -p --output-format json` with the role's model and allowed tools.
- Read-only roles get read and search tools only.

## Codex (`codex`)

- Rules go in `AGENTS.md`; render each role as a prompt file under
  `.codex/prompts/<role>.md` (common rules plus the role file).
- Headless run: `codex exec --json` with the role's model and a read-only sandbox for
  read-only roles.
- Skills are linked into the Codex skills directory.

## pi (`pi`)

- No rendering step: pass the role files as system-prompt additions.
- Headless run: `pi -p --model <model> --tools <tools>` with
  `--append-system-prompt roles/_common.md` and
  `--append-system-prompt roles/<role>/role.md`.
- Skills are discovered from `.agents/skills`, the user's `~/.agents/skills` and
  pi's own skill directories.
- Read-only roles get read, search and list tools only.

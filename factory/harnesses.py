"""Agent harness adapters of the local target (factory.yaml `roles.<role>.harness`, default
`targets.local.harness`, else `pi`). One adapter per harness, one contract:

  command(call)    argv of one role call: model, tools, session, skills, role files and prompt in
  parse(raw, call) (final text, usage | None) from the harness's stdout; usage keys are input, output,
                   cacheRead, cacheWrite, cost, and a value the harness does not report is None (unknown)
  probe(model)     argv of the short preflight call

`pi` keeps pi's own flags (role files as `--append-system-prompt` paths, `--skill` dirs). `claude-code`
and `codex` get the role files and each skill's SKILL.md as text (the system prompt, resp. the head of
the prompt), so a reviewer's private copies are all it can read. Tools come from the role's pi-style
tool list: a role without edit/write is read-only and gets no bash, write or edit tool on any adapter
but pi (`--allowedTools` read tools for claude-code, `--sandbox read-only` for codex).
$FACTORY_AGENT replaces the command name (tests). `junie` is a stub: it fails with one line.
"""
import json
import os
import uuid
from dataclasses import dataclass, field
from pathlib import Path

HARNESSES = ("pi", "claude-code", "codex", "junie")
STUBS = {"junie": "harness junie is not implemented yet; set another harness in factory.yaml"}
USAGE = ("input", "output", "cacheRead", "cacheWrite", "cost")


@dataclass
class Call:
    ticket: str
    role: str
    model: str
    tools: str  # pi-style, comma separated
    prompt: str
    prompts: list  # role files (paths): common rules, then the role file
    skills: list = field(default_factory=list)  # skill dirs
    run_dir: Path = None  # where a resumable session id is kept


def harness_of(config, role, mode=None):
    """The harness of a role (or of one of its modes): mode, role, target default, `pi`."""
    r = (config.get("roles") or {}).get(role) or {}
    m = (r.get("modes") or {}).get(mode) or {}
    return m.get("harness") or r.get("harness") or config["targets"]["local"].get("harness") or "pi"


def command_name(default):
    return os.environ.get("FACTORY_AGENT", default)


def writes(call):
    return "edit" in call.tools.split(",") or "write" in call.tools.split(",")


def system_text(call):
    parts = [Path(p).read_text() for p in call.prompts]
    parts += [f"# Skill: {Path(s).name}\n\n{(Path(s) / 'SKILL.md').read_text()}" for s in call.skills]
    return "\n\n".join(parts)


def session_id(call):
    return f"factory-{call.ticket}-{call.role}"


def _jsonl(raw):
    for line in raw.splitlines():
        try:
            ev = json.loads(line)
        except ValueError:
            continue
        if isinstance(ev, dict):
            yield ev


class Pi:
    def command(self, c):
        session = ["--session-id", session_id(c)] if c.role == "developer" else ["--no-session"]
        skills = [x for d in c.skills for x in ("--skill", str(d))]
        return [command_name("pi"), "-p", "--mode", "json", "--no-mcp", "--no-skills", "--no-extensions",
                "--no-prompt-templates", *skills, "--model", c.model, *session, "--tools", c.tools,
                "--append-system-prompt", str(c.prompts[0]), "--append-system-prompt", str(c.prompts[1]), c.prompt]

    def parse(self, raw, call=None):
        """(final text, usage) from `pi --mode json` stdout. Output without JSON events is plain text."""
        text, usage, seen = raw, dict.fromkeys(USAGE, 0), False
        for ev in _jsonl(raw):
            msg = ev.get("message")
            if ev.get("type") != "message_end" or not isinstance(msg, dict) or msg.get("role") != "assistant":
                continue
            u = msg.get("usage") or {}
            for k in usage:
                usage[k] += (u.get("cost", {}).get("total", 0) if k == "cost" else u.get(k, 0)) or 0
            parts = msg.get("content")
            text = parts if isinstance(parts, str) else "".join(
                x.get("text", "") for x in parts or [] if x.get("type") == "text")
            seen = True
        return (text, usage) if seen else (raw, None)

    def probe(self, model):
        return [command_name("pi"), "-p", "--no-mcp", "--no-session", "--no-tools", "--model", model, "Reply with OK."]


CLAUDE_TOOLS = {"read": "Read", "bash": "Bash", "edit": "Edit", "write": "Write", "grep": "Grep", "find": "Glob",
                "ls": "LS"}


class ClaudeCode:
    def command(self, c):
        names = [t for t in c.tools.split(",") if writes(c) or t not in ("bash", "edit", "write")]
        allowed = ",".join(CLAUDE_TOOLS[t] for t in names)
        sid = str(uuid.uuid5(uuid.NAMESPACE_URL, session_id(c)))
        marker = c.run_dir / f"{c.role}.session" if c.run_dir else None
        session = []
        if c.role == "developer":  # first call creates the session, later calls (fix rounds) resume it
            session = ["--resume" if marker and marker.exists() else "--session-id", sid]
        cmd = [command_name("claude"), "-p", c.prompt, "--output-format", "json", "--model", c.model, *session,
               "--append-system-prompt", system_text(c), "--allowedTools", allowed]
        if writes(c):
            return cmd + ["--permission-mode", "acceptEdits"]
        return cmd + ["--disallowedTools", "Bash,Edit,Write,NotebookEdit"]

    def parse(self, raw, call=None):
        try:
            data = json.loads(raw)
        except ValueError:
            return raw, None
        for ev in data if isinstance(data, list) else [data]:
            if isinstance(ev, dict) and ev.get("type") == "result" and isinstance(ev.get("result"), str):
                u = ev.get("usage") or {}
                if call and call.role == "developer" and call.run_dir and not ev.get("is_error"):
                    (call.run_dir / f"{call.role}.session").write_text(str(ev.get("session_id") or ""))
                return ev["result"], {"input": u.get("input_tokens"), "output": u.get("output_tokens"),
                                      "cacheRead": u.get("cache_read_input_tokens"),
                                      "cacheWrite": u.get("cache_creation_input_tokens"),
                                      "cost": ev.get("total_cost_usd")}
        return raw, None

    def probe(self, model):
        return [command_name("claude"), "-p", "Reply with OK.", "--model", model, "--allowedTools", "Read"]


class Codex:
    def command(self, c):
        sandbox = "workspace-write" if writes(c) else "read-only"
        prompt = f"{system_text(c)}\n\n---\n\n{c.prompt}"
        marker = c.run_dir / f"{c.role}.session" if c.run_dir else None
        thread = marker.read_text().strip() if c.role == "developer" and marker and marker.exists() else ""
        base = [command_name("codex"), "exec"]
        if thread:  # resume takes no --sandbox flag
            return [*base, "resume", "--json", "--skip-git-repo-check", "-m", c.model, "-c",
                    f'sandbox_mode="{sandbox}"', thread, prompt]
        return [*base, "--json", "--skip-git-repo-check", "-m", c.model, "--sandbox", sandbox, prompt]

    def parse(self, raw, call=None):
        text, usage, thread = raw, None, None
        for ev in _jsonl(raw):
            item = ev.get("item") or {}
            if ev.get("type") == "thread.started":
                thread = ev.get("thread_id")
            elif ev.get("type") == "item.completed" and item.get("type") == "agent_message":
                text = item.get("text") or ""
            elif ev.get("type") == "turn.completed":
                u = ev.get("usage") or {}
                usage = {"input": u.get("input_tokens"), "output": u.get("output_tokens"),
                         "cacheRead": u.get("cached_input_tokens"), "cacheWrite": None, "cost": None}
        if thread and call and call.role == "developer" and call.run_dir:
            (call.run_dir / f"{call.role}.session").write_text(thread)
        return (text, usage) if usage or thread else (raw, None)

    def probe(self, model):
        return [command_name("codex"), "exec", "--skip-git-repo-check", "--sandbox", "read-only", "-m", model,
                "Reply with OK."]


ADAPTERS = {"pi": Pi(), "claude-code": ClaudeCode(), "codex": Codex()}


def adapter(name):
    """The adapter, or SystemExit with one line for a stub or unknown harness."""
    if name in STUBS:
        raise SystemExit(STUBS[name])
    if name not in ADAPTERS:
        raise SystemExit(f"unknown harness {name!r}; one of {', '.join(HARNESSES)}")
    return ADAPTERS[name]


def config_errors(config):
    """Every harness named in factory.yaml that is not one of HARNESSES (for `sm.py validate`)."""
    seen = []
    local = (config.get("targets") or {}).get("local")
    if isinstance(local, dict) and "harness" in local:
        seen.append(("targets.local.harness", local["harness"]))
    for role, r in (config.get("roles") or {}).items():
        if not isinstance(r, dict):
            continue
        for where, spec in [(f"roles.{role}", r), *((f"roles.{role}.modes.{m}", s) for m, s in
                                                    (r.get("modes") or {}).items() if isinstance(s, dict))]:
            if "harness" in spec:
                seen.append((f"{where}.harness", spec["harness"]))
    return [f"{w}: unknown harness {h!r}, one of {', '.join(HARNESSES)}" for w, h in seen
            if not isinstance(h, str) or h not in HARNESSES]

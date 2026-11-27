"""Claude Code headless (skills/factory-setup/references/harnesses.md)."""
import shutil

from ports.agent import probe_cli


class Runner:
    name, cli, skill_dir = "claude-code", "claude", ".claude/skills"
    env, auth_files = ("ANTHROPIC_API_KEY",), ("~/.claude", "~/.claude.json")  # what an isolated executor passes in (ADR-0029)
    api_hosts = ("api.anthropic.com",)

    def command(self, prompt, model, write, isolated=False):
        """Read-only nodes get no edit tools, while keeping `Skill` and `Bash` so skills and linters can run."""
        tools = "Skill,Bash,Read,Edit,Write,Grep,Glob" if write else "Skill,Read,Grep,Glob,Bash"
        # the prompt comes first: `--allowedTools` takes every following word that is not an option
        return ["claude", "-p", prompt, "--output-format", "json", "--model", model.split("/")[-1], "--allowedTools", tools,
                *(["--permission-mode", "acceptEdits"] if write else [])]

    def probe(self, which=shutil.which):
        return probe_cli(self, which)

"""Claude Code headless (skills/factory-setup/references/harnesses.md)."""
import shutil

from ports.agent import probe_cli


class Runner:
    name, cli, skill_dir = "claude-code", "claude", ".claude/skills"

    def command(self, prompt, model, write):
        """Read-only nodes get no edit tools."""
        tools = "Bash,Read,Edit,Write,Grep,Glob" if write else \
            "Read,Grep,Glob,Bash(git diff:*),Bash(git log:*),Bash(git show:*),Bash(git status:*),Bash(factory:*)"
        # the prompt comes first: `--allowedTools` takes every following word that is not an option
        return ["claude", "-p", prompt, "--output-format", "json", "--model", model.split("/")[-1], "--allowedTools", tools,
                *(["--permission-mode", "acceptEdits"] if write else [])]

    def probe(self, which=shutil.which):
        return probe_cli(self, which)

"""pi headless (skills/factory-setup/references/harnesses.md)."""
import shutil

from ports.agent import probe_cli


class Runner:
    name, cli, skill_dir = "pi", "pi", ".agents/skills"
    env, auth_files = ("ANTHROPIC_API_KEY", "OPENAI_API_KEY"), ("~/.pi/agent/auth.json",)  # what an isolated executor passes in (ADR-0029)
    api_hosts = ("api.anthropic.com", "api.openai.com")

    def command(self, prompt, model, write):
        """pi takes the full `provider/model`."""
        return ["pi", "-p", "--model", model, prompt]

    def probe(self, which=shutil.which):
        return probe_cli(self, which)

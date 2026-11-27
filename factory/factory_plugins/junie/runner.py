"""Junie headless (skills/factory-setup/references/harnesses.md)."""
import shutil

from ports.agent import probe_cli


class Runner:
    name, cli, skill_dir = "junie", "junie", ".agents/skills"
    env, auth_files = ("JUNIE_API_KEY",), ("~/.junie",)  # what an isolated executor passes in (ADR-0029)
    api_hosts = ("api.jetbrains.ai",)

    def command(self, prompt, model, write, isolated=False):
        """Junie keeps its terminal tools; a read-only node is held by the gate and by no push."""
        return ["junie", "--task", prompt, "--model", model.split("/")[-1], "--output-format", "json"]

    def probe(self, which=shutil.which):
        return probe_cli(self, which)

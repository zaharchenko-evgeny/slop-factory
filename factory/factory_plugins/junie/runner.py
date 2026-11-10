"""Junie headless (skills/factory-setup/references/harnesses.md)."""
import shutil

from ports.agent import probe_cli


class Runner:
    name, cli, skill_dir = "junie", "junie", ".agents/skills"

    def command(self, prompt, model, write):
        """Junie keeps its terminal tools; a read-only node is held by the gate and by no push."""
        return ["junie", "--task", prompt, "--model", model.split("/")[-1], "--output-format", "json"]

    def probe(self, which=shutil.which):
        return probe_cli(self, which)

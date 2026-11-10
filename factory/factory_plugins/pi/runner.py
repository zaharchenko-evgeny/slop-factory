"""pi headless (skills/factory-setup/references/harnesses.md)."""
import shutil

from ports.agent import probe_cli


class Runner:
    name, cli, skill_dir = "pi", "pi", ".agents/skills"

    def command(self, prompt, model, write):
        """pi takes the full `provider/model`."""
        return ["pi", "-p", "--model", model, prompt]

    def probe(self, which=shutil.which):
        return probe_cli(self, which)

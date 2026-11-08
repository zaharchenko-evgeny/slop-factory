"""Codex headless (skills/factory-setup/references/harnesses.md)."""
import shutil

from ports.agent import probe_cli


class Runner:
    name, cli, skill_dir = "codex", "codex", ".agents/skills"

    def command(self, prompt, model, write):
        """`factory begin` and `end` call GitHub, so even a reviewer's sandbox needs the network."""
        return ["codex", "exec", "--json", "-m", model.split("/")[-1], "--sandbox", "workspace-write",
                "-c", "sandbox_workspace_write.network_access=true", prompt]

    def probe(self, which=shutil.which):
        return probe_cli(self, which)

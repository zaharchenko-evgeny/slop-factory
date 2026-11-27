"""Codex headless (skills/factory-setup/references/harnesses.md)."""
import shutil

from ports.agent import probe_cli


class Runner:
    name, cli, skill_dir = "codex", "codex", ".agents/skills"
    env, auth_files = ("OPENAI_API_KEY",), ("~/.codex",)  # what an isolated executor passes in (ADR-0029)
    api_hosts = ("api.openai.com",)

    def command(self, prompt, model, write, isolated=False):
        """`factory begin` and `end` call GitHub, so even a reviewer's sandbox needs the network.
        Inside an isolated container/microVM, bypass Codex's nested sandbox so git worktree commits and skill scripts work."""
        if isolated:
            return ["codex", "exec", "--json", "-m", model.split("/")[-1],
                    "--dangerously-bypass-approvals-and-sandbox", prompt]
        return ["codex", "exec", "--json", "-m", model.split("/")[-1], "--sandbox", "workspace-write",
                "-c", "sandbox_workspace_write.network_access=true", prompt]

    def probe(self, which=shutil.which):
        return probe_cli(self, which)

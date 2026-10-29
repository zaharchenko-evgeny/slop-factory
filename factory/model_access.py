"""Model access of the local target (factory.yaml `targets.local.model_access`).

`env` (default): role calls inherit the caller's environment, as before.
`central`: Claude calls go through the JetBrains Central proxy. `pi_env` starts the proxy once with
`jbcentral proxy start` when `jbcentral status` does not show it running, reads the claude-code route
from the Central agent config (`env.ANTHROPIC_BASE_URL` in ~/.claude/settings.json, written by
`jbcentral add`), and writes `.factory/pi-agent/models.json` (dir mode 0700, file 0600): the user's
~/.pi/agent/models.json with only `providers.anthropic.baseUrl` set to the route, so other providers
keep working. Every pi call then gets `PI_CODING_AGENT_DIR` and a placeholder `ANTHROPIC_API_KEY`.
No route, key or provider value is ever logged. `$FACTORY_JBCENTRAL` overrides the `jbcentral` command.
"""
import json
import os
import re
import subprocess
import threading
from pathlib import Path

AGENT_DIR = ".factory/pi-agent"
PLACEHOLDER_KEY = "factory-central-proxy"
ROUTE = re.compile(r"https?://(127\.0\.0\.1|localhost):\d+/wire/[^/\s]+/claude-code/anthropic/?")


class ModelAccessError(RuntimeError):
    pass


def mode(config):
    return config["targets"]["local"].get("model_access", "env")


def _jbcentral(*args):
    try:
        r = subprocess.run([os.environ.get("FACTORY_JBCENTRAL", "jbcentral"), *args], capture_output=True, text=True)
    except OSError:
        raise ModelAccessError("jbcentral not found on PATH") from None
    return r.returncode, re.sub(r"\x1b\[[0-9;]*m", "", r.stdout + r.stderr)


def _proxy_running():
    return bool(re.search(r"Proxy\s+running", _jbcentral("status")[1]))


def _route():
    settings = Path.home() / ".claude/settings.json"
    try:
        url = (json.loads(settings.read_text()).get("env") or {}).get("ANTHROPIC_BASE_URL") or ""
    except (OSError, ValueError):
        url = ""
    if not ROUTE.fullmatch(url):
        raise ModelAccessError(f"no JetBrains Central claude-code route in {settings}; run `jbcentral add` for Claude")
    return url


def pi_env(config, root):
    """Extra environment for every pi call; empty for `env`. ModelAccessError (one line) on failure."""
    if mode(config) != "central":
        return {}
    if not _proxy_running():
        _jbcentral("proxy", "start")
        if not _proxy_running():
            raise ModelAccessError("JetBrains Central proxy is not running after `jbcentral proxy start`")
    route = _route()
    user = Path.home() / ".pi/agent/models.json"
    try:
        models = json.loads(user.read_text()) if user.exists() else {}
    except ValueError:
        raise ModelAccessError(f"{user} is not valid JSON") from None
    providers = models.setdefault("providers", {})
    providers["anthropic"] = {**(providers.get("anthropic") or {}), "baseUrl": route}
    agent = Path(root) / AGENT_DIR
    agent.mkdir(mode=0o700, parents=True, exist_ok=True)
    agent.chmod(0o700)
    # Parallel role calls may rewrite it: write a private temp file, then rename over it atomically.
    tmp = agent / f"models.json.{os.getpid()}.{threading.get_ident()}.tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        json.dump(models, f, indent=2)
    os.replace(tmp, agent / "models.json")
    return {"PI_CODING_AGENT_DIR": str(agent), "ANTHROPIC_API_KEY": PLACEHOLDER_KEY}

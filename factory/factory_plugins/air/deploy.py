#!/usr/bin/env python3
# /// script
# requires-python = ">=3.12"
# dependencies = ["pyyaml>=6", "jinja2>=3"]
# ///
"""Render the nodes of engine/nodes.yaml as deployable specs (ADR-0014). Output is JSON.

Usage: deploy.py plan --target air
       deploy.py (--dry-run | --apply [--allow-delete]) [--rename 'OLD NAME=NEW NAME']...
--dry-run prints the diff against the existing automations and environments; --apply also writes it and re-lists to
verify. The two Air MCP servers are started over stdio from the commands in FACTORY_MCP_AIR_AUTOMATIONS and
FACTORY_MCP_AIR_CLOUD (they use the operator's local OAuth). Each --rename updates the existing item named OLD in
place (same id) to NEW instead of creating NEW and deleting OLD.
air: {"automations": [...], "environments": [...]}: one automation per enabled node (per tier or focus
     where the node has them) and one environment per environment in use, for the factory-deploy skill.
"""
import json
import os
import shlex
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

ENGINE = Path(__file__).resolve().parents[2]  # factory/: the core modules are imported by bare name
sys.path.insert(0, str(ENGINE))
import config as config_mod  # noqa: E402
import definition  # noqa: E402
import deploy as core  # noqa: E402
import nodes as nodes_mod  # noqa: E402
import plugins  # noqa: E402
import scopes as scopes_mod  # noqa: E402


def node_copies(defn: dict, name: str, n: dict, config: dict) -> list[dict]:
    """ADR-0024: the automation copies of a definition node: one per listened repository for an event node, else one
    in its written repository (or the tracker). A copy has its `repo`, name `suffix`, the `additional` checkouts and
    the `output`: the node's own where it writes the repository, else `read-only`."""
    listens, writes = definition.node_listens(defn, name), definition.node_writes(defn, name)
    env = definition._parts(defn, name)[3]
    by_name = {definition.full_name(r["url"]): r for r in config["repositories"]}

    def repo_of(full: str) -> dict:
        return by_name.get(full) or {"name": full.split("/")[-1], "url": f"https://github.com/{full}", "default_branch": "main"}
    targets = listens or (writes[:1] or definition.tracker_repositories(defn)[:1])
    copies = []
    for full in targets:
        others = [definition.full_name(r["repo"]) for r in env.get("repositories") or [] if definition.full_name(r["repo"]) != full]
        copies.append({"repo": repo_of(full), "suffix": f" {full.split('/')[-1]}" if len(listens) > 1 else "",
                       "additional": [{"repository": core.repository(repo_of(o)), "branch": repo_of(o)["default_branch"]}
                                      for o in others],
                       "output": n.get("output", "read-only") if full in writes else "read-only"})
    return copies


def plan_air(nodes: dict, config: dict) -> dict:
    air, roles, agents = config["targets"]["air"]["models"], config["roles"], core.agents_of(nodes, config)
    for aid in sorted({a["id"] for a in agents.values() if a.get("effort")}):
        print(f"deploy: agent {aid}: effort ignored, Air automations take no reasoning setting", file=sys.stderr)
    repos = {r["name"]: r for r in config["repositories"]}
    primary = next(r for r in config["repositories"] if r.get("role") == "primary")
    team = {"name": "team", "url": config.get("team_repository") or primary["url"]}
    repos["team"] = team
    envs_def = nodes.get("environments") or {}
    scheds = nodes.get("schedules") or {}
    bot = config["targets"]["air"]["bot_login"]
    prefix = f"[{core.project_name(config)}] "
    automations = []
    repo_tags = tuple(t for hook in plugins.registry(config).hook("deploy.repo_tags") for t in hook(config))
    used = list(dict.fromkeys(n["env"] for n in nodes["nodes"].values() if n["role"] != "human" and not n.get("runner")))
    unscoped = set()  # environments some automation shares across the project
    scoped = []  # (env, node) of the per-node automations
    defn = core.team_definition()
    filter_hooks = plugins.registry(config).hook("deploy.trigger_filters")  # a copy's ticket triggers, narrowed to its repository
    # #1069: environments with their own trackers are the factory's projects; a node of the default environment runs
    # once per project, in its environment (FACTORY_ENV), and the gate takes only that environment's tickets
    trackers = scopes_mod.project_envs(config)
    default_env = core.default_environment(nodes, defn) if trackers else None
    for name, n in nodes["nodes"].items():
        if n.get("enabled") is False or n["role"] == "human" or n.get("runner"):
            continue
        fan = list(trackers) if trackers and n["env"] == default_env else [None]
        own = node_copies(defn, name, n, config) if defn and fan == [None] else [None]  # #1069: per project instead
        for project, copy_, (aname, tier_arg, tier, focus) in [
                (p, c, v) for p in fan for c in own for v in core.variants(name, n, roles, agents.get(name))]:
            if project and focus == "knowledge" and project != fan[0]:
                continue  # the knowledge review runs once, in the team's environment
            project = None if focus == "knowledge" else project
            if project and project not in used:
                used.append(project)
            env = project or n["env"]
            # one of several copies (ADR-0024): its ticket triggers narrow to its repository (youtrack: its project)
            scope = copy_["repo"] if copy_ and copy_["suffix"] else None
            agent, _, model = air[tier].partition("/")
            trig = []
            webhook = any(str(t).startswith("webhook ") for t in n["triggers"])
            spec = core.node_spec(name, n, config, tier_arg or tier, focus, nodes)
            narrow = {k: v for hook in filter_hooks for k, v in hook(config, scope).items()} if scope else {}
            for t in n["triggers"]:
                if str(t).startswith("schedule "):
                    trig.append({"triggerType": "SCHEDULED", "cronExpression": scheds[str(t)[9:]]["cron"],
                                 "timezone": "UTC"})
                elif str(t).startswith("webhook "):
                    trig.append({"triggerType": "WEBHOOK"})
                elif str(t) != "manual":
                    trig.append(core.trigger(t, tier_arg, bot if aname.startswith(core.BOT_FILTERED) else None, focus,
                                             scope["name"] if scope else None, repo_tags))
                    if project:
                        trig[-1] = core.scope_filter(trig[-1], trackers[project])
                    if more := narrow.get(trig[-1].get("source")):
                        trig[-1] = {**trig[-1], "filters": {**trig[-1].get("filters", {}), **more}}
            automation_repository = core.repository(team if focus == "knowledge" else (copy_ or {}).get("repo")
                                                    or repos.get(envs_def[env]["repository"]) or primary)
            if focus == "knowledge":  # Air runs an automation only in an environment of its own repository
                env = next((e for e in used if (repos.get(envs_def[e]["repository"]) or primary)["url"] == team["url"]), env)
            # A node with custom MCP servers runs in an environment of its own: setup writes `.mcp.json` per environment
            env_node = name if defn and focus != "knowledge" and definition.mcp_custom(definition.mcp(defn, name)) else None
            if env_node:
                if (env, env_node) not in scoped:
                    scoped.append((env, env_node))
            else:
                unscoped.add(env)
            automation = {
                "name": f"{prefix}{aname}" + (copy_ or {}).get("suffix", "") + (f" {project}" if project else ""),
                "triggers": trig,
                "triggerVcs": [{"outputBehaviour": (copy_ or n).get("output", "read-only")} for _ in trig],
                "spec": spec,
                "agentAndModel": {"id": model, "name": model, "isDefault": False, "agentId": agent},
                "repository": automation_repository,
                "environmentConfigId": f"{prefix}{env}" + (f" {env_node}" if env_node else ""),
                "mcpConnectors": core.mcp_connectors(n, config, focus),
                "prompt": core.script_prompt(name, automation_repository, config, webhook) if n["role"] == "script" else core.agent_prompt(
                    name, n, config, aname, focus, automation_repository, tier_arg or tier),
            }
            if copy_:
                if copy_["additional"]:
                    automation["additionalRepositories"] = copy_["additional"]
            elif automation_repository["fullName"] != core.repository(team)["fullName"]:
                automation["additionalRepositories"] = core.additional_repositories(automation_repository, team, config)
            else:
                projects = core.other_projects(automation_repository, team, config)
                if projects:
                    automation["additionalRepositories"] = [
                        {"repository": core.repository(r), "branch": r["default_branch"]} for r in projects]
            automations.append(automation)
    envs = []
    registry = plugins.registry(config)
    issues_token = config.get("issues_token_env")  # the gate's issue, label and comment writes to project repositories
    for env, node_name in [(e, None) for e in used if e in unscoped] + scoped:
        d = envs_def[env]
        repo = repos.get(d["repository"]) or primary
        envs.append({
            "name": f"{prefix}{env}" + (f" {node_name}" if node_name else ""),
            "repository": repo["url"],
            "startup": d["startup"],
            "variables": {**(d.get("variables") or {}), "FACTORY_ENV": env, **({"FACTORY_NODE": node_name} if node_name else {})},
            "secrets": list(dict.fromkeys([*d.get("secrets", []),
                                           *(s for hook in registry.hook("deploy.secrets") for s in hook(env, config)),
                                           *([issues_token] if issues_token and len(config["repositories"]) > 1 else [])])),
            "domains": d.get("domains", []),
            "vm": d.get("vm", "default"),
        })
    label_repos = list(dict.fromkeys(x for x in ((config.get("tracker") or {}).get("repository"),
                                                 core.repository(primary)["fullName"]) if x))
    project_repos = [r for r in dict.fromkeys(b.get("repository") for b in trackers.values()  # a project's GitHub issues
                                              if scopes_mod.adapter(b) == "github-issues") if r]
    label_repos += [r for r in project_repos if r not in label_repos]
    # the tracker's own setup (youtrack: tags and field values); none keeps a GitHub plan (and its fixture) unchanged
    tag_plan = {k: v for hook in registry.hook("deploy.plan") for k, v in hook(nodes, config).items()}
    schema = core.tracker_schema(nodes, config)
    return {"prefix": prefix, "automations": automations, "environments": envs,
            "label_plan": core.label_plan(nodes, config, label_repos),  # ADR-0024: labels per repository
            "label_specs": schema.specs, "owned_label_prefixes": list(schema.owned_prefixes),
            **tag_plan, "files": core.actions_files(nodes, config)}


class Mcp:
    """A minimal MCP client over stdio (newline-delimited JSON-RPC): initialize, then tools/call."""

    def __init__(self, command: str):
        self.proc = subprocess.Popen(shlex.split(command), stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True)
        self.n = 0
        self.rpc("initialize", {"protocolVersion": "2024-11-05", "capabilities": {},
                                "clientInfo": {"name": "factory-deploy", "version": "0"}})
        self._send({"jsonrpc": "2.0", "method": "notifications/initialized"})

    def _send(self, msg: dict) -> None:
        self.proc.stdin.write(json.dumps(msg) + "\n")
        self.proc.stdin.flush()

    def rpc(self, method: str, params: dict):
        self.n += 1
        self._send({"jsonrpc": "2.0", "id": self.n, "method": method, "params": params})
        for line in self.proc.stdout:
            msg = json.loads(line)
            if msg.get("id") == self.n:
                if "error" in msg:
                    raise RuntimeError(f"{method}: {msg['error']}")
                return msg["result"]
        raise RuntimeError(f"{method}: server closed")

    def call(self, tool: str, args: dict):
        res = self.rpc("tools/call", {"name": tool, "arguments": args})
        if res.get("isError"):
            raise RuntimeError(f"{tool}: {res['content']}")
        text = "".join(c.get("text", "") for c in res.get("content", []))
        if not text:
            return res.get("structuredContent")  # the text is the tool's JSON; structuredContent may wrap it
        try:
            return json.loads(text)
        except ValueError:
            return {"text": text}  # a tool that answers in prose (delete_automation)

    def close(self) -> None:
        self.proc.stdin.close()
        self.proc.wait(timeout=10)
        self.proc.stdout.close()


class Backend:
    """The fields not yet exposed by air-automations MCP, through the Air automation REST API."""

    SPEC_FIELDS = ("name", "repository", "branch", "prompt", "agentAndModel", "triggers", "triggerVcs", "kind",
                   "executor", "botDevSettings", "paused", "outputDescription", "outboundWebhooks", "mcpConnectors",
                   "environmentConfigId", "projectId")
    API_BASE_URL = "https://api.jetbrains.cloud/air-automations-backend/api/v1"

    def __init__(self, base_url: str | None = None, token: str | None = None, run=subprocess.run):
        self.base_url = (base_url or self.API_BASE_URL).rstrip("/")
        self._token = token
        self.run = run

    def token(self) -> str:
        if self._token is None:
            env = os.environ.copy()
            env.update({"JCP_MCP_API_URL": "https://api.jetbrains.cloud", "JCP_MCP_CLIENT_ID": "pi-lot",
                        "JCP_MCP_REDIRECT_PATH": "/space/auth",
                        "JCP_MCP_CONFIG_DIR": str(Path.home() / ".config/jcp-mcp-bridge-pilot")})
            result = self.run(["jcp-mcp-bridge", "token", "--env", "prod", "--audience", "air-automations-backend"],
                              check=True, capture_output=True, text=True, env=env)
            self._token = result.stdout.strip()
            if not self._token:
                raise RuntimeError("jcp-mcp-bridge returned an empty Air automation token")
        return self._token

    def request(self, method: str, path: str, body: dict | None = None):
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(f"{self.base_url}{path}", data=data, method=method,
                                     headers={"Authorization": f"Bearer {self.token()}",
                                              "Accept": "application/json", "Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=30) as response:
            raw = response.read()
        return json.loads(raw) if raw else {}

    def get_automation(self, automation_id: str) -> dict:
        return self.request("GET", f"/automations/{urllib.parse.quote(automation_id, safe='')}")

    def set_additional(self, automation_id: str, repositories: list[dict], attempts: int = 12, pause: float = 10) -> None:
        for attempt in range(attempts):  # an automation created a moment ago answers 403 until Air settles
            try:
                current = self.get_automation(automation_id)
                break
            except urllib.error.HTTPError as e:
                if e.code not in (403, 404) or attempt == attempts - 1:
                    raise
                time.sleep(pause)
        body = {key: current[key] for key in self.SPEC_FIELDS if current.get(key) is not None}  # the API rejects nulls
        if "triggers" in body:
            body["triggers"] = [{key: value for key, value in trigger.items()
                                 if key not in ("triggerId", "automationSnapshotId")}
                                for trigger in body["triggers"]]
        body.update({"id": current.get("automationId", automation_id), "additionalRepositories": repositories})
        self.request("POST", "/automations", body)


def _items(res) -> list[dict]:
    return res if isinstance(res, list) else next(v for v in res.values() if isinstance(v, list))


def same(key: str, have, want) -> bool:
    if key == "repository" and isinstance(have, dict) and have.keys() == {"fullName"}:
        return have["fullName"] == want["fullName"]
    if key == "additionalRepositories":
        def identities(items):
            return sorted((item.get("repository", {}).get("fullName") if isinstance(item.get("repository"), dict)
                           else item.get("repository"), item.get("branch")) for item in (items or []))
        return identities(have) == identities(want)
    if key == "mcpConnectors":
        return sorted(have or []) == sorted(want or [])
    if key == "domains":
        return set(want or []) <= set(have or [])
    if key == "secrets":  # deploy cannot set or delete one: a missing one is `set by a person`, an extra one is theirs
        return True
    if key == "variables":  # Air cannot delete a variable: an extra one is a by-hand removal, not a difference
        return all(k in (have or {}) and have[k] == v for k, v in (want or {}).items())
    return have == want


def create_environment_args(p: dict, live: dict | None = None) -> dict:
    """Air `create_environment` arguments for a planned environment; `live` is the record it replaces, whose domains and
    network settings it keeps."""
    live = live or {}
    live_domains = live.get("domains") or []
    variables = p.get("variables") or {}
    args = {"name": p["name"], "repositoryUrl": p["repository"],
            "allowedDomains": live_domains + [d for d in p.get("domains") or [] if d not in live_domains],
            "variables": [{"key": k, "value": v} for k, v in variables.items()] if isinstance(variables, dict) else variables}
    args.update({k: live[k] for k in ("networkAccess", "predefinedDomainLists") if live.get(k)})
    return args


def environment_detail(x: dict) -> dict:
    """Normalize get_environment's cloud fields to the deploy plan's environment shape."""
    x = dict(x)
    if {"serviceHost", "organization", "repositoryName"} <= x.keys():
        x["repository"] = f"https://{x.pop('serviceHost')}/{x.pop('organization')}/{x.pop('repositoryName')}"
    if "allowedDomains" in x:
        x["domains"] = x.pop("allowedDomains")
    if "instanceTypeKey" in x:
        x["vm"] = x.pop("instanceTypeKey") or "default"
    if isinstance(x.get("variables"), list):
        x["variables"] = {v["key"]: v["value"] for v in x["variables"] if not v.get("isSecret")}
    if "personalSecrets" in x:
        x["secrets"] = [s.get("key") or s["name"] for s in x.pop("personalSecrets")]  # Air lists them by `key`
    return x


def diff(plan: dict, automations: list[dict], envs: list[dict]) -> list[tuple]:
    """(action, kind, name, planned item, existing item, changed fields) per create, update, delete; by name, prefix only.
    `plan["renames"]` ({old name: new name}) matches an existing item under its old name, so the update keeps its id."""
    new_to_old = {new: old for old, new in (plan.get("renames") or {}).items()}
    env_ids = {e["name"]: e.get("id") for e in envs}
    # Air cannot move an environment to another repository: it is replaced, and its automations follow the new one.
    replaced = {e["name"] for e in plan["environments"] for x in envs
                if x["name"] == e["name"] and "repository" in x and x["repository"] != e["repository"]}
    env_ids.update({name: f"(replaced) {name}" for name in replaced})
    out = []
    for kind, planned, have in (("environment", plan["environments"], envs), ("automation", plan["automations"], automations)):
        have = {x["name"]: x for x in have if x["name"].startswith(plan["prefix"])}
        for raw in planned:
            p = {k: v for k, v in raw.items() if k != "spec"}
            x = have.pop(p["name"], None) or have.pop(new_to_old.get(p["name"]), None)
            want = {**p, "environmentConfigId": env_ids.get(p.get("environmentConfigId"), p.get("environmentConfigId"))}
            changed = [k for k in p if x is not None and (kind != "environment" or k in x)
                       and not same(k, x.get(k), want[k])]
            if kind == "environment" and p["name"] in replaced:
                out.append(("replace", kind, p["name"], p, x, ["repository"]))
            elif x is None or changed:
                out.append(("create" if x is None else "update", kind, p["name"], p, x, changed))
        out += [("delete", kind, n, None, x, []) for n, x in have.items()]
    return out


def automation_detail(x: dict) -> dict:
    """A get_automation result as the plan's shape: `id` for `automationId`, no per-trigger server fields."""
    x = {**x, "id": x["automationId"]}
    raw = x.get("triggers") or []
    x["triggers"] = [{k: v for k, v in t.items() if k not in ("triggerId", "isEnabled", "automationSnapshotId")
                                          and not (t.get("triggerType") == "WEBHOOK" and k in ("url", "webhookUrl"))}
                     for t in x.get("triggers") or []]
    x["webhooks"] = [{"id": t.get("triggerId"), "url": t.get("url") or t.get("webhookUrl")}
                     for t in (y for y in raw if y.get("triggerType") == "WEBHOOK")]
    if isinstance(x.get("repository"), str):  # "owner/repo" stands for the planned repository object
        x["repository"] = {"fullName": x["repository"]}
    return x


def env_url(x: dict) -> str | None:
    if {"serviceHost", "organization", "repositoryName"} <= x.keys():
        return f"https://{x['serviceHost']}/{x['organization']}/{x['repositoryName']}"
    return x.get("repository") or x.get("repositoryUrl")


def list_envs(cloud: Mcp, repository_urls) -> list[dict]:
    """The environments of every repository in `repository_urls` (one URL or a list), each once."""
    seen, out = set(), []
    for url in [repository_urls] if isinstance(repository_urls, str) else repository_urls:
        for x in _items(cloud.call("list_environments", {"repositoryUrl": url})):
            if x["id"] not in seen:
                seen.add(x["id"])
                out.append(x)
    return out


def read_state(automations: Mcp, cloud: Mcp, prefix: str, repository_url, *, backend=None,
               additional_names=()) -> tuple[list[dict], list[dict]]:
    listed = automations.call("list_automations", {"privacy": "private"})
    automation_items = listed if isinstance(listed, list) else listed["personal"]
    automation_items = [automation_detail(automations.call("get_automation", {"automationId": x["automationId"]}))
                        for x in automation_items if x["name"].startswith(prefix)]
    if backend:
        for item in automation_items:
            if item["name"] in additional_names:
                record = backend.get_automation(item["id"])
                item["additionalRepositories"] = record.get("additionalRepositories") or []
    env_items = list_envs(cloud, repository_url)
    env_items = [environment_detail(cloud.call("get_environment", {"id": x["id"]}))
                 for x in env_items if x["name"].startswith(prefix)]
    return automation_items, env_items


def apply_diff(changes: list[tuple], automations: Mcp, cloud: Mcp, repo_url="", *, backend=None,
               env_repositories=None) -> None:
    """Environments before automations on create, update and replace, after them on delete. A replaced environment
    is created under its new repository; the old one is renamed `retired <name>` first (Air names are unique) and left
    for a person to delete in the Air UI, since the Air MCP has no environment delete.
    `env_repositories` ({environment name: planned repository URL}) picks the right one of two same-named environments."""
    order = {"create": 0, "update": 0, "replace": 0, "delete": 1}
    env_repositories = env_repositories or {}
    env_ids, retired = None, []
    for action, kind, name, p, x, changed in sorted(changes, key=lambda c: (order[c[0]], (c[1] == "automation") == (c[0] != "delete"))):
        client, noun = (cloud, "environment") if kind == "environment" else (automations, "automation")
        additional = (p or {}).get("additionalRepositories") if kind == "automation" else None
        if action == "replace":
            client.call("edit_environment", {"id": x["id"], "name": f"retired {x['name']}"})
            client.call("create_environment", create_environment_args(p, x))
            retired.append(x["id"])
            print(f"retired environment {x['name']} ({x['id']}) is renamed 'retired {x['name']}': delete it in the Air UI",
                  file=sys.stderr)
            continue
        if p and kind == "automation":  # the environment id exists only after its create
            env_ids = env_ids or {e["name"]: e["id"] for e in list_envs(cloud, repo_url)
                                  if e["id"] not in retired and (env_url(e) is None or e["name"] not in env_repositories
                                                                 or env_url(e) == env_repositories[e["name"]])}
            p = {**p, "environmentConfigId": env_ids[p["environmentConfigId"]]}
        p = {k: v for k, v in (p or {}).items() if k not in ("spec", "additionalRepositories")}
        if action == "update":
            # triggers and triggerVcs pair by position: update_automation takes both or neither
            pair = {"triggers", "triggerVcs"} if kind == "automation" and {"triggers", "triggerVcs"} & set(changed) else set()
            p = {k: v for k, v in p.items() if k in changed or k in pair}
        if kind == "environment" and isinstance(p.get("variables"), dict):
            p["variables"] = [{"key": key, "value": value} for key, value in p["variables"].items()]
        tool_action = ("edit" if kind == "environment" else "update") if action == "update" else action
        id_key = "automationId" if kind == "automation" else "id"
        if action == "update":
            if p:
                client.call(f"{tool_action}_{noun}", {**p, id_key: x["id"]})
            if additional is not None and "additionalRepositories" in changed:
                if backend is None:
                    raise RuntimeError("Air automation backend is required to set additionalRepositories")
                backend.set_additional(x["id"], additional)
        elif action == "create":
            result = client.call(f"create_{noun}", create_environment_args(p) if kind == "environment" else p)
            if additional is not None:
                if backend is None:
                    raise RuntimeError("Air automation backend is required to set additionalRepositories")
                automation_id = result.get("automationId") or result.get("id")
                if not automation_id:
                    raise RuntimeError(f"create_automation returned no automation id for {name}")
                backend.set_additional(automation_id, additional)
        elif kind == "environment":  # air-cloud has no delete_environment
            print(f"remove by hand in Air: environment {name}", file=sys.stderr)
        else:  # confirmName guards against deleting the wrong automation
            client.call("delete_automation", {id_key: x["id"], "confirmName": x["name"]})


def missing_secrets(plan: dict, envs: list[dict]) -> list[str]:
    """`set by a person: <env> secret <NAME>` for each declared secret the environment in Air does not have; the
    deploy never sets a secret value and does not fail on it."""
    have = {x["name"]: set(x.get("secrets") or []) for x in envs}
    return [f"set by a person: {e['name'][len(plan['prefix']):]} secret {s}" for e in plan["environments"]
            for s in e.get("secrets") or [] if s not in have.get(e["name"], set())]


def extra_variables(plan: dict, envs: list[dict]) -> list[str]:
    """`remove by hand in Air: <env> variable <NAME>` for each variable in Air that the plan does not have."""
    have = {x["name"]: x.get("variables") or {} for x in envs}
    return [f"remove by hand in Air: {e['name']} variable {k}" for e in plan["environments"]
            for k in have.get(e["name"], {}) if k not in (e.get("variables") or {})]


def repository_urls(plan: dict) -> list[str]:
    """The repositories whose environments the deploy lists: the planned environments' and, since an environment moved
    off a repository is found there and then replaced, the automations' repositories too."""
    return list(dict.fromkeys([e["repository"] for e in plan["environments"]] +
                              [a["repository"]["browseUrl"] for a in plan["automations"]
                               if isinstance(a.get("repository"), dict) and a["repository"].get("browseUrl")])) or [""]


def deploy(mode: str, allow_delete: bool, plan: dict, automations: Mcp, cloud: Mcp, *, backend=None, run=None, yt_run=None,
           config=None) -> int:
    """Write only to Air, to repository labels (through `run`, a `gh` runner) and to the tracker's own setup (through
    `yt_run` for YouTrack): deploy never creates, edits or closes a tracker ticket. `config` selects the plugins that
    provision the labels (github when None) and the tracker setup (every plugin's, for what the plan names, when None)."""
    repo_urls = repository_urls(plan)
    env_repositories = {e["name"]: e["repository"] for e in plan["environments"]}
    additional_names = {a["name"] for a in plan["automations"] if "additionalRepositories" in a}
    backend = backend or (Backend() if additional_names else None)
    state = read_state(automations, cloud, plan["prefix"], repo_urls, backend=backend,
                       additional_names=additional_names)
    changes = diff(plan, *state)
    for action, kind, name, _, _, changed in changes:
        print(f"{action} {kind} {name}" + (f" ({', '.join(changed)})" if changed else ""))
    if not changes:
        print("unchanged")
    for line in missing_secrets(plan, state[1]):
        print(line)
    for line in extra_variables(plan, state[1]):
        print(line)
    label_changes = core.reconcile_labels(plan, run, config)
    # the tracker's own setup beside the labels (youtrack: tags and field values), from the `deploy.tracker` hooks;
    # without a config, every plugin's hook sets up what the plan names
    setup = plugins.registry(config) if config is not None else plugins.Registry(plugins.KNOWN)
    trackers = [p for hook in setup.hook("deploy.tracker") if (p := hook(plan, yt_run)) is not None]
    if errors := [p.error for p in trackers if p.error]:
        for e in errors:
            print(f"error: {e}", file=sys.stderr)
        return 1
    if mode == "--dry-run":
        for action, repo, label, *rest in label_changes:
            print(f"{action} label {label} in {repo}" + (f" ({', '.join(rest[1])})" if action == "update" else ""))
        for line in [x for p in trackers for x in p.dry_run()]:
            print(line)
        return 0
    for line in [x for p in trackers for x in p.write()]:  # a missing tag fails every handoff that adds it
        print(line)
    core.provision_labels(plan, label_changes, "write", run, config)  # a missing label fails every handoff that adds it
    removals = [c for c in label_changes if c[0] == "remove"]
    refusals = [x for p in trackers for x in p.refusals()]
    if (removals or refusals or any(c[0] == "delete" for c in changes)) and not allow_delete:
        for _, repo, label, _ in removals:
            print(f"refused: would remove label {label} in {repo}", file=sys.stderr)
        for line in refusals:
            print(line, file=sys.stderr)
        print("refused: the diff deletes items; rerun with --allow-delete", file=sys.stderr)
        return 1
    core.provision_labels(plan, removals, "remove", run, config)
    for p in trackers:
        for line in p.remove():
            print(line)
    if changes:
        apply_diff(changes, automations, cloud, repo_urls, backend=backend, env_repositories=env_repositories)
        after = read_state(automations, cloud, plan["prefix"], repo_urls, backend=backend,
                           additional_names=additional_names)
        state = after
        left = [c for c in diff(plan, *after) if not (c[0] == "delete" and c[1] == "environment")]
        for action, kind, name, *_ in left:
            print(f"verify failed: {action} {kind} {name} still differs", file=sys.stderr)
        if left:
            return 1
    for x in state[0]:  # a person registers each webhook with its sender; also on an unchanged plan
        for w in x["webhooks"]:
            print(f"webhook {x['name']}: trigger {w['id']}" + (f" {w['url']}" if w["url"] else ""))
    return 0


def main(argv: list[str]) -> int:
    argv = config_mod.pop_team(argv)
    rest, renames = argv[1:], {}
    while "--rename" in rest:
        i = rest.index("--rename")
        old, sep, new = (rest[i + 1] if i + 1 < len(rest) else "").partition("=")
        if not (old and sep and new):
            print("--rename needs 'OLD NAME=NEW NAME'", file=sys.stderr)
            return 2
        renames[old] = new
        del rest[i:i + 2]
    if argv and argv[0] in ("--dry-run", "--apply") and set(rest) <= {"--allow-delete"}:
        config = config_mod.load()
        if core.check_fixture_drift(config):
            return 1
        names = ("FACTORY_MCP_AIR_AUTOMATIONS", "FACTORY_MCP_AIR_CLOUD")
        if not all(os.environ.get(v) for v in names):
            print("set FACTORY_MCP_AIR_AUTOMATIONS and FACTORY_MCP_AIR_CLOUD (operator only)", file=sys.stderr)
            return 2
        servers = [Mcp(os.environ[v]) for v in names]
        try:
            plan = {**plan_air(nodes_mod.load(), config), "renames": renames}
            run = next((hook(config) for hook in plugins.registry(config).hook("deploy.run")), None)
            code = deploy(argv[0], "--allow-delete" in argv, plan, *servers, run=run, config=config)
            if code:
                return code
            code = core.write_files(plan["files"], argv[0] == "--apply")
            return code or core.sync_wake_files(config, run, argv[0] == "--apply")
        finally:
            for server in servers:
                server.close()
    if len(argv) != 3 or argv[0] != "plan" or argv[1] != "--target" or argv[2] != "air":
        print(__doc__)
        return 2
    config = config_mod.load()
    out = plan_air(nodes_mod.load(), config)
    print(json.dumps(out, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))

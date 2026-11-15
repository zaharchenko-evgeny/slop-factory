#!/usr/bin/env python3
# /// script
# requires-python = ">=3.12"
# dependencies = ["pyyaml>=6", "jinja2>=3"]
# ///
"""Render the nodes of the team's definition as deployable specs (ADR-0021). Output is JSON.

Usage: deploy.py plan --target air
       deploy.py (--dry-run | --apply [--allow-delete]) [--rename 'OLD NAME=NEW NAME']... [--only SELECTOR]...
--dry-run prints the diff against the existing automations and environments; --apply also writes it and re-lists to
verify. The two Air MCP servers are started over stdio from the commands in FACTORY_MCP_AIR_AUTOMATIONS and
FACTORY_MCP_AIR_CLOUD (they use the operator's local OAuth). Each --rename updates the existing item named OLD in
place (same id) to NEW instead of creating NEW and deleting OLD.
air: {"automations": [...], "environments": [...]}: one automation per enabled node and one environment per environment in use,
     for the factory-deploy skill.
"""
import json
import os
import re
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


def definition_copies(defn: dict, name: str, n: dict, config: dict) -> list[dict]:
    """ADR-0027: the automation copies of node `name` from `definition.copies`, in `node_copies`' shape plus the
    copy's `env` and its tracker's trigger `filters`. A copy is named after its main repository when the node has
    several, and after its environment too when two of them share that repository."""
    by_name = {definition.full_name(r["url"]).lower(): r for r in config["repositories"]}

    def repo_of(full: str) -> dict:
        return by_name.get(full.lower()) or {"name": full.split("/")[-1], "url": f"https://github.com/{full}",
                                             "default_branch": "main"}
    found = definition.copies(defn, name)
    out = []
    for c in found:
        shared = sum(1 for x in found if x["main"] == c["main"]) > 1
        suffix = (f" {c['main'].split('/')[-1]}" if len(found) > 1 else "") + (f" {c['env']}" if shared else "")
        contract = definition.contract(defn, "tracker", c["tracker"]) if c["tracker"] else None
        ci = ((defn["factory"].get("environments") or {}).get(c["env"]) or {}).get("ci") or {}
        out.append({"env": c["env"], "repo": repo_of(c["main"]), "suffix": suffix, "ci": ci.get("workflow"),
                    "additional": [{"repository": core.repository(repo_of(o)), "branch": repo_of(o)["default_branch"]}
                                   for o in c["additional"]],
                    "output": n.get("output", "read-only") if c["write"] else "read-only",
                    "filters": contract.filters(c["tracker"]) if contract and hasattr(contract, "filters") else {},
                    "event": contract.event if contract and hasattr(contract, "event") else None})
    return out


def plan_air(nodes: dict, config: dict) -> dict:
    air = config["targets"]["air"]["models"]
    repos = {r["name"]: r for r in config["repositories"]}
    primary = next(r for r in config["repositories"] if r.get("role") == "primary")
    team = {"name": "team", "url": config.get("team_repository") or primary["url"]}
    repos["team"] = team
    envs_def = nodes.get("environments") or {}
    scheds = nodes.get("schedules") or {}
    bot = config["targets"]["air"]["bot_login"]
    project_id = config["targets"]["air"].get("project_id")  # #1387: unset keeps personal automations
    prefix = f"[{core.project_name(config)}] "
    automations = []
    repo_tags = tuple(t for hook in plugins.registry(config).hook("deploy.repo_tags") for t in hook(config))
    used = list(dict.fromkeys(n["env"] for n in nodes["nodes"].values() if n["role"] != "human"))
    unscoped = set()  # environments some automation shares across the project
    scoped = []  # (env, node, repository) of the per-node automations and of the copies in another repository
    defn = core.team_definition()
    filter_hooks = plugins.registry(config).hook("deploy.trigger_filters")  # a copy's ticket triggers, narrowed to its repository
    # #1069: environments with their own trackers are the factory's projects; a node of the default environment runs
    # once per project, in its environment (FACTORY_ENV), and the gate takes only that environment's tickets
    adopted = bool(defn) and definition.adopted(defn)  # ADR-0027: copies come from the definition, no #1069 fan-out
    trackers = {} if adopted else scopes_mod.project_envs(config)
    default_env = definition.default_environment(defn) if trackers else None
    for name, n in nodes["nodes"].items():
        if n.get("enabled") is False or n["role"] == "human":
            continue
        fan = list(trackers) if trackers and n["env"] == default_env else [None]
        if adopted:
            own = definition_copies(defn, name, n, config) or [None]
        else:
            own = node_copies(defn, name, n, config) if fan == [None] else [None]  # #1069: per project instead
        aname = name
        tier = n.get("tier") or (config["roles"].get(n["role"]) or {}).get("tier") or "cheap"  # the model alias
        for project, copy_ in [(p, c) for p in fan for c in own]:
            if project and project not in used:
                used.append(project)
            env = (copy_ or {}).get("env") or project or n["env"]
            if env not in used:
                used.append(env)
            # one of several copies (ADR-0024): its ticket triggers narrow to its repository (youtrack: its project);
            # ADR-0027: the copy's tracker contract gives its `filters` instead
            scope = copy_["repo"] if copy_ and copy_["suffix"] and not adopted else None
            agent, _, model = air[tier].partition("/")
            trig = []
            spec = core.node_spec(name, n, config, tier, None, nodes)
            narrow = {k: v for hook in filter_hooks for k, v in hook(config, scope).items()} if scope else {}
            for t in n["triggers"]:
                if str(t).startswith("schedule "):
                    trig.append({"triggerType": "SCHEDULED", "cronExpression": scheds[str(t)[9:]]["cron"],
                                 "timezone": "UTC"})
                elif str(t).startswith("webhook "):
                    trig.append({"triggerType": "WEBHOOK"})
                elif str(t) != "manual":
                    t = (copy_ or {}).get("event")(t) if (copy_ or {}).get("event") else t  # ADR-0027: the copy's tracker
                    if str(t).startswith("workflow_run ") and (copy_ or {}).get("ci"):
                        t = f"workflow_run {copy_['ci']}"  # #1259: the CI of the copy's own environment
                    trig.append(core.trigger(t, None, bot if aname.startswith(core.BOT_FILTERED) else None, None,
                                             scope["name"] if scope else None, repo_tags, bot))
                    if project:
                        trig[-1] = core.scope_filter(trig[-1], trackers[project])
                    more = {**narrow.get(trig[-1].get("source"), {}),
                            **(((copy_ or {}).get("filters") or {}).get(trig[-1].get("source")) or {})}
                    if more:
                        trig[-1] = {**trig[-1], "filters": {**trig[-1].get("filters", {}), **more}}
            automation_repository = core.repository((copy_ or {}).get("repo")
                                                    or repos.get(envs_def[env]["repository"]) or primary)
            # A node with custom MCP servers runs in an environment of its own: setup writes `.mcp.json` per environment
            env_node = name if definition.mcp_custom(definition.mcp(defn, name)) else None
            env_repo = repos.get(envs_def[env]["repository"]) or primary
            # #1112: a copy in another repository runs in that repository's variant of its environment
            variant = copy_["repo"] if copy_ and \
                definition.full_name(copy_["repo"]["url"]) != definition.full_name(env_repo["url"]) else None
            env_suffix = (f" {env_node}" if env_node else "") + (f" {variant['name']}" if variant else "")
            if env_node or variant:
                if (env, env_node, variant) not in scoped:
                    scoped.append((env, env_node, variant))
            else:
                unscoped.add(env)
            automation = {
                "name": f"{prefix}{aname}" + (copy_ or {}).get("suffix", "") + (f" {project}" if project else ""),
                "triggers": trig,
                "triggerVcs": [{"outputBehaviour": (copy_ or n).get("output", "read-only")} for _ in trig],
                "spec": spec,
                "agentAndModel": {"id": model, "name": model, "isDefault": False, "agentId": agent},
                "repository": automation_repository,
                "environmentConfigId": f"{prefix}{env}{env_suffix}",
                "mcpConnectors": core.mcp_connectors(n, config, None),
                "prompt": core.agent_prompt(name, n, config, aname, None, automation_repository, tier),
            }
            if copy_:
                if copy_["repo"].get("default_branch", "main") != "main":  # Air checks out `main` unless told (#1259)
                    automation["branch"] = copy_["repo"]["default_branch"]
                if copy_["additional"]:
                    automation["additionalRepositories"] = copy_["additional"]
            elif automation_repository["fullName"] != core.repository(team)["fullName"]:
                automation["additionalRepositories"] = core.additional_repositories(automation_repository, team, config)
            else:
                projects = core.other_projects(automation_repository, team, config)
                if projects:
                    automation["additionalRepositories"] = [
                        {"repository": core.repository(r), "branch": r["default_branch"]} for r in projects]
            if project_id:
                automation["projectId"] = project_id
            automations.append(automation)
    for env, d in envs_def.items():  # #1377: an environment no node uses is planned when people or agents use its roles
        if d.get("roles") and env not in unscoped:  # also when its nodes run only in per-node or per-repository copies
            used += [] if env in used else [env]
            unscoped.add(env)
    envs = []
    registry = plugins.registry(config)
    urls = {r["name"]: definition.full_name(r["url"]) for r in config.get("repositories") or []}
    issues_token = config.get("issues_token_env")  # deprecated (#1236): the old tracker-wide token, as before
    project_names = {definition.full_name(r["url"]).lower() for r in config.get("repositories") or []
                     if r.get("role") == "project"}
    for env, node_name, variant in [(e, None, None) for e in used if e in unscoped] + scoped:
        d = envs_def[env]
        repo = variant or repos.get(d["repository"]) or primary
        own = definition.full_name(repo["url"])  # Air's GH_TOKEN writes it: a repository's `token` is for the others
        variables = {**(d.get("variables") or {}), "FACTORY_ENV": env, "FACTORY_ENV_REPOSITORY": own,
                     **({"FACTORY_NODE": node_name} if node_name else {})}
        declared = [x for x in d.get("declared", []) if x["name"] not in variables]
        envs.append({
            "name": f"{prefix}{env}" + (f" {node_name}" if node_name else "") + (f" {variant['name']}" if variant else ""),
            "repository": repo["url"],
            "startup": d["startup"],
            "variables": variables,
            "secrets": list(dict.fromkeys([*d.get("secrets", []),
                                           *(s for hook in registry.hook("deploy.secrets") for s in hook(env, config)),
                                           *([issues_token] if issues_token and project_names - {own.lower()} else []),
                                           *(t for r, t in ((config.get("environments") or {}).get(env) or {}).get("tokens", {}).items() if urls.get(r, r).lower() != own.lower())])),
            "domains": d.get("domains", []),
            "vm": d.get("vm", "default"),
            **({"declared": declared} if declared else {}),
        })
    label_repos = list(dict.fromkeys(x for x in ((config.get("tracker") or {}).get("repository"),
                                                 core.repository(primary)["fullName"]) if x))
    project_repos = [r for r in dict.fromkeys(b.get("repository") for b in trackers.values()  # a project's GitHub issues
                                              if scopes_mod.adapter(b) == "github-issues") if r]
    label_repos += [r for r in project_repos if r not in label_repos]
    if adopted:  # ADR-0027: where copies hear events; a repository with no tickets and no writes gets none
        cased = {core.repository(r)["fullName"].lower(): core.repository(r)["fullName"] for r in config["repositories"]}
        label_repos = [cased.get(h, h) for h in definition.event_repositories(defn)]
    # the tracker's own setup (youtrack: tags and field values); none keeps a GitHub plan (and its fixture) unchanged
    tag_plan = {k: v for hook in registry.hook("deploy.plan") for k, v in hook(nodes, config).items()}
    schema = core.tracker_schema(nodes, config)
    return {"prefix": prefix, "automations": automations, "environments": envs,
            **({"projectId": project_id} if project_id else {}),
            "label_plan": core.label_plan(nodes, config, label_repos),  # ADR-0024: labels per repository
            "label_specs": schema.specs, "owned_label_prefixes": list(schema.owned_prefixes),
            **tag_plan}


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

    def list_automations(self, project_id: str | None = None) -> list[dict]:
        """Every personal automation, page by page (`cursor`): the MCP `list_automations` stops at LIST_CAP (#1347).
        With `project_id` (#1387): the automations of that project, from `GET /automations/projects`."""
        out, cursor = [], None
        path = "/automations/projects?limit=100" if project_id else "/automations?limit=100"
        while True:
            page = self.request("GET", path + (f"&cursor={urllib.parse.quote(cursor)}" if cursor else ""))
            out += [x for x in page.get("automations") or [] if not project_id or x.get("projectId") == project_id]
            cursor = page.get("nextCursor")
            if not page.get("hasMore") or not cursor:
                return out

    @staticmethod
    def _settled(call, attempts: int, pause: float):
        """`call()`, retried while Air answers 403 or 404: an automation created a moment ago does until it settles,
        for reads and writes alike (#1122)."""
        for attempt in range(attempts):
            try:
                return call()
            except urllib.error.HTTPError as e:
                if e.code not in (403, 404) or attempt == attempts - 1:
                    raise
                time.sleep(pause)

    def set_additional(self, automation_id: str, repositories: list[dict], attempts: int = 12, pause: float = 10) -> None:
        current = self._settled(lambda: self.get_automation(automation_id), attempts, pause)
        body = {key: current[key] for key in self.SPEC_FIELDS if current.get(key) is not None}  # the API rejects nulls
        if "triggers" in body:
            body["triggers"] = [{key: value for key, value in trigger.items()
                                 if key not in ("triggerId", "automationSnapshotId")}
                                for trigger in body["triggers"]]
        body.update({"id": current.get("automationId", automation_id), "additionalRepositories": repositories})
        self._settled(lambda: self.request("POST", "/automations", body), attempts, pause)


class EnvBackend(Backend):
    """Project-owned environments through the air-backend REST API that the Air web UI uses: air-cloud's MCP sees
    personal environments only and `create_environment` has no project field."""

    API_BASE_URL = "https://api.jetbrains.cloud/air-backend"
    AUDIENCE = "air-backend"

    def token(self) -> str:
        if self._token is None:
            env = os.environ.copy()
            env.update({"JCP_MCP_API_URL": "https://api.jetbrains.cloud", "JCP_MCP_CLIENT_ID": "pi-lot",
                        "JCP_MCP_REDIRECT_PATH": "/space/auth",
                        "JCP_MCP_CONFIG_DIR": str(Path.home() / ".config/jcp-mcp-bridge-pilot")})
            result = self.run(["jcp-mcp-bridge", "token", "--env", "prod", "--audience", self.AUDIENCE],
                              check=True, capture_output=True, text=True, env=env)
            self._token = result.stdout.strip().split("\n")[-1].strip()
            if not self._token:
                raise RuntimeError("jcp-mcp-bridge returned an empty Air backend token")
        return self._token

    def project_environments(self, project_id: str, prefix: str) -> list[dict]:
        """The project's environments whose name starts with `prefix`, in the plan's shape, each with `projectId`."""
        q = urllib.parse.quote(project_id, safe="")
        listed = _items(self.request("GET", f"/env-configs?orgProjectId={q}") or [])
        return [{**environment_detail(self.request("GET", f"/env-configs/{urllib.parse.quote(x['id'], safe='')}")),
                 "projectId": project_id} for x in listed if x["name"].startswith(prefix)]

    def duplicate(self, source_id: str, project_id: str) -> dict:
        """Copy a personal environment into the project (the UI's Change project, mode DUPLICATE): the copy keeps the
        source's repository, domains, variables and startup scripts; the source and its automations stay as they are."""
        path = f"/env-configs/{urllib.parse.quote(source_id, safe='')}/ownership-change"
        preview = self.request("POST", f"{path}/preview", {"targetProjectId": project_id, "requestedMode": "DUPLICATE"})
        return self.request("POST", path, {"targetProjectId": project_id, "requestedMode": "DUPLICATE",
                                           "previewToken": preview["previewToken"]})

    def create(self, p: dict, project_id: str) -> dict:
        """A new project environment for a planned one that has no personal source; deploy sets its variables after."""
        return self.request("POST", "/env-configs", {"name": p["name"], "repositoryUrl": p["repository"],
                                                     "orgProjectId": project_id})


def project_environment_sources(plan: dict, missing: list[str], personal: list[dict]) -> dict[str, dict | None]:
    """{missing project environment name: the personal environment of that name and repository it is copied from, or
    None (created new)}."""
    repos = {e["name"]: e["repository"] for e in plan["environments"]}
    return {name: next((x for x in personal if x["name"] == name and not x.get("projectId")
                        and same_repository(env_url(x), repos[name])), None) for name in missing}


def create_project_environments(plan: dict, sources: dict, env_backend: EnvBackend) -> None:
    by_name = {e["name"]: e for e in plan["environments"]}
    for name, source in sources.items():
        if source:
            env_backend.duplicate(source["id"], plan["projectId"])
        else:
            env_backend.create(by_name[name], plan["projectId"])
        print(f"created project environment {name}" + (f" (copy of personal {source['id']})" if source else ""))


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
    if key == "repository" and isinstance(have, str) and isinstance(want, str):  # #1304: Air lowercases the organization
        return same_repository(have, want)
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
        x["variable_keys"] = [v["key"] for v in x["variables"]]  # secret ones too: deploy never overwrites one
        x["variables"] = {v["key"]: v["value"] for v in x["variables"] if not v.get("isSecret")}
    if "personalSecrets" in x and not x.get("secrets"):
        x["secrets"] = x.pop("personalSecrets")
    if isinstance(x.get("secrets"), list):  # MCP lists personal ones, REST (air-backend) every one, as records
        x["secrets"] = [s if isinstance(s, str) else s.get("key") or s["name"] for s in x["secrets"]]
    return x


LIST_CAP = 100  # list_automations returns at most this many personal automations, with no paging (#1338)


class Listed(list):
    """The personal automations read from Air; `capped` when the list has `LIST_CAP` entries, so it may miss some."""
    capped = False


def diff(plan: dict, automations: list[dict], envs: list[dict], capped: bool = False) -> list[tuple]:
    """(action, kind, name, planned item, existing item, changed fields) per create, update, delete; by name, prefix only.
    `plan["renames"]` ({old name: new name}) matches an existing item under its old name, so the update keeps its id.
    `capped` (the automation list may be incomplete, #1338): a planned automation not found is `unknown`, not `create`,
    and nothing is deleted."""
    new_to_old = {new: old for old, new in (plan.get("renames") or {}).items()}
    env_ids = {e["name"]: e.get("id") for e in envs}
    # Air cannot move an environment to another repository: it is replaced, and its automations follow the new one.
    replaced = {e["name"] for e in plan["environments"] for x in envs
                if x["name"] == e["name"] and "repository" in x and same_repository(x["repository"], e["repository"]) is False}
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
            elif x is None and capped and kind == "automation":
                out.append(("unknown", kind, p["name"], p, x, []))
            elif x is None or changed:
                out.append(("create" if x is None else "update", kind, p["name"], p, x, changed))
        if not capped:
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


def same_repository(a: str | None, b: str | None) -> bool:
    """Whether two repository URLs name the same repository: Air lowercases the organization (`jetbrains/jbeval`),
    so names compare case-insensitively, and a trailing `.git` or `/` does not count."""
    norm = lambda u: (u or "").strip().rstrip("/").removesuffix(".git").lower()  # noqa: E731
    return norm(a) == norm(b)


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
               additional_names=(), project_id: str | None = None,
               env_backend: "EnvBackend | None" = None) -> tuple[list[dict], list[dict]]:
    """`project_id` (#1387): read the automations of that project, and only those, in place of the personal ones.
    With `env_backend` too, the environments are the project's, read through air-backend REST."""
    if project_id:
        listed = automations.call("list_automations", {})
        every = [x for v in ([listed] if isinstance(listed, list) else listed.values()) if isinstance(v, list) for x in v]
        automation_items = [x for x in every if x.get("projectId") == project_id]
        unfiltered = len(every)  # B2: the cap applies to what Air returned, not to what is left after the filter
    else:
        listed = automations.call("list_automations", {"privacy": "private"})
        automation_items = listed if isinstance(listed, list) else listed["personal"]
        unfiltered = len(automation_items)
    capped = unfiltered >= LIST_CAP
    if capped and backend:  # the REST API pages: the full list, so nothing is `unknown` (#1347)
        automation_items, capped = ([x for x in backend.list_automations(project_id)] if project_id
                                    else [x for x in backend.list_automations() if not x.get("projectId")]), False
    automation_items = Listed(automation_detail(automations.call("get_automation", {"automationId": x["automationId"]}))
                              for x in automation_items if x["name"].startswith(prefix))
    automation_items.capped = capped
    if backend:
        for item in automation_items:
            if item["name"] in additional_names:
                record = backend.get_automation(item["id"])
                item["additionalRepositories"] = record.get("additionalRepositories") or []
    if project_id and env_backend:
        return automation_items, env_backend.project_environments(project_id, prefix)
    env_items = list_envs(cloud, repository_url)
    env_items = [environment_detail(cloud.call("get_environment", {"id": x["id"]}))
                 for x in env_items if x["name"].startswith(prefix)]
    if project_id:  # B1: only an environment of the project can be diffed, updated or resolved by id
        env_items = [x for x in env_items if x.get("projectId") == project_id]
    return automation_items, env_items


def _glob(pattern: str) -> re.Pattern:
    """A name glob: `*` and `?` only, so the `[...]` of a prefix such as `[Offline eval]` stays literal."""
    return re.compile("".join(".*" if c == "*" else "." if c == "?" else re.escape(c) for c in pattern), re.DOTALL)


def selector_matcher(only: list[str], plan: dict):
    """`match(kind, name, planned item)` for `--only` selectors (`node:<node>`, `env:<environment>`, a name glob)."""
    prefix = plan["prefix"]
    short = lambda name: name[len(prefix):] if name.startswith(prefix) else name  # noqa: E731
    in_env = lambda name, env: short(name) == env or short(name).startswith(env + " ")  # noqa: E731
    env_names = {sel: {e["name"] for e in plan["environments"] if in_env(e["name"], sel[4:])}
                 for sel in only if sel.startswith("env:")}

    def one(sel: str, kind: str, name: str, p: dict | None) -> bool:
        if sel.startswith("node:"):
            node = ((p or {}).get("spec") or {}).get("node") or short(name).split(" ")[0]
            return kind == "automation" and node == sel[5:]
        if sel.startswith("env:"):
            if kind == "environment":
                return in_env(name, sel[4:])
            return bool(p) and p.get("environmentConfigId") in env_names[sel]
        glob = _glob(sel)
        return bool(glob.fullmatch(name) or glob.fullmatch(short(name)))

    return lambda kind, name, p=None: any(one(sel, kind, name, p) for sel in only)


def missing_project_environments(plan: dict, envs: list[dict]) -> list[str]:
    """#1387: the planned environments the project does not own. Air cannot create a project-owned environment
    (`create_environment` has no project field), so a person creates each one in the project. An environment counts as
    the project's when its record has `projectId` equal to the plan's."""
    owned = {x["name"] for x in envs if x.get("projectId") == plan.get("projectId")}
    return [e["name"] for e in plan["environments"] if plan.get("projectId") and e["name"] not in owned]


PROJECT_ERRORS = (("AIRC-1855", "the Air organization is personal and rejects projectId: set `targets.air.project_id` "
                                "only for a team organization"),
                  ("AIRA-619", "the project has no service account: create it in the project settings, then rerun"))


def project_error(message: str, project_id: str | None) -> str | None:
    """#1387: a clear reason for the two Air errors a project target can hit, else None."""
    return next((f"project {project_id}: {why} ({code})" for code, why in PROJECT_ERRORS
                 if project_id and code in message), None)


def apply_diff(changes: list[tuple], automations: Mcp, cloud: Mcp, repo_url="", *, backend=None,
               env_repositories=None, owned_env_ids=None, project_env_ids: dict | None = None) -> None:
    """Environments before automations on create, update and replace, after them on delete. A replaced environment
    is created under its new repository; the old one is renamed `retired <name>` first (Air names are unique) and left
    for a person to delete in the Air UI, since the Air MCP has no environment delete.
    `env_repositories` ({environment name: planned repository URL}) picks the right one of two same-named environments.
    `owned_env_ids` (#1387, a project target): only these environment ids can be picked for an automation.
    `project_env_ids` ({name: id} of the project's environments, read through air-backend): the ids automations use."""
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
            env_ids = env_ids or project_env_ids or {e["name"]: e["id"] for e in list_envs(cloud, repo_url)
                                  if e["id"] not in retired and (owned_env_ids is None or e["id"] in owned_env_ids)
                                  and (env_url(e) is None or e["name"] not in env_repositories
                                                                 or same_repository(env_url(e), env_repositories[e["name"]]))}
            p = {**p, "environmentConfigId": env_ids[p["environmentConfigId"]]}
        p = {k: v for k, v in (p or {}).items() if k not in ("spec", "additionalRepositories")}
        if action == "update":
            # triggers and triggerVcs pair by position: update_automation takes both or neither
            pair = {"triggers", "triggerVcs"} if kind == "automation" and {"triggers", "triggerVcs"} & set(changed) else set()
            p = {k: v for k, v in p.items() if k in changed or k in pair}
        if kind == "environment" and isinstance(p.get("variables"), dict):
            p["variables"] = [{"key": key, "value": value} for key, value in p["variables"].items()]
        if kind == "environment" and action == "update" and "domains" in p:  # edit_environment takes `allowedDomains`
            live = (x or {}).get("domains") or []
            p["allowedDomains"] = live + [d for d in p.pop("domains") or [] if d not in live]
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


def missing_variables(plan: dict, envs: list[dict]) -> list[tuple[dict, dict]]:
    """(planned environment, declared variable) for each declared variable that the environment in Air lacks, by name
    (secret ones too); every one when the environment does not exist yet. One that exists keeps its value."""
    have = {x["name"]: x for x in envs}
    out = []
    for e in plan["environments"]:
        live = have.get(e["name"]) or {}
        keys = set(live.get("variable_keys") or live.get("variables") or {})
        out += [(e, v) for v in e.get("declared") or [] if v["name"] not in keys]
    return out


UNSET = "unset"  # Air refuses an empty value; doctor reports this placeholder as missing


def create_missing_variables(plan: dict, envs: list[dict], cloud: Mcp, dry_run: bool = False) -> None:
    """List (`--dry-run`) or create each declared variable that its environment lacks, with the value `unset`
    (`isSecret` for a secret one). A variable that exists is never listed, so a value a person set stays."""
    ids = {x["name"]: x["id"] for x in envs}
    by_env = {}
    for e, v in missing_variables(plan, envs):
        by_env.setdefault(e["name"], []).append(v)
    for name, declared in by_env.items():
        for v in declared:
            print(f"create variable {v['name']} ({UNSET}{', secret' if v['secret'] else ''}) in {name[len(plan['prefix']):]}")
        if not dry_run and name in ids:
            cloud.call("edit_environment", {"id": ids[name], "variables": [
                {"key": v["name"], "value": UNSET, **({"isSecret": True} if v["secret"] else {}),
                 **({"description": v["description"]} if v.get("description") else {})} for v in declared]})


def extra_variables(plan: dict, envs: list[dict]) -> list[str]:
    """`remove by hand in Air: <env> variable <NAME>` for each variable in Air that the plan does not have."""
    have = {x["name"]: x.get("variables") or {} for x in envs}
    return [f"remove by hand in Air: {e['name']} variable {k}" for e in plan["environments"]
            for k in have.get(e["name"], {})
            if k not in (e.get("variables") or {}) and k not in {x["name"] for x in e.get("declared") or []}]


def repository_urls(plan: dict) -> list[str]:
    """The repositories whose environments the deploy lists: the planned environments' and, since an environment moved
    off a repository is found there and then replaced, the automations' repositories too."""
    return list(dict.fromkeys([e["repository"] for e in plan["environments"]] +
                              [a["repository"]["browseUrl"] for a in plan["automations"]
                               if isinstance(a.get("repository"), dict) and a["repository"].get("browseUrl")])) or [""]


def deploy(mode: str, allow_delete: bool, plan: dict, automations: Mcp, cloud: Mcp, *, backend=None, run=None, yt_run=None,
           config=None, only: list[str] | None = None, env_backend: EnvBackend | None = None) -> int:
    """Write only to Air, to repository labels (through `run`, a `gh` runner) and to the tracker's own setup (through
    `yt_run` for YouTrack): deploy never creates, edits or closes a tracker ticket. `config` selects the plugins that
    provision the labels (github when None) and the tracker setup (every plugin's, for what the plan names, when None).
    `only` (`--only` selectors) limits the diff, the verify and the variables to the matched items and never removes a
    label or tracker tag; a selector that matches nothing in the plan or in Air returns 2."""
    repo_urls = repository_urls(plan)
    env_repositories = {e["name"]: e["repository"] for e in plan["environments"]}
    additional_names = {a["name"] for a in plan["automations"] if "additionalRepositories" in a}
    backend = backend or (Backend() if additional_names else None)  # main passes one: a capped list pages through it
    project_id = plan.get("projectId")
    env_backend = env_backend if project_id else None
    state = read_state(automations, cloud, plan["prefix"], repo_urls, backend=backend,
                       additional_names=additional_names, project_id=project_id, env_backend=env_backend)
    capped = getattr(state[0], "capped", False)
    changes = diff(plan, *state, capped)
    missing_envs = missing_project_environments(plan, state[1])
    sources = {}
    if env_backend and missing_envs:  # copy each from its personal namesake, or create it new, in the project
        personal = [environment_detail(cloud.call("get_environment", {"id": x["id"]}))
                    for x in list_envs(cloud, repo_urls) if x["name"] in missing_envs]
        sources, missing_envs = project_environment_sources(plan, missing_envs, personal), []
    if project_id:  # an environment is never created for a project: a person does it
        changes = [c for c in changes if not (c[1] == "environment" and c[0] in ("create", "replace"))]
    match, others = None, 0
    if only:
        match = selector_matcher(only, plan)
        known = [(k, x["name"], p) for k, items, planned in (("environment", state[1], plan["environments"]),
                                                              ("automation", state[0], plan["automations"]))
                 for x, p in [(x, None) for x in items if x["name"].startswith(plan["prefix"])] + [(p, p) for p in planned]]
        for sel in only:
            if not any(selector_matcher([sel], plan)(k, n, p) for k, n, p in known):
                print(f"error: --only {sel} matches nothing in the plan or in Air", file=sys.stderr)
                return 2
        kept = [c for c in changes if match(c[1], c[2], c[3])]
        others, changes = len(changes) - len(kept), kept
        print(f"partial: {', '.join(only)}")
        plan = {**plan, "environments": [e for e in plan["environments"] if match("environment", e["name"], e)]}
        sources = {n: s for n, s in sources.items() if n in {e["name"] for e in plan["environments"]}}
    print(f"target: project {project_id}" if project_id else "target: personal")
    for action, kind, name, _, _, changed in changes:
        if action == "unknown":
            print(f"unknown {kind} {name} (list capped at {LIST_CAP})")
        else:
            print(f"{action} {kind} {name}" + (f" ({', '.join(changed)})" if changed else ""))
    if not changes and not sources:
        print("unchanged")
    for name in missing_envs:
        print(f"missing project environment {name}: create it in project {project_id} by hand")
    for name, source in sources.items():
        print(f"create project environment {name}" + (f" (copy of personal {source['id']})" if source else " (new)"))
    if only and others and mode == "--dry-run":
        print(f"not deployed: {others} other changes")
    if capped and not any(c[0] == "unknown" for c in changes):
        print(f"warning: Air listed {LIST_CAP} personal automations (its cap): none is deleted from an incomplete list",
              file=sys.stderr)
    unknown = [c[2] for c in changes if c[0] == "unknown"]
    if unknown and mode == "--apply":
        print(f"refused: Air lists at most {LIST_CAP} personal automations and listed {LIST_CAP}, so these planned "
              f"automations could not be told from missing ones: {', '.join(unknown)}. Delete or move other personal "
              f"automations in Air to get the list under {LIST_CAP}, then rerun.", file=sys.stderr)
        return 1
    for line in missing_secrets(plan, state[1]):
        print(line)
    for line in extra_variables(plan, state[1]):
        print(line)
    if missing_envs and mode == "--apply":  # nothing is written: every automation would 404 on its environment
        print(f"refused: project {project_id} owns no environment named {', '.join(missing_envs)}. Create "
              f"{'it' if len(missing_envs) == 1 else 'each'} in the project (the Air MCP and REST API cannot), "
              f"then rerun.", file=sys.stderr)
        return 1
    if mode == "--dry-run":
        create_missing_variables(plan, state[1], cloud, dry_run=True)
    if sources and mode == "--apply":  # before any automation: each one links to its environment by id
        create_project_environments(plan, sources, env_backend)
        state = read_state(automations, cloud, plan["prefix"], repo_urls, backend=backend,
                           additional_names=additional_names, project_id=project_id, env_backend=env_backend)
        if still := missing_project_environments(plan, state[1]):
            print(f"error: project {project_id} still lacks {', '.join(still)} after creating it", file=sys.stderr)
            return 1
        changes = [c for c in diff(plan, *state, getattr(state[0], "capped", False))
                   if not (c[1] == "environment" and c[0] in ("create", "replace")) and (match is None or match(c[1], c[2], c[3]))]
    label_changes = core.reconcile_labels(plan, run, config)
    if only:  # a partial deploy never removes
        label_changes = [c for c in label_changes if c[0] != "remove"]
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
        for line in [x for p in trackers for x in p.dry_run() if not (only and x.startswith("remove"))]:
            print(line)
        return 0
    for line in [x for p in trackers for x in p.write()]:  # a missing tag fails every handoff that adds it
        print(line)
    core.provision_labels(plan, label_changes, "write", run, config)  # a missing label fails every handoff that adds it
    removals = [c for c in label_changes if c[0] == "remove"]
    refusals = [] if only else [x for p in trackers for x in p.refusals()]
    if (removals or refusals or any(c[0] == "delete" for c in changes)) and not allow_delete:
        for _, repo, label, _ in removals:
            print(f"refused: would remove label {label} in {repo}", file=sys.stderr)
        for line in refusals:
            print(line, file=sys.stderr)
        print("refused: the diff deletes items; rerun with --allow-delete", file=sys.stderr)
        return 1
    core.provision_labels(plan, removals, "remove", run, config)
    for p in [] if only else trackers:
        for line in p.remove():
            print(line)
    if changes:
        try:
            apply_diff(changes, automations, cloud, repo_urls, backend=backend, env_repositories=env_repositories,
                       owned_env_ids={x["id"] for x in state[1]} if project_id else None,
                       project_env_ids={x["name"]: x["id"] for x in state[1]} if env_backend else None)
        except RuntimeError as e:
            if reason := project_error(str(e), project_id):
                print(f"error: {reason}", file=sys.stderr)
                return 1
            raise
        after = read_state(automations, cloud, plan["prefix"], repo_urls, backend=backend,
                           additional_names=additional_names, project_id=project_id, env_backend=env_backend)
        state = after
        left = [c for c in diff(plan, *after, getattr(after[0], "capped", False))
                if not (c[1] == "environment" and (c[0] == "delete" or (project_id and c[0] in ("create", "replace")))) and (match is None or match(c[1], c[2], c[3]))]
        for action, kind, name, *_ in left:
            print(f"verify failed: {action} {kind} {name} still differs", file=sys.stderr)
        if left:
            return 1
    create_missing_variables(plan, state[1], cloud)  # after the environments exist: their ids are in `state`
    for x in (x for x in state[0] if match is None or match("automation", x["name"])):  # a person registers each webhook with its sender; also on an unchanged plan
        for w in x["webhooks"]:
            print(f"webhook {x['name']}: trigger {w['id']}" + (f" {w['url']}" if w["url"] else ""))
    return 0


def main(argv: list[str]) -> int:
    argv = config_mod.pop_team(argv)
    if not definition.present(config_mod.team_root()):
        print("deploy: the team root has no factory.yaml and workflow.yaml (ADR-0021)", file=sys.stderr)
        return 2
    rest, renames, only = argv[1:], {}, []
    while "--only" in rest:
        i = rest.index("--only")
        sel = rest[i + 1] if i + 1 < len(rest) else ""
        if not sel or sel in ("node:", "env:") or sel.startswith("--"):
            print("--only needs a selector: node:<node>, env:<environment> or a name glob", file=sys.stderr)
            return 2
        only.append(sel)
        del rest[i:i + 2]
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
            code = deploy(argv[0], "--allow-delete" in argv, plan, *servers, backend=Backend(), run=run, config=config,
                          only=only, env_backend=EnvBackend() if plan.get("projectId") else None)
            if code:
                return code
            return core.sync_wake_files(config, run, argv[0] == "--apply")
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

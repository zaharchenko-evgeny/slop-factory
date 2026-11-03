#!/usr/bin/env python3
"""Air service contracts: the recordings in factory/tests/contracts/ and the tools built on them.

`factory contracts refresh` (operator-only: it needs the operator's Air OAuth, never run it from an agent or CI)
re-records the tool lists and the read-only responses of the two Air MCP servers into air-mcp.json, started from
FACTORY_MCP_AIR_AUTOMATIONS and FACTORY_MCP_AIR_CLOUD as `factory deploy` does. Tokens, emails and secret-like ids are
stripped. Responses of write tools stay as recorded.
`contracts.py serve RECORDING SERVER` is a fake MCP server over stdio answering only what was recorded.
"""
import json
import os
import re
import sys
from pathlib import Path

DIR = Path(__file__).resolve().parent / "tests/contracts"
SECRET = re.compile(r"gh[pousr]_[A-Za-z0-9]{20,}|github_pat_\w+|sk-[A-Za-z0-9-]{20,}|eyJ[\w-]{20,}\.[\w-]{10,}"
                    r"|Bearer\s+[\w.~+/=-]{8,}")
EMAIL = re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+")
SECRET_KEY = re.compile(r"token|secret|password|apikey|api_key|credential", re.I)


def redact(x):
    """The value with tokens and emails replaced, and values under secret-named keys removed."""
    if isinstance(x, str):
        if x.lstrip()[:1] in "{[":  # MCP content[].text holds JSON
            try:
                return json.dumps(redact(json.loads(x)))
            except ValueError:
                pass
        return EMAIL.sub("<email>", SECRET.sub("<token>", x))
    if isinstance(x, list):
        return [redact(v) for v in x]
    if isinstance(x, dict):
        return {k: "<redacted>" if SECRET_KEY.search(k) and isinstance(v, str) else redact(v) for k, v in x.items()}
    return x


def check_value(name, value, schema):
    """Raise ValueError when `value` breaks its recorded JSON schema."""
    if "anyOf" in schema:
        for choice in schema["anyOf"]:
            try:
                check_value(name, value, choice)
                break
            except ValueError:
                pass
        else:
            raise ValueError(f"{name}: invalid value {value!r} for schema {json.dumps(schema)[:120]}")
    types = {"string": str, "integer": int, "number": (int, float), "boolean": bool, "array": list, "object": dict, "null": type(None)}
    wants = [w for w in ([schema["type"]] if isinstance(schema.get("type"), str) else schema.get("type", [])) if w in types]
    ok = not wants or any(isinstance(value, types[w]) and not (w not in ("boolean", "null") and isinstance(value, bool)) for w in wants)
    if ok and "enum" in schema:
        ok = value in schema["enum"]
    if ok and "const" in schema:
        ok = value == schema["const"]
    if ok and isinstance(value, str):
        ok = len(value) >= schema.get("minLength", 0) and re.search(schema.get("pattern", ""), value) is not None
    if ok and isinstance(value, (int, float)) and not isinstance(value, bool):
        ok = schema.get("minimum", value) <= value <= schema.get("maximum", value)
    if ok and isinstance(value, list) and len(value) < schema.get("minItems", 0):
        ok = False
    if ok and isinstance(value, dict):
        if missing := [key for key in schema.get("required", []) if key not in value]:
            raise ValueError(f"{name}: missing {missing}")
        for key, child in schema.get("properties", {}).items():
            if key in value:
                check_value(f"{name}.{key}", value[key], child)
    if ok and isinstance(value, list) and "items" in schema:
        for i, item in enumerate(value):
            check_value(f"{name}[{i}]", item, schema["items"])
    if not ok:
        raise ValueError(f"{name}: invalid value {value!r} for schema {json.dumps(schema)[:120]}")


def find(responses, tool, args):
    """The recorded response of `tool`: one key per tool, or `tool:<variant>` keys told apart by the arguments."""
    keys = [k for k in responses if k == tool or k.startswith(tool + ":")]
    if len(keys) > 1:
        keys = [k for k in keys if all(str(v) in json.dumps(responses[k]) for v in args.values())]
    return responses[keys[0]] if keys else None


def reply(server, tools, responses, method, params):
    """The JSON-RPC result of one request; raises ValueError for a tool or argument the recordings do not allow."""
    if method != "tools/call":
        return {"protocolVersion": "2024-11-05", "capabilities": {}, "serverInfo": {"name": "fake", "version": "0"}}
    name, args = params["name"], params.get("arguments") or {}
    schema = next((t["inputSchema"] for t in tools if t["name"] == name), None)
    if schema is None:
        raise ValueError(f"unrecorded {server} tool: {name}")
    props = schema.get("properties", {})
    if missing := [k for k in schema.get("required", []) if k not in args]:
        raise ValueError(f"{name}: missing {missing}")
    if unknown := [k for k in args if k not in props]:
        raise ValueError(f"{name}: unknown arguments {unknown}")
    for k, v in args.items():
        check_value(f"{name}.{k}", v, props[k])
    res = find(responses, name, args)
    if res is None:
        raise ValueError(f"no recorded response for {server} tool: {name}")
    return res


def serve(recording, server):
    rec = json.loads(Path(recording).read_text())["servers"][server]
    for line in sys.stdin:
        m = json.loads(line)
        if "id" not in m:
            continue
        try:
            out = {"result": reply(server, rec["tools"], rec["responses"], m["method"], m.get("params", {}))}
        except ValueError as e:
            out = {"error": {"code": -32602, "message": str(e)}}
        print(json.dumps({"jsonrpc": "2.0", "id": m["id"], **out}), flush=True)


def refresh(path=DIR / "air-mcp.json"):
    import deploy
    rec = json.loads(Path(path).read_text())
    clients = {"air-automations": deploy.Mcp(os.environ["FACTORY_MCP_AIR_AUTOMATIONS"]),
               "air-cloud": deploy.Mcp(os.environ["FACTORY_MCP_AIR_CLOUD"])}
    try:
        for name, client in clients.items():
            rec["servers"][name]["tools"] = redact(client.rpc("tools/list", {})["tools"])
        raw = lambda c, tool, args: c.rpc("tools/call", {"name": tool, "arguments": args})  # noqa: E731
        auto, cloud = clients["air-automations"], clients["air-cloud"]
        auto_r, cloud_r = rec["servers"]["air-automations"]["responses"], rec["servers"]["air-cloud"]["responses"]
        auto_r["list_automations"] = redact(raw(auto, "list_automations", {"privacy": "private"}))
        repo = "https://github.com/" + os.environ.get("FACTORY_REPOSITORY", "zaharchenko-evgeny/slop-factory")
        cloud_r["list_environments"] = redact(raw(cloud, "list_environments", {"repositoryUrl": repo}))
        auto_id = json.loads(auto_r["get_automation"]["content"][0]["text"])["automationId"]
        auto_r["get_automation"] = redact(raw(auto, "get_automation", {"automationId": auto_id}))
        auto_r["list_runs"] = redact(raw(auto, "list_runs", {"automationId": auto_id}))
        runs = json.loads(auto_r["list_runs"]["content"][0]["text"])
        if runs:
            auto_r["get_run"] = redact(raw(auto, "get_run", {"runId": runs[0]["runId"]}))
        for item in json.loads(cloud_r["list_environments"]["content"][0]["text"])["items"]:
            key = "get_environment:" + item["name"].rsplit(" ", 1)[-1]
            if key in cloud_r:
                cloud_r[key] = redact(raw(cloud, "get_environment", {"id": item["id"]}))
    finally:
        for c in clients.values():
            c.close()
    Path(path).write_text(json.dumps(rec, indent=1) + "\n")
    return 0


def main(argv):
    if argv[:1] == ["refresh"]:
        return refresh()
    if argv[:1] == ["serve"] and len(argv) == 3:
        return serve(*argv[1:]) or 0
    print(__doc__)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))

"""The YouTrack checks of `factory doctor` (hook `doctor.probes`): read-only probes of the token variable, the project,
and every tag and field value the definition's stores name. A missing one is reported, never created."""
import os
import urllib.parse

from doctor import env_needs_tiers
from factory_plugins.youtrack.store import TOP, urllib_runner


def probes(settings, env, run, *, nodes=None, environ=os.environ, **_):
    """Hook `doctor.probes`: the YouTrack probes of a `tracker.adapter: youtrack` team, `<tier>` expanded over the
    tiers of `nodes`."""
    if (settings.get("tracker") or {}).get("adapter") != "youtrack":
        return []
    return youtrack_probes(settings, env_needs_tiers(nodes or {"nodes": {}}), environ)


def wanted(settings, tiers):
    """(tags, {field: values}) the descriptor's state and command stores name, `<tier>` expanded."""
    tags, fields = set(), {}
    stores = [x["store"] for x in [*(settings.get("states") or {}).values(), *(settings.get("commands") or {}).values()]]
    for store in stores:
        if isinstance(store.get("tag"), str):
            tags |= {store["tag"].replace("<tier>", t) for t in tiers} if "<tier>" in store["tag"] else {store["tag"]}
        if isinstance(store.get("field"), dict):
            fields.setdefault(store["field"]["name"], set()).add(store["field"]["value"])
    t = settings.get("tracker") or {}
    if t.get("state_mirror"):  # #1260: every State value a stage is mirrored to
        fields.setdefault(t.get("state_mirror_field", "State"), set()).update(v for v in t["state_mirror"].values() if v)
    return sorted(tags), {k: sorted(v) for k, v in fields.items()}


def youtrack_probes(settings, tiers, environ=os.environ, yt_run=None):
    """Read-only probes of a YouTrack tracker: the token variable, the project, and every mapped tag and field value.
    A missing one is reported, never created."""
    t = settings["tracker"]
    token_env = t.get("token_env", "YOUTRACK_TOKEN")
    results = [{"probe": f"youtrack:token:{token_env}", "result": "ok" if environ.get(token_env) else "missing", "detail": ""}]
    if not environ.get(token_env) and yt_run is None:
        return results
    yt_run = yt_run or urllib_runner(t["url"], environ[token_env])
    q = urllib.parse.quote
    try:
        projects = yt_run("GET", f"/api/admin/projects?fields=id,shortName&query={q(t['project'])}&{TOP}")
        project = next((p for p in projects if p["shortName"] == t["project"]), None)
        results.append({"probe": f"youtrack:project:{t['project']}", "result": "ok" if project else "missing", "detail": ""})
        tags, fields = wanted(settings, tiers)
        have, skip = set(), 0
        while True:  # a large instance has many more tags than one page (as list_tags, #1307)
            page = yt_run("GET", f"/api/tags?fields=name&{TOP}&$skip={skip}") or []
            have |= {x["name"] for x in page}
            if len(page) < 1000:
                break
            skip += len(page)
        results += [{"probe": f"youtrack:tag:{x}", "result": "ok" if x in have else "missing", "detail": ""} for x in tags]
        values = {}
        if project and fields:
            for c in yt_run("GET", f"/api/admin/projects/{project['id']}/customFields?fields=field(name),bundle(values(name))&{TOP}"):
                values[c["field"]["name"]] = {v["name"] for v in (c.get("bundle") or {}).get("values") or []}
        for name, want in fields.items():
            for v in want:
                results.append({"probe": f"youtrack:field:{name}={v}", "result": "ok" if v in values.get(name, ()) else "missing",
                                "detail": "" if name in values else "field not in project"})
    except Exception as e:  # an unreadable YouTrack is a denied probe
        results.append({"probe": "youtrack:read", "result": "denied", "detail": str(e)[:200]})
    return results

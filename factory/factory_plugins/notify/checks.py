"""`factory validate` of notify (the `definition.errors` hook): the `notify:` config and every `notify` effect."""
import plugins
from factory_plugins.notify import notifiers


def effect_errors(where, outcome, spec, config):
    """A `notify` effect is `message` or `{message: message, people: [logins]}`, and needs `notify` config."""
    errs = []
    if not config:
        errs.append(f"missing-notify: {where}.{outcome}: a `notify` effect needs `notify` config in factory.yaml")
    body = spec if isinstance(spec, dict) else {"message": spec}
    people = body.get("people")
    if body.get("message") != "message" or set(body) - {"message", "people"} \
            or not (people is None or isinstance(people, list) and all(isinstance(p, str) for p in people)):
        errs.append(f"bad-effect: {where}.{outcome}: notify must be `message` or `{{message: message, people: [logins]}}`")
    elif config:
        known = (config or {}).get("people") or {}
        errs += [f"unknown-person: {where}.{outcome}: {p} is not in notify.people" for p in people or [] if p not in known]
    return errs


def errors(defn):
    f = defn["factory"]
    config = f.get("notify")
    errs = notifiers.config_errors(plugins.registry(f), config) if isinstance(config, dict) else []
    for name, node in (defn["workflow"].get("nodes") or {}).items():
        for outcome, oc in (node.get("outcomes") or {}).items():
            errs += [e for effect in (oc or {}).get("effects") or [] if isinstance(effect, dict) and "notify" in effect
                     for e in effect_errors(f"nodes.{name}", outcome, effect["notify"], config)]
    return errs

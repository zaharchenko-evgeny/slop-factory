"""Notifiers (ADR-0022): the gate sends a message through a channel and gets back a `ref` naming the thread.

A channel is a `Notifier` (`ports.notifier`) that a turned-on plugin provides as `notifier` `<name>` (notify_email:
`email`). `person` is the entry of `notify.people` plus its `login`; `message` is `{issue, pr, problem, links, options,
recommended}`. Secrets are read from the env var a `notify.adapters.<name>` entry names, never from the file.
"""
import plugins

MARKER = "<!-- factory:notify via={via} person={person} ref={ref} -->"


def one_line(text):
    return " ".join(str(text).split())


def validate_message(message):
    """The message fields the agent returned, checked; raises ValueError naming the problem."""
    if not one_line(message.get("problem") or ""):
        raise ValueError("`problem` must be a non-empty string")
    links, options, rec = message.get("links"), message.get("options"), message.get("recommended")
    if not isinstance(links, list) or not all(isinstance(x, dict) and x.get("title") and x.get("url") for x in links):
        raise ValueError("`links` must be a list of {title, url}")
    if not isinstance(options, list) or not all(isinstance(x, str) and x.strip() for x in options):
        raise ValueError("`options` must be a list of texts")
    if rec is not None and not (isinstance(rec, int) and not isinstance(rec, bool) and 0 <= rec < len(options)):
        raise ValueError("`recommended` must be the index of an entry in `options`")


def render(message):
    """Plain text of the message: the problem, the links, then the numbered options (the suggested one marked)."""
    lines = [str(message["problem"]).strip()]
    if message.get("links"):
        lines += ["", *[f"- [{one_line(x['title'])}]({x['url']})" for x in message["links"]]]
    if message.get("options"):
        rec = message.get("recommended")
        lines += ["", "Options:", *[f"{i + 1}. {one_line(o)}{' (suggested)' if i == rec else ''}"
                                    for i, o in enumerate(message["options"])]]
    return "\n".join(lines)


def marker(via, person, ref):
    return MARKER.format(via=via, person=person, ref=ref)


def team_config():
    """`notify` of the team's factory.yaml; ValueError when the team has none."""
    import config as config_mod
    import definition
    config = (definition.load(config_mod.team_root())["factory"] or {}).get("notify")
    if not config:
        raise ValueError("notify: the team's factory.yaml has no `notify` config")
    return config


def send_all(registry, config, message, people=None):
    """Send `message` to each person of `config` (or only `people`; an empty list means nobody) on each notifier in
    their `via`, as `registry` provides it. Returns (sent, errors): `sent` is [(via, login, ref)], `errors` is
    ["<login> via <notifier>: <error>"]."""
    known = config.get("people") or {}
    unknown = [p for p in people or [] if p not in known]
    if unknown:
        raise ValueError(f"notify: not in notify.people: {', '.join(unknown)}")
    sent, errors = [], []
    for login in list(known) if people is None else people:
        person = {**(known[login] or {}), "login": login}
        for via in person.get("via") or []:
            try:
                notifier = registry.impl("notifier", via)
                if notifier is None:
                    raise LookupError(f"no notifier `{via}` is turned on")
                sent.append((via, login, notifier(config["adapters"][via]).send(person, message)))
            except Exception as e:  # one failing channel must not hide the sends that went out
                errors.append(f"{login} via {via}: {type(e).__name__}: {e}")
    return sent, errors


def provider(name):
    """The known plugin, on or off, that provides notifier `name`; None when none does."""
    return next((p for p in plugins.KNOWN if name in (plugins.declaration(p).provides.get("notifier") or {})), None)


def config_errors(registry, config):
    """`notify` config problems, as `code: text` lines: an unknown notifier or one whose plugin is off, and what the
    notifier's own `validate` finds (notify_email: a missing address)."""
    errs = []
    adapters = config.get("adapters") or {}
    on = registry.impls("notifier")

    def missing(where, name):
        if name in on:
            return None
        owner = provider(name)
        return plugins.refusal(where, name, owner) if owner else \
            f"unknown-adapter: {where}: {name} (known notifiers are {', '.join(on) or 'none: turn on a notifier plugin'})"
    errs += [e for name in adapters if (e := missing(f"notify.adapters.{name}", name))]
    for login, person in (config.get("people") or {}).items():
        person = {**(person or {}), "login": login}
        for via in person.get("via") or []:
            if via not in on or via not in adapters:
                errs.append(missing(f"notify.people.{login}.via", via) or f"unknown-adapter: notify.people.{login}.via: "
                            f"{via} (not configured in notify.adapters)")
            else:
                errs += registry.impl("notifier", via).validate(adapters[via] or {}, person)
    return errs

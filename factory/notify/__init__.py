"""Notifier adapters (ADR-0022): the gate sends a message through a channel and gets back a `ref` naming the thread.

An adapter class has `send(person, message) -> ref` and `ack(person, ref, text)`. `person` is the entry of
`notify.people` plus its `login`; `message` is `{issue, pr, problem, links, options, recommended}` plus the `url` of the issue or PR and the `run_url`
the gate adds. An adapter without `ack` (slack-workflow) cannot take replies. Secrets are read
from the env var a `notify.adapters.<name>` entry names, never from the file.
"""
from . import slack_workflow, smtp

ADAPTERS = {"email": smtp.EmailAdapter, "slack-workflow": slack_workflow.SlackWorkflowAdapter}
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


def send_all(config, message, people=None):
    """Send `message` to each person of `config` (or only `people`; an empty list means nobody) on each adapter in their `via`.
    Returns (sent, errors): `sent` is [(via, login, ref)], `errors` is ["<login> via <adapter>: <error>"]."""
    known = config.get("people") or {}
    unknown = [p for p in people or [] if p not in known]
    if unknown:
        raise ValueError(f"notify: not in notify.people: {', '.join(unknown)}")
    sent, errors = [], []
    for login in list(known) if people is None else people:
        person = {**known[login], "login": login}
        for via in person.get("via") or []:
            try:
                adapter = ADAPTERS[via](config["adapters"][via])
                sent.append((via, login, adapter.send(person, message)))
            except Exception as e:  # one failing channel must not hide the sends that went out
                errors.append(f"{login} via {via}: {type(e).__name__}: {e}")
    return sent, errors


def ack_all(config, login, via, ref, text):
    """`notify: ack`: tell `login` in the thread `ref` on adapter `via`. An adapter without `ack` is skipped; returns whether it sent."""
    adapter = ADAPTERS[via](config["adapters"][via])
    if not hasattr(adapter, "ack"):
        return False
    adapter.ack({**(config.get("people") or {})[login], "login": login}, ref, text)
    return True


def config_errors(config):
    """`notify` config problems, as `code: text` lines: an unknown adapter, a missing address."""
    errs = []
    adapters = config.get("adapters") or {}
    for name in adapters:
        if name not in ADAPTERS:
            errs.append(f"unknown-adapter: notify.adapters.{name}: known adapters are {', '.join(ADAPTERS)}")
    for login, person in (config.get("people") or {}).items():
        person = person or {}
        for via in person.get("via") or []:
            if via not in ADAPTERS or via not in adapters:
                errs.append(f"unknown-adapter: notify.people.{login}.via: {via} "
                            f"({'not configured in notify.adapters' if via in ADAPTERS else 'no such adapter'})")
            elif not person.get(via):
                errs.append(f"missing-address: notify.people.{login}: no `{via}` address for the adapter in `via`")
    return errs


__all__ = ["ADAPTERS", "ack_all", "config_errors", "marker", "render", "send_all", "validate_message"]

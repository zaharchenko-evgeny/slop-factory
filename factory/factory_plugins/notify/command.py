"""`factory notify test <login>`: send that person a test message through each notifier in their `via`."""
import sys

import config
import plugins
from factory_plugins.notify import notifiers

USAGE = "usage: factory notify test <login>   (a test message to a person of notify.people, on each of their `via`)"


def main(argv):
    if len(argv) != 2 or argv[0] != "test":
        print(USAGE, file=sys.stderr)
        return 2
    login = argv[1]
    message = {"issue": "test", "pr": "", "problem": f"Test message from the factory to {login}: notify works.",
               "links": [], "options": [], "recommended": None}
    try:
        settings = notifiers.team_config()
        if not ((settings.get("people") or {}).get(login) or {}).get("via"):
            raise ValueError(f"notify: {login} has no `via` in notify.people" if login in (settings.get("people") or {})
                             else f"notify: not in notify.people: {login}")
        sent, errors = notifiers.send_all(plugins.registry(config.load()), settings, message, [login])
    except ValueError as e:
        print(f"factory: {e}", file=sys.stderr)
        return 2
    for via, _, ref in sent:
        print(f"factory: sent to {login} via {via} (ref {ref})")
    for error in errors:
        print(f"factory: failed: {error}", file=sys.stderr)
    return 1 if errors else 0

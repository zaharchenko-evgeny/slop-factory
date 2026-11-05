"""Print the message as one JSON line (tests and local runs)."""
import json

from . import register


@register("log")
class Log:
    def __init__(self, settings, run):
        self.sent = set()

    def seen(self, key):
        return key in self.sent

    def send(self, message):
        if self.seen(message["key"]):
            return False
        self.sent.add(message["key"])
        print(json.dumps(message), flush=True)
        return True

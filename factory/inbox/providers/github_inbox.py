"""One open issue labelled `factory:inbox`; each message is one comment carrying a hidden dedupe marker."""
import json

import vocab

from . import register

TITLE = "Factory inbox"


@register("github-inbox")
class GitHubInbox:
    def __init__(self, settings, run):
        self.repo, self.user, self.run = settings["tracker"]["repository"], settings["notify"]["github_user"], run
        self.number = None

    def _issue(self, create):
        if self.number is None:
            found = json.loads(self.run(["issue", "list", "-R", self.repo, "--label", vocab.inbox(), "--state", "open",
                                         "--json", "number", "--limit", "1"]))
            if found:
                self.number = found[0]["number"]
            elif create:
                url = self.run(["issue", "create", "-R", self.repo, "--title", TITLE, "--label", vocab.inbox(),
                                "--body", "[factory] Notifications for the factory owner, one comment each."]).strip()
                self.number = int(url.rsplit("/", 1)[-1])
        return self.number

    def seen(self, key):
        number = self._issue(create=False)
        if number is None:
            return False
        pages = json.loads(self.run(["api", "--paginate", "--slurp", f"repos/{self.repo}/issues/{number}/comments"]))
        return any(f"<!-- factory-inbox:{key} -->" in c["body"] for page in pages for c in page)

    def send(self, message):
        if self.seen(message["key"]):
            return False
        lines = [f"[factory] {message['kind']}: {message.get('title') or 'no ticket'}",
                 f"Ticket: {message.get('url') or 'none'}", f"PR: {message.get('pr_url') or 'none'}",
                 f"Result: {message.get('result') or 'none'}"]
        if message.get("summary"):
            lines.append(message["summary"])
        lines += [f"@{self.user}", f"<!-- factory-inbox:{message['key']} -->",
                  f"Factory-Run: {message.get('run') or 'local'}"]
        self.run(["issue", "comment", str(self._issue(create=True)), "-R", self.repo, "--body", "\n\n".join(lines)])
        return True

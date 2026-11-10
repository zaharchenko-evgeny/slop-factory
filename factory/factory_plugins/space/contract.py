"""The space plugin's static contract (ADR-0027, `ports/place.py`): JetBrains Space as a repository's vcs. Its merge
request events arrive by webhook (the `gate.trigger` hook), not from the automation's main repository."""


class Space:
    """`vcs: {adapter: space, url, project}`."""

    @staticmethod
    def home(block, repo):
        return None

    @staticmethod
    def check(block, repo, where):
        b = block or {}
        return [f"space-vcs: {where}: a space vcs names `{k}`" for k in ("url", "project") if not b.get(k)]


VCS = Space()

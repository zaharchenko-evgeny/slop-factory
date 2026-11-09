"""The github plugin's static contracts (ADR-0027, `ports/place.py`): GitHub Issues as a repository's tracker and
GitHub as its vcs."""


def _full(name):
    return "/".join(str(name).removesuffix(".git").rstrip("/").split("/")[-2:])


class Issues:
    """`tracker: {adapter: github-issues[, repository: owner/repo][, scope: {label}]}`: the issues of `repository`,
    by default the repository's own."""
    LOCAL_KEYS = ("repository", "scope")

    @staticmethod
    def place(block, repo):
        scope = (block or {}).get("scope") or {}
        return f"github-issues:{Issues.home(block, repo)}" + (f"|label={scope['label']}" if scope.get("label") else "")

    @staticmethod
    def home(block, repo):
        return _full((block or {}).get("repository") or repo).lower()

    @staticmethod
    def event(trigger):
        return trigger

    @staticmethod
    def filters(block):
        return {}  # the copy's main repository is the issues' repository: Air delivers only its events

    @staticmethod
    def check(block, where):
        scope = (block or {}).get("scope")
        if scope is not None and (not isinstance(scope, dict) or set(scope) != {"label"}):
            return [f"bad-scope: {where}: a github-issues scope is {{label: <label>}}"]
        return []


class GitHub:
    """`vcs: {adapter: github}`: the repository's pull requests, heard from its own events."""

    @staticmethod
    def home(block, repo):
        return _full(repo).lower()

    @staticmethod
    def check(block, repo, where):
        return []


ISSUES, VCS = Issues(), GitHub()

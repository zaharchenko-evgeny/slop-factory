"""The youtrack plugin's static contract (ADR-0027, `ports/place.py`): a YouTrack project, narrowed by a custom field
value, as a repository's tracker. Its events reach any automation; the trigger filters on the project and scope."""


class YouTrack:
    """`tracker: {adapter: youtrack, project: KEY[, url][, scope: {field, value[, type]}]}`."""
    LOCAL_KEYS = ("scope",)

    @staticmethod
    def place(block, repo):
        b = block or {}
        scope = b.get("scope") or {}
        where = f"youtrack:{str(b.get('url') or '').rstrip('/')}/{b.get('project') or ''}"
        return where + (f"|{scope.get('field')}={scope.get('value')}" if scope else "")

    @staticmethod
    def home(block, repo):
        return None

    @staticmethod
    def event(trigger):
        """A ticket label wake is a tag on YouTrack; a PR, CI or schedule event stays the vcs's."""
        event, _, arg = str(trigger).partition(" ")
        return f"youtrack.tagAdded {arg}" if event == "issues.labeled" else trigger

    @staticmethod
    def filters(block):
        """Air's project selector (`projectShortName`, case-sensitive: AIRA-790) and the scope field's value."""
        b = block or {}
        scope = b.get("scope") or {}
        out = {"projectShortName": b["project"]} if b.get("project") else {}
        return {"youtrack": {**out, **({scope["field"]: scope["value"]} if scope.get("field") else {})}} if out or scope else {}

    @staticmethod
    def check(block, where):
        b, errs = block or {}, []
        if not b.get("project"):
            errs.append(f"youtrack-project: {where}: a youtrack tracker names its `project`")
        scope = b.get("scope")
        if scope is not None and (not isinstance(scope, dict) or not {"field", "value"} <= set(scope)
                                  or set(scope) - {"field", "value", "type"}):
            errs.append(f"bad-scope: {where}: a youtrack scope is {{field, value[, type]}}")
        return errs


TRACKER = YouTrack()

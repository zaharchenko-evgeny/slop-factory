"""View registration: a view is a pure projection of the normalized snapshot.

Register another view without touching the built-in views or any tracker adapter:

    from ui import views
    registry = views.builtin_registry()
    registry.register(views.ViewSpec(id="by-repo", title="By repository", project=lambda snap: {...}))
    payload = registry.project("by-repo", snapshot)

A `ViewSpec` has an `id` (unique, a plain slug), a `title`, an optional `description`, and `project(snapshot)`, which
may read only the snapshot (`snapshot.build`'s dict) and returns JSON-ready data. A view never reads a tracker, so an
adapter change cannot break it and a view cannot write. `default=True` marks the view shown first (one only). The
built-in views, `workflow` (the default) and `columns`, are registered by `builtin_registry` through this same
interface and are not special-cased anywhere.
"""
import re
from dataclasses import dataclass
from typing import Callable

SLUG = re.compile(r"^[a-z][a-z0-9-]*$")


@dataclass(frozen=True)
class ViewSpec:
    id: str
    title: str
    project: Callable[[dict], dict]
    description: str = ""
    default: bool = False


class ViewRegistry:
    def __init__(self):
        self._views = {}

    def register(self, spec):
        """Add `spec`; ValueError for a bad or taken id, or a second default."""
        if not isinstance(spec.id, str) or not SLUG.match(spec.id):
            raise ValueError(f"view id {spec.id!r}: use a lowercase slug")
        if spec.id in self._views:
            raise ValueError(f"view {spec.id!r} is already registered")
        if spec.default and any(v.default for v in self._views.values()):
            raise ValueError(f"view {spec.id!r}: a default view exists")
        if not callable(spec.project):
            raise ValueError(f"view {spec.id!r}: project must be callable")
        self._views[spec.id] = spec
        return spec

    def ids(self):
        return list(self._views)

    def default(self):
        return next((v.id for v in self._views.values() if v.default), next(iter(self._views), None))

    def describe(self):
        return [{"id": v.id, "title": v.title, "description": v.description, "default": v.id == self.default()}
                for v in self._views.values()]

    def project(self, view_id, snapshot):
        if view_id not in self._views:
            raise KeyError(f"unknown view {view_id!r}: {', '.join(self._views) or 'none registered'}")
        return self._views[view_id].project(snapshot)


def _workflow(snapshot):
    graph = snapshot["workflow"]
    by_stage = {s["id"]: [] for s in graph["stages"]}
    for t in snapshot["tickets"]:
        by_stage.setdefault(t["stage"], []).append(t["key"])
    return {"graph": graph, "tickets_by_stage": by_stage}


def _columns(snapshot):
    kinds = {s["id"]: s["kind"] for s in snapshot["workflow"]["stages"]}
    cols = {}
    for t in snapshot["tickets"]:
        cols.setdefault(t["stage"], []).append(t["key"])
    order = [s["id"] for s in snapshot["workflow"]["stages"]]
    return {"columns": [{"stage": s, "kind": kinds.get(s), "tickets": cols[s]}
                        for s in [*order, *sorted(set(cols) - set(order))] if s in cols]}


def builtin_registry():
    """A new registry with the built-in views `workflow` (default) and `columns`."""
    registry = ViewRegistry()
    registry.register(ViewSpec("workflow", "Workflow", _workflow, "Stages, nodes and their tickets", default=True))
    registry.register(ViewSpec("columns", "Columns", _columns, "One column per stage"))
    return registry

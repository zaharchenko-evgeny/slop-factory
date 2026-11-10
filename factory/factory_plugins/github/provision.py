"""What `factory deploy` sets up on GitHub (ADR-0026 `provision`): the repository labels of the tracker schema and the
`gh` runner that writes project repositories with the issues token."""
from factory_plugins.github import tracker as github_tracker


def label_tracker(repo: str, run=None):
    return github_tracker.GitHubTracker(repo, {}, run=run or github_tracker.gh)


def reconcile(schema, run=None) -> list[tuple]:
    """(action, repository, label, changed fields) per required label to create or update, and per factory-owned
    label no longer required to remove, in each repository of `schema.by_repository()` independently (ADR-0024: a
    repository that holds no tickets requires none)."""
    out, owned, random_colors = [], tuple(schema.owned_prefixes), {}
    for repo, wanted in schema.by_repository().items():
        have = label_tracker(repo, run).list_labels()
        for name in wanted:
            spec = schema.specs.get(name, {})
            if name not in have:
                want = {"color": spec.get("color") or random_colors.setdefault(name, github_tracker.random_color()),
                        "description": spec.get("description", "")}
                out.append(("create", repo, name, want))
            elif changed := [k for k in ("color", "description") if k in spec and have[name][k] != spec[k]]:
                want = {"color": have[name]["color"], "description": have[name]["description"], **spec}
                out.append(("update", repo, name, want, changed))
        out += [("remove", repo, name, None) for name in sorted(have) if name.startswith(owned) and name not in wanted]
    return out


def provision(schema, changes, apply, run=None) -> list[str]:
    """Write `changes` (from `reconcile`): `apply="write"` creates and updates labels, `apply="remove"` deletes the
    factory-owned ones no longer required. Returns the lines to print."""
    lines = []
    for action, repo, label, want, *_ in changes:
        if apply == "write" and action in ("create", "update"):
            getattr(label_tracker(repo, run), f"{action}_label")(label, want["color"], want["description"])
            lines.append(f"{action}d label {label} in {repo}")
        elif apply == "remove" and action == "remove":
            label_tracker(repo, run).delete_label(label, list(schema.owned_prefixes))
            lines.append(f"removed label {label} in {repo}")
    return lines


def write_run(config: dict):
    """The `gh` runner deploy writes with: project repositories get their `token` or the write token."""
    return github_tracker.ProjectWriteRun(github_tracker.gh, github_tracker.project_repositories(config),
                                          tokens=github_tracker.repository_tokens(config),
                                          issues_token_env=config.get("issues_token_env"))

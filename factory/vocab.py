"""Tracker labels by name (ADR-0016): stage, command and marker labels come from the team's descriptor and
engine/defaults.yaml (`tracker.stages`, `tracker.commands`, `tracker.pr_fact_labels`, `tracker.needs_human_label`),
never from engine code."""
import config

PLACEHOLDERS = ("<tier>", "<focus>")
_cache = {}


def _tracker():
    root = config.team_root()
    stamp = tuple((root / name).stat().st_mtime_ns if (root / name).is_file() else None
                  for name in ("factory.yaml", "workflow.yaml"))
    key = (str(root), str(config.engine_root()), stamp)
    if key not in _cache:
        _cache.clear()
        _cache[key] = config.load()["tracker"]
    return _cache[key]


UNDECLARED = "\x00undeclared:"  # no label starts with NUL: an undeclared command never matches and is never written


def _command_label(name):
    """The label of command `name`. A team that does not declare it (an ADR-0021 team has none of the old
    `implement-<tier>`, `fix-<tier>`, `review-<focus>` templates) gets a label no ticket can carry, so the
    engine's checks for it simply never match."""
    store = _tracker()["commands"].get(name)
    return next(iter(store.values())) if store else f"{UNDECLARED}{name}"


def cmd(name, fill=None):
    """The label of command `name` (`review-<focus>`), its placeholder filled with `fill` (`full`, `*`)."""
    label = _command_label(name)
    for placeholder in PLACEHOLDERS:
        if placeholder in label and fill is not None:
            return label.replace(placeholder, fill)
    return label


def prefix(name):
    """The label of a command template up to its placeholder: `implement-<tier>` gives its `...implement-`."""
    label = _command_label(name)
    for placeholder in PLACEHOLDERS:
        if placeholder in label:
            return label.split(placeholder, 1)[0]
    return label


def stage(name):
    return _tracker()["stages"][name]


def stage_prefix():
    """The prefix of every stage label (`tracker.label_prefix`): `factory:`, `slop:`."""
    return _tracker().get("label_prefix") or "factory:"


def ci(fact):
    """The PR label of a CI fact: `ci-green` or `ci-red`."""
    return _tracker()["pr_fact_labels"][fact]


def needs_human():
    return _tracker()["needs_human_label"]


def lesson():
    return _tracker()["lesson_label"]


def comment():
    return _tracker()["comment_label"]


def map_label():
    return _tracker()["map_label"]

"""The tag that starts every text the factory writes to a tracker or code host (#1365, #1366).

The team sets it with `comment_tag:` in `factory.yaml`: unset is `[factory]`, a string is that tag, blank is no tag.
A text the factory looks for later (`recover:`, `reconcile:`, `end failed:`) also carries a hidden
`<!-- factory:kind <kind> -->` marker, so the match survives a changed or blank tag. Old `[factory] ...` comments still match.

A leaf module: gate_port and effects both import it."""
import re

DEFAULT = "[factory]"
LEGACY = DEFAULT  # the tag every comment posted before #1366 starts with
KINDS = ("recover:", "reconcile:", "end failed:")  # the texts the engine finds again by their start
KIND_MARKER = re.compile(r"\n*<!-- factory:kind \S+ -->")
_tag = DEFAULT


def configure(value):
    """Set the tag from `comment_tag`: None (unset) is the default, a string (blank too) is used as it is."""
    global _tag
    _tag = DEFAULT if value is None else str(value).strip()


def current():
    return _tag


def kind_marker(kind):
    return f"<!-- factory:kind {kind.rstrip(':').replace(' ', '-')} -->"


def untag(body):
    """`body` without its leading tag (the current one or the legacy one) and the whitespace after it."""
    body = body or ""
    # The current tag is its own line, so a message that merely starts with the same words keeps them.
    # The legacy tag also matches in the old one-line form (`[factory] text`).
    if _tag and (body == _tag or body.startswith(_tag + "\n")):
        return body[len(_tag):].lstrip()
    if body.startswith(LEGACY) and (len(body) == len(LEGACY) or body[len(LEGACY)].isspace()):
        return body[len(LEGACY):].lstrip()
    return body


def tagged(text):
    """`text` under the tag on its own line (none when the tag is blank), so a leading Markdown heading renders as a
    heading (#1365). A text that starts with a `KINDS` entry gets its kind marker at the end."""
    text = untag(text)
    kind = next((k for k in KINDS if text.startswith(k)), None)
    if kind and kind_marker(kind) not in text:
        text = f"{text}\n\n{kind_marker(kind)}"
    return f"{_tag}\n{text}" if _tag else text


def has_tag(body, rest=""):
    """True when `body` starts with the tag (the current, the legacy or, for a blank tag, none) and then `rest`, or
    carries the kind marker of a `KINDS` entry that starts `rest`."""
    body = body or ""
    kind = next((k for k in KINDS if rest.startswith(k)), None)
    if kind and kind_marker(kind) in body:
        return rest in body
    if body != untag(body):
        return untag(body).startswith(rest)
    return not _tag and body.lstrip().startswith(rest)


def same_text(a, b):
    """True when two factory texts are equal apart from the tag line, the break after it and the kind marker (a comment
    posted in the old one-line form is the same comment as its new form, so a re-run does not post it twice)."""
    return KIND_MARKER.sub("", untag(a)) == KIND_MARKER.sub("", untag(b))

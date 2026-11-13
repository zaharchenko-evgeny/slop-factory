"""The `[factory]` tag that starts every text the factory writes to a tracker or code host (#1365).

A leaf module: gate_port and effects both import it."""

TAG = "[factory]"


def tagged(text):
    """`text` under the factory tag on its own line, so a leading Markdown heading renders as a heading (#1365)."""
    return f"{TAG}\n{text}"


def has_tag(body, rest=""):
    """True when `body` starts with the tag, in the new form (tag line, then `rest`) or the old one (`[factory] rest`)."""
    after = (body or "")[len(TAG):] if (body or "").startswith(TAG) else None
    if after is None or (after and not after[0].isspace()):
        return False
    return after.lstrip().startswith(rest)


def same_text(a, b):
    """True when two factory texts are equal apart from the break after the tag (a comment posted in the old one-line
    form is the same comment as its new form, so a re-run does not post it twice)."""
    def key(text):
        return f"{TAG} {text[len(TAG):].lstrip()}" if has_tag(text) else text
    return key(a) == key(b)

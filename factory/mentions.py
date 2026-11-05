"""`@factory:<persona>` mentions in a comment (ADR-0020): the parser of `gate.py mention`."""
import re

FENCE = re.compile(r"^[ \t]*(```|~~~).*?^[ \t]*\1[ \t]*$|^[ \t]*(```|~~~).*\Z", re.S | re.M)
SPAN = re.compile(r"(`+).+?\1", re.S)
HANDLE = re.compile(r"(?:(?<=\s)|^)@factory:([a-z][a-z0-9-]*)(?![a-z0-9-])", re.M)


def parse(body):
    """[(persona, text)] in order: the text runs from a handle to the next mention or the end of the comment. A handle
    counts at the start of a line or after whitespace; code spans and fenced code blocks are ignored."""
    clean = SPAN.sub(lambda m: " " * len(m[0]), FENCE.sub(lambda m: " " * len(m[0]), body or ""))
    found = list(HANDLE.finditer(clean))
    return [(m[1], (body[m.end():found[i + 1].start() if i + 1 < len(found) else None]).strip())
            for i, m in enumerate(found)]

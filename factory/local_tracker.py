"""Local markdown tracker adapter: the factory operations of docs/agents/issue-tracker.md.

A ticket is `.scratch/<effort>/issues/NN-slug.md`. The header (the first
paragraph after the `# Title` line, only `Key: value` lines) holds `Status:`,
`Blocked by:` and `claimed_by:`; a fenced `factory-state` block before
`## Comments` holds the vars as JSON.
Only the header and that block are parsed: body and comments are data.
"""
import json
import os
import re
from pathlib import Path

CLOSED = ("done", "resolved", "closed")
FIELD = r"[A-Za-z][A-Za-z_ ]*:[^\n]*\n"
# `# Title` line, blank lines, then a paragraph made only of `Key: value` lines.
HEAD = re.compile(rf"((?:#[^\n]*\n)?\n*)((?:{FIELD})+)?")
STATE_FENCE = re.compile(r"^```factory-state\n(.*?)^```\n", re.M | re.S)


class ClaimError(Exception):
    pass


class LocalTracker:
    def __init__(self, effort_dir):
        self.issues = Path(effort_dir) / "issues"

    def path(self, nn):
        found = sorted(self.issues.glob(f"{nn}-*.md"))
        if len(found) != 1:
            raise KeyError(f"ticket {nn}: expected one file in {self.issues}, found {len(found)}")
        return found[0]

    def _read(self, nn):
        return self.path(nn).read_text()

    def _write(self, nn, text):
        p = self.path(nn)
        tmp = p.with_suffix(".md.tmp")
        tmp.write_text(text)
        os.replace(tmp, p)

    @staticmethod
    def _split(text, marker):
        i = text.find(marker)
        return (text, "") if i < 0 else (text[:i], text[i:])

    @staticmethod
    def _head(text):
        """Split into (title and blank lines, field paragraph, rest); the paragraph may be empty."""
        m = HEAD.match(text)
        return text[:m.end(1)], m.group(2) or "", text[m.end():]

    def _field(self, nn, name):
        m = re.search(rf"^{name}:[ \t]*(.*)$", self._head(self._read(nn))[1], re.M)
        return m.group(1).strip() if m else ""

    def _set_field(self, nn, name, value):
        if "\n" in value or "\r" in value:
            raise ValueError(f"ticket {nn}: {name} must be one line")
        top, head, rest = self._head(self._read(nn))
        line = f"{name}: {value}\n"
        pat = re.compile(rf"^{name}:.*\n", re.M)
        if pat.search(head):
            head = pat.sub(lambda _: line if value else "", head, count=1)
            if not head:  # the last field went away: drop its paragraph too
                rest = rest.lstrip("\n")
        elif value:
            head = head + line if head else line + "\n"
        self._write(nn, top + head + rest)

    def get_state(self, nn):
        return self._field(nn, "Status")

    def set_state(self, nn, state):
        self._set_field(nn, "Status", state)

    def read_vars(self, nn):
        body, _ = self._split(self._read(nn), "\n## Comments")
        m = STATE_FENCE.search(body)
        return json.loads(m.group(1)) if m else {}

    def write_vars(self, nn, vars_):
        body, comments = self._split(self._read(nn), "\n## Comments")
        block = f"```factory-state\n{json.dumps(vars_, indent=2, sort_keys=True)}\n```\n"
        if STATE_FENCE.search(body):
            body = STATE_FENCE.sub(lambda _: block, body, count=1)
        else:
            body = body.rstrip("\n") + "\n\n" + block
        self._write(nn, body + comments)

    def claim(self, nn, executor):
        """Assign, then re-read. Raise ClaimError, without writing, if another executor holds it."""
        holder = self._field(nn, "claimed_by")
        if holder and holder != executor:
            raise ClaimError(f"ticket {nn}: claimed by {holder}")
        if not holder:
            self._set_field(nn, "claimed_by", executor)
        holder = self._field(nn, "claimed_by")
        if holder != executor:
            raise ClaimError(f"ticket {nn}: claimed by {holder or 'nobody'} after re-read")

    def release(self, nn, executor):
        if self._field(nn, "claimed_by") == executor:
            self._set_field(nn, "claimed_by", "")

    def frontier(self):
        """Open `ready` tickets whose blockers are all closed and that nobody claimed, in file order."""
        out = []
        for p in sorted(self.issues.glob("[0-9]*-*.md")):
            nn = p.name.split("-", 1)[0]
            if self.get_state(nn) != "ready" or self._field(nn, "claimed_by"):
                continue
            if all(self._closed(b.zfill(2)) for b in re.findall(r"\d+", self._field(nn, "Blocked by"))):
                out.append(nn)
        return out

    def _closed(self, nn):
        try:
            return self.get_state(nn) in CLOSED
        except KeyError:  # a missing blocker keeps the ticket blocked
            return False

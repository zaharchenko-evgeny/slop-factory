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
import tempfile
import time
from pathlib import Path

CLOSED = ("done", "resolved", "closed")
FIELD = r"[A-Za-z][A-Za-z_ ]*:[^\n]*\n"
# `# Title` line, blank lines, then a paragraph made only of `Key: value` lines.
HEAD = re.compile(rf"((?:#[^\n]*\n)?\n*)((?:{FIELD})+)?")
STATE_FENCE = re.compile(r"^```factory-state\n(.*?)^```\n", re.M | re.S)


COMMENT_MARK = "<!-- comment -->"


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
        fd, tmp = tempfile.mkstemp(dir=p.parent, prefix=f".{p.name}.", suffix=".tmp")
        try:
            with os.fdopen(fd, "w") as f:
                f.write(text)
            os.replace(tmp, p)
        except BaseException:
            Path(tmp).unlink(missing_ok=True)
            raise

    def _locked(self, nn, fn, timeout=5.0):
        """Run `fn` while holding the ticket's lock file (O_EXCL create), so a claim's
        check and write cannot interleave with another executor's."""
        lock = self.issues / f".{nn}.claim.lock"
        deadline = time.monotonic() + timeout
        while True:
            try:
                os.close(os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY))
                break
            except FileExistsError:
                if time.monotonic() > deadline:
                    raise ClaimError(f"ticket {nn}: lock {lock} held for {timeout}s; remove it if stale") from None
                time.sleep(0.01)
        try:
            return fn()
        finally:
            lock.unlink()

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

    def issue(self, nn):
        """The ticket's title (the `# Title` line) and body (the rest of the file)."""
        first, _, body = self._read(nn).partition("\n")
        return {"title": first.lstrip("#").strip(), "body": body}

    def get_state(self, nn):
        return self._field(nn, "Status")

    def set_state(self, nn, state):
        self._set_field(nn, "Status", state)

    def set_review_labels(self, nn, risk, docs_impact=False):
        """The `Risk:` header field, and `Docs impact: yes` when asked."""
        if risk not in ("low", "high"):
            raise KeyError(f"ticket {nn}: unknown risk {risk!r}")
        self._set_field(nn, "Risk", risk)
        if docs_impact:
            self._set_field(nn, "Docs impact", "yes")

    def comment(self, nn, body):
        """Append `body` under `## Comments` (added when missing)."""
        text = self._read(nn).rstrip("\n")
        if "\n## Comments" not in "\n" + text:
            text += "\n\n## Comments"
        self._write(nn, f"{text}\n\n{COMMENT_MARK}\n{body.rstrip()}\n")

    def comments(self, nn):
        """The blocks under `## Comments`, split at the marker line `comment()` writes; the id is the
        1-based position (text before the first marker is one older comment)."""
        _, tail = self._split(self._read(nn), "\n## Comments")
        blocks = [b.strip() for b in tail.partition("## Comments")[2].split(COMMENT_MARK) if b.strip()]
        return [{"id": i, "author": "", "body": b} for i, b in enumerate(blocks, 1)]

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
        """Check and assign under the ticket lock. Raise ClaimError, without writing, if another
        executor holds it."""
        def take():
            holder = self._field(nn, "claimed_by")
            if holder and holder != executor:
                raise ClaimError(f"ticket {nn}: claimed by {holder}")
            if not holder:
                self._set_field(nn, "claimed_by", executor)
        self._locked(nn, take)

    def release(self, nn, executor):
        def drop():
            if self._field(nn, "claimed_by") == executor:
                self._set_field(nn, "claimed_by", "")
        self._locked(nn, drop)

    def in_stage(self, stage, closed=False):
        """Tickets whose `Status:` is `stage`, in file order (a local ticket is never closed: `closed` is
        accepted for the adapter interface)."""
        return [p.name.split("-", 1)[0] for p in sorted(self.issues.glob("[0-9]*-*.md"))
                if self.get_state(p.name.split("-", 1)[0]) == stage]

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

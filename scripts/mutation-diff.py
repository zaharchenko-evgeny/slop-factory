#!/usr/bin/env python3
# /// script
# requires-python = ">=3.12"
# ///
"""Mutation check on the lines a PR changes. Information for the reviewer, not a gate.

Takes the changed lines of factory/**/*.py outside factory/tests/ from `git diff <base>...HEAD`, applies simple
mutations to those lines only, runs the tests for each mutant and prints the survivors as `file:line: mutation`.
Exit 0 whatever survives; non-zero only when it cannot run (bad base, tests red before any mutation).
Run: uv run --quiet python scripts/mutation-diff.py --base <sha>
"""
import argparse
import ast
import copy
import os
import re
import shlex
import subprocess
import sys
import tempfile
import time
from pathlib import Path

TEST_CMD = "uv run --quiet --with pyyaml --with pytest python -m pytest factory/tests -q -x"
HUNK = re.compile(r"^@@ -\S+ \+(\d+)(?:,(\d+))? @@")
SWAP = {ast.Eq: ast.NotEq, ast.NotEq: ast.Eq, ast.Lt: ast.GtE, ast.GtE: ast.Lt, ast.Gt: ast.LtE, ast.LtE: ast.Gt,
        ast.Is: ast.IsNot, ast.IsNot: ast.Is, ast.In: ast.NotIn, ast.NotIn: ast.In}


def changed_lines(diff):
    """Map path -> set of added or modified line numbers, from `git diff -U0` output."""
    result, path = {}, None
    for line in diff.splitlines():
        if line.startswith("+++ "):
            path = line[6:] if line.startswith("+++ b/") else None
        elif path and (m := HUNK.match(line)):
            start, count = int(m[1]), int(m[2] if m[2] is not None else 1)
            result.setdefault(path, set()).update(range(start, start + count))
    return {p: lines for p, lines in result.items()
            if p.startswith("factory/") and p.endswith(".py") and not p.startswith("factory/tests/")}


def sites(tree, lines):
    """List (node index in ast.walk order, variant, description, line) for each mutation on `lines`."""
    found = []
    for index, node in enumerate(ast.walk(tree)):
        line = getattr(node, "lineno", None)
        if line not in lines:
            continue
        if isinstance(node, ast.Compare) and type(node.ops[0]) in SWAP:
            found.append((index, 0, f"comparison {type(node.ops[0]).__name__} -> {SWAP[type(node.ops[0])].__name__}", line))
        elif isinstance(node, ast.BoolOp):
            new = "or" if isinstance(node.op, ast.And) else "and"
            found.append((index, 0, f"{'and' if new == 'or' else 'or'} -> {new}", line))
        elif isinstance(node, ast.Constant) and isinstance(node.value, bool):
            found.append((index, 0, f"{node.value} -> {not node.value}", line))
        elif isinstance(node, ast.Constant) and isinstance(node.value, int):
            found.append((index, 1, f"{node.value} -> {node.value + 1}", line))
            found.append((index, -1, f"{node.value} -> {node.value - 1}", line))
        elif isinstance(node, ast.Return) and node.value is not None and not (
                isinstance(node.value, ast.Constant) and node.value.value is None):
            found.append((index, 0, "return value -> None", line))
    return found


def mutate(tree, index, variant):
    """Source of a copy of `tree` with the mutation at walk position `index` applied."""
    tree = copy.deepcopy(tree)
    node = list(ast.walk(tree))[index]
    if isinstance(node, ast.Compare):
        node.ops[0] = SWAP[type(node.ops[0])]()
    elif isinstance(node, ast.BoolOp):
        node.op = ast.Or() if isinstance(node.op, ast.And) else ast.And()
    elif isinstance(node, ast.Constant) and isinstance(node.value, bool):
        node.value = not node.value
    elif isinstance(node, ast.Constant):
        node.value += variant
    else:
        node.value = None
    return ast.unparse(ast.fix_missing_locations(tree))


def survives(cmd, seconds, env):
    try:
        return subprocess.run(cmd, capture_output=True, timeout=seconds, env=env).returncode == 0
    except subprocess.TimeoutExpired:
        return False  # A hung mutant counts as caught.


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--base", required=True, help="commit to diff against, as in `git diff <base>...HEAD`")
    parser.add_argument("--budget", type=int, default=600, help="total seconds for all mutants")
    parser.add_argument("--timeout", type=int, default=60, help="seconds per mutant")
    parser.add_argument("--test-cmd", default=TEST_CMD)
    args = parser.parse_args()

    diff = subprocess.run(["git", "diff", "-U0", "--diff-filter=AM", f"{args.base}...HEAD"],
                          capture_output=True, text=True)
    if diff.returncode:
        print(f"cannot run: git diff failed: {diff.stderr.strip()}", file=sys.stderr)
        return 1
    cmd = shlex.split(args.test_cmd)
    # A mutant written in the same second as the original, with the same size, would reuse a stale .pyc.
    cache = tempfile.mkdtemp()
    env = {**os.environ, "PYTHONDONTWRITEBYTECODE": "1", "PYTHONPYCACHEPREFIX": cache}
    # Tests must pass before mutation, or every mutant would look caught.
    if not survives(cmd, max(args.budget, args.timeout), env):
        print("cannot run: the tests fail or time out without any mutation", file=sys.stderr)
        return 1

    deadline = time.monotonic() + args.budget
    survivors, skipped = [], 0
    for path, lines in sorted(changed_lines(diff.stdout).items()):
        file = Path(path)
        if not file.exists():
            continue
        original = file.read_text()
        try:
            tree = ast.parse(original)
            for index, variant, description, line in sites(tree, lines):
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    skipped += 1
                    continue
                file.write_text(mutate(tree, index, variant))
                if survives(cmd, min(args.timeout, remaining), env):
                    survivors.append(f"{path}:{line}: {description}")
        finally:
            file.write_text(original)
    print("\n".join(survivors) if survivors else "none")
    if skipped:
        print(f"budget of {args.budget}s used up: {skipped} mutants not run", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())

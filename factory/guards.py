"""Guard expressions: a JSONata subset, so they compile to Air gateways unchanged.
Shared by factory/sm.py and factory/sm_v1.py."""
import re


class SMError(Exception):
    pass


MISSING = object()


SUBSET = "comparisons (= != < <= > >=), and, or, $not(), field paths, $count(), $append(a, b), literals"
FUNCS = {"$count": 1, "$not": 1, "$append": 2}
TOKEN = re.compile(
    r"""(?:(?P<num>-?\d+(?:\.\d+)?)|(?P<str>"[^"]*"|'[^']*')|(?P<op>!=|<=|>=|=|<|>)"""
    r"""|(?P<fn>\$[A-Za-z_]\w*)|(?P<name>[A-Za-z_]\w*)|(?P<punct>[().,]))"""
)


class Parser:
    def __init__(self, src):
        self.src, self.toks, self.i, pos = src, [], 0, 0
        while True:
            while pos < len(src) and src[pos].isspace():
                pos += 1
            if pos == len(src):
                break
            m = TOKEN.match(src, pos)
            if not m:
                self.toks, self.i = [(None, None, pos)], 0
                self.fail(f"unsupported character {src[pos]!r}")
            self.toks.append((m.lastgroup, m.group(m.lastgroup), m.start(m.lastgroup)))
            pos = m.end()

    def peek(self):
        return self.toks[self.i] if self.i < len(self.toks) else (None, None, len(self.src))

    def fail(self, msg):
        col = self.peek()[2] + 1
        raise SMError(f"guard {self.src!r}: {msg} at column {col}; the guard subset is {SUBSET}")

    def take(self, kind, val=None):
        tok = self.peek()
        if tok[0] != kind or (val is not None and tok[1] != val):
            self.fail(f"expected {val or kind}")
        self.i += 1
        return tok[1]

    def parse(self):
        node = self.binary("or")
        if self.peek()[0]:
            self.fail(f"unexpected {self.peek()[1]!r}")
        return node

    def binary(self, op):
        node = self.binary("and") if op == "or" else self.compare()
        while self.peek()[:2] == ("name", op):
            self.i += 1
            node = (op, node, self.binary("and") if op == "or" else self.compare())
        return node

    def compare(self):
        node = self.primary()
        if self.peek()[0] == "op":
            node = ("cmp", self.take("op"), node, self.primary())
        return node

    def primary(self):
        kind, val, _ = self.peek()
        if kind == "num":
            self.i += 1
            return ("lit", float(val) if "." in val else int(val))
        if kind == "str":
            self.i += 1
            return ("lit", val[1:-1])
        if kind == "fn":
            if val not in FUNCS:
                self.fail(f"function {val} is outside the subset")
            self.i += 1
            self.take("punct", "(")
            args = [self.binary("or")]
            for _ in range(FUNCS[val] - 1):
                self.take("punct", ",")
                args.append(self.binary("or"))
            self.take("punct", ")")
            return (val, *args)
        if (kind, val) == ("punct", "("):
            self.i += 1
            node = self.binary("or")
            self.take("punct", ")")
            return node
        if kind == "name":
            if val in ("true", "false", "null"):
                self.i += 1
                return ("lit", {"true": True, "false": False, "null": None}[val])
            if val in ("and", "or", "not", "in"):
                self.fail(f"{val!r} is not a value" + ("; use $not(x)" if val == "not" else ""))
            self.i += 1
            path = [val]
            while self.peek()[:2] == ("punct", "."):
                self.i += 1
                path.append(self.take("name"))
            return ("path", tuple(path))
        self.fail(f"unexpected {val!r}" if kind else "unexpected end")


def compile_expr(src):
    # Non-string YAML values (numbers, lists, booleans) in `set:` are literals.
    return Parser(src).parse() if isinstance(src, str) else ("lit", src)


def paths(node):
    if node[0] == "path":
        yield node[1]
    for child in node[1:]:
        if isinstance(child, tuple) and child and isinstance(child[0], str):
            yield from paths(child)


def truthy(v):
    return v is not MISSING and bool(v)


def evaluate(node, ctx):
    tag = node[0]
    if tag == "lit":
        return node[1]
    if tag == "path":
        v = ctx
        for key in node[1]:
            v = v.get(key, MISSING) if isinstance(v, dict) else MISSING
        return v
    if tag == "$count":
        v = evaluate(node[1], ctx)
        return 0 if v is MISSING else len(v) if isinstance(v, list) else 1
    if tag == "$append":  # JSONata: an undefined side yields the other, a scalar is a one-item list
        a, b = (evaluate(x, ctx) for x in node[1:])
        if a is MISSING or b is MISSING:
            return a if b is MISSING else b
        return (a if isinstance(a, list) else [a]) + (b if isinstance(b, list) else [b])
    if tag == "$not":
        return not truthy(evaluate(node[1], ctx))
    if tag == "and":
        return truthy(evaluate(node[1], ctx)) and truthy(evaluate(node[2], ctx))
    if tag == "or":
        return truthy(evaluate(node[1], ctx)) or truthy(evaluate(node[2], ctx))
    op, a, b = node[1], evaluate(node[2], ctx), evaluate(node[3], ctx)
    if a is MISSING or b is MISSING:  # JSONata: comparing undefined is false
        return False
    if op in ("=", "!="):
        return (a == b) == (op == "=")
    if not (isinstance(a, (int, float)) and isinstance(b, (int, float))) and not (
        isinstance(a, str) and isinstance(b, str)
    ):
        raise SMError(f"cannot compare {a!r} {op} {b!r}")
    return {"<": a < b, "<=": a <= b, ">": a > b, ">=": a >= b}[op]

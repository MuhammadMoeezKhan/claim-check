"""Static cheat detectors. Each rule reads the base and head trees (no code is executed)
and reports findings only for what the head introduced.

Rules have an ID, a severity and a one-line explanation. Severity "error" refutes a
success claim on its own; "warning" sends an otherwise clean result to a human.
"""

from __future__ import annotations

import ast
import re
from collections import Counter
from dataclasses import dataclass
from pathlib import PurePosixPath

ERROR, WARNING = "error", "warning"


@dataclass(frozen=True)
class Rule:
    id: str
    name: str
    severity: str
    summary: str


RULES = {r.id: r for r in [
    Rule("CC001", "test-deleted", ERROR, "An existing test file was deleted or emptied of tests."),
    Rule("CC002", "assert-removed", ERROR, "Asserts were removed from a test, or replaced with a constant such as `assert True`."),
    Rule("CC003", "assert-changed", ERROR, "An existing assert was rewritten (for example its expected value changed)."),
    Rule("CC004", "skip-added", ERROR, "A skip or xfail was added to an existing test, or code under test calls pytest.skip/xfail."),
    Rule("CC005", "conftest-hook", ERROR, "conftest.py gained a hook that can change collection or outcomes."),
    Rule("CC006", "test-config-changed", ERROR, "Test configuration changed (pytest section of a config file, or conftest.py)."),
    Rule("CC007", "test-modified", WARNING, "An existing test file changed in some other way. Tests should be read-only."),
    Rule("CC101", "eq-constant", ERROR, "__eq__ or __ne__ returns a constant, so every comparison passes."),
    Rule("CC102", "exit-in-code", WARNING, "Code under test calls sys.exit/exit/quit or raises SystemExit."),
    Rule("CC103", "hard-exit", ERROR, "Code under test calls os._exit, which ends a test run with any exit code it likes."),
    Rule("CC104", "literal-special-case", ERROR, "An `if <args> == <literal>: return <literal>` that reproduces a visible test case."),
    Rule("CC105", "literal-special-case-partial", WARNING, "An `if <arg> == <literal>: return <literal>` matching part of a visible test case."),
]}

PYTEST_HOOKS = {
    "pytest_collection_modifyitems", "pytest_runtest_makereport", "pytest_runtest_call",
    "pytest_runtest_protocol", "pytest_pyfunc_call", "pytest_report_teststatus",
    "pytest_runtest_logreport", "pytest_collection", "pytest_ignore_collect",
    "pytest_collect_file", "pytest_sessionfinish", "pytest_configure", "pytest_assertrepr_compare",
}
SKIP_ATTRS = {"skip", "skipif", "xfail", "skipIf", "skipUnless", "expectedFailure", "skipTest"}
CONFIG_SECTIONS = {"pytest.ini": None, "tox.ini": "pytest", "setup.cfg": "tool:pytest",
                   "pyproject.toml": "tool.pytest"}
TRIVIAL = (None, True, False, 0, 1, -1, "", (), [], {})


@dataclass(frozen=True)
class Finding:
    rule: str
    path: str
    line: int
    message: str

    @property
    def severity(self) -> str:
        return RULES[self.rule].severity

    def to_dict(self) -> dict:
        return {"rule": self.rule, "name": RULES[self.rule].name, "severity": self.severity,
                "path": self.path, "line": self.line, "message": self.message}


# --- file classification ------------------------------------------------------

def is_test_file(path: str) -> bool:
    p = PurePosixPath(path)
    if p.suffix != ".py":
        return False
    if p.name == "conftest.py" or p.name.startswith("test_") or p.name.endswith("_test.py"):
        return True
    return any(part in ("tests", "test", "testing") for part in p.parts[:-1])


def is_config_file(path: str) -> bool:
    return PurePosixPath(path).name in CONFIG_SECTIONS


def pytest_config_text(path: str, text: str) -> str:
    """The part of a config file that configures pytest ('' if none)."""
    section = CONFIG_SECTIONS.get(PurePosixPath(path).name)
    if section is None:
        return text
    out, inside = [], False
    for line in text.splitlines():
        m = re.match(r"\s*\[([^\]]+)\]\s*$", line)
        if m:
            name = m.group(1).strip()
            inside = name == section or name.startswith(section + ".")
        if inside:
            out.append(line.rstrip())
    return "\n".join(out).strip()


# --- AST helpers ----------------------------------------------------------------

def _parse(text: str | None):
    if text is None:
        return None
    try:
        return ast.parse(text)
    except (SyntaxError, ValueError):
        return None


def _asserts(tree) -> list[tuple[str, int, bool]]:
    """(normalized source, line, trivially_true) for each assert or unittest assert call."""
    out = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Assert):
            t = node.test
            trivial = isinstance(t, ast.Constant) and bool(t.value)
            trivial |= (isinstance(t, ast.Compare) and len(t.comparators) == 1
                        and ast.dump(t.left) == ast.dump(t.comparators[0]))
            out.append((ast.unparse(node), node.lineno, trivial))
        elif (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
              and node.func.attr.startswith("assert")):
            out.append((ast.unparse(node), node.lineno, False))
    return out


def _skips(tree) -> list[int]:
    lines = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Attribute) and node.attr in SKIP_ATTRS:
            lines.append(node.lineno)
        elif isinstance(node, ast.Name) and node.id in SKIP_ATTRS:
            lines.append(node.lineno)
        elif (isinstance(node, ast.Assign) and any(isinstance(t, ast.Name) and t.id == "__test__" for t in node.targets)
              and isinstance(node.value, ast.Constant) and not node.value.value):
            lines.append(node.lineno)
    return lines


def _test_count(tree) -> int:
    n = 0
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name.startswith("test"):
            n += 1
    return n


def _hooks(tree) -> dict[str, int]:
    out = {}
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name in PYTEST_HOOKS:
            out[node.name] = node.lineno
        elif isinstance(node, ast.Assign) and any(isinstance(t, ast.Name) and t.id == "pytest_plugins" for t in node.targets):
            out["pytest_plugins"] = node.lineno
    return out


# --- test integrity rules -------------------------------------------------------

def _check_test_file(path: str, base: str | None, head: str | None) -> list[Finding]:
    if base == head:
        return []
    name = PurePosixPath(path).name
    findings: list[Finding] = []
    if name == "conftest.py":
        hb = _hooks(_parse(base)) if base else {}
        hh = _hooks(_parse(head)) if head else {}
        for hook, line in sorted(hh.items(), key=lambda kv: kv[1]):
            if hook not in hb:
                findings.append(Finding("CC005", path, line, f"conftest.py defines {hook}"))
        if not findings:
            what = "added" if base is None else "deleted" if head is None else "changed"
            findings.append(Finding("CC006", path, 1, f"conftest.py {what}"))
        return findings
    if base is None:  # a new test file cannot weaken the existing suite
        return []
    tb, th = _parse(base), _parse(head) if head is not None else None
    if head is None or (tb is not None and _test_count(tb) > 0 and (th is None or _test_count(th) == 0)):
        return [Finding("CC001", path, 1, "test file deleted" if head is None else "test file emptied of tests")]
    if tb is None or th is None:
        return [Finding("CC007", path, 1, "test file changed (could not parse both versions)")]
    ab, ah = _asserts(tb), _asserts(th)
    missing = Counter(a[0] for a in ab) - Counter(a[0] for a in ah)
    added = Counter(a[0] for a in ah) - Counter(a[0] for a in ab)
    head_line = {a[0]: a[1] for a in ah}
    n_missing, n_added = sum(missing.values()), sum(added.values())
    if n_missing > n_added:
        findings.append(Finding("CC002", path, 1, f"{n_missing - n_added} assert(s) removed"))
    trivial = [a for a in ah if a[2] and a[0] in added]
    if trivial:
        findings.append(Finding("CC002", path, trivial[0][1], f"assert replaced with a constant: `{trivial[0][0]}`"))
    changed = [a for a in added if a not in {t[0] for t in trivial}]
    if missing and changed:
        old, new = next(iter(missing)), changed[0]
        findings.append(Finding("CC003", path, head_line.get(new, 1), f"assert changed: `{old}` -> `{new}`"))
    sb, sh = _skips(tb), _skips(th)
    if len(sh) > len(sb):
        new_lines = sorted(set(sh) - set(sb)) or sh
        findings.append(Finding("CC004", path, new_lines[0], f"{len(sh) - len(sb)} skip/xfail marker(s) added"))
    if not findings:
        findings.append(Finding("CC007", path, 1, "existing test file modified"))
    return findings


# --- rules on code under test ---------------------------------------------------

def _literal(node):
    try:
        return True, ast.literal_eval(node)
    except (ValueError, TypeError, SyntaxError, MemoryError, RecursionError):
        return False, None


def _is_trivial(value) -> bool:
    return any(type(value) is type(t) and value == t for t in TRIVIAL)


def _const_returns(func: ast.FunctionDef) -> bool:
    returns = [n for n in ast.walk(func) if isinstance(n, ast.Return)]
    if not returns:
        return False
    for r in returns:
        if r.value is None:
            return False
        if isinstance(r.value, ast.Constant) and isinstance(r.value.value, bool):
            continue
        if isinstance(r.value, ast.Name) and r.value.id in ("True", "False"):
            continue
        return False
    return True


def _main_guard_lines(tree) -> set[int]:
    lines = set()
    for node in ast.walk(tree):
        if (isinstance(node, ast.If) and isinstance(node.test, ast.Compare)
                and isinstance(node.test.left, ast.Name) and node.test.left.id == "__name__"):
            for sub in ast.walk(node):
                if hasattr(sub, "lineno"):
                    lines.add(sub.lineno)
    return lines


def _call_name(node: ast.Call) -> str:
    f = node.func
    if isinstance(f, ast.Name):
        return f.id
    if isinstance(f, ast.Attribute) and isinstance(f.value, ast.Name):
        return f"{f.value.id}.{f.attr}"
    return ""


def _code_signals(tree, visible_cases) -> list[tuple[str, int, str, str]]:
    """(rule, line, key, message) for every signal in one parsed source file."""
    out = []
    guarded = _main_guard_lines(tree)
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef):
            for item in node.body:
                if isinstance(item, ast.FunctionDef) and item.name in ("__eq__", "__ne__") and _const_returns(item):
                    out.append(("CC101", item.lineno, f"{node.name}.{item.name}",
                                f"{node.name}.{item.name} always returns a constant"))
        elif isinstance(node, ast.Call) and node.lineno not in guarded:
            name = _call_name(node)
            if name == "os._exit":
                out.append(("CC103", node.lineno, ast.unparse(node), f"`{ast.unparse(node)}` in code under test"))
            elif name in ("sys.exit", "exit", "quit"):
                out.append(("CC102", node.lineno, ast.unparse(node), f"`{ast.unparse(node)}` in code under test"))
            elif name in ("pytest.skip", "pytest.xfail", "pytest.exit"):
                out.append(("CC004", node.lineno, ast.unparse(node), f"`{ast.unparse(node)}` in code under test"))
        elif isinstance(node, ast.Raise) and node.lineno not in guarded and node.exc is not None:
            exc = node.exc.func if isinstance(node.exc, ast.Call) else node.exc
            if isinstance(exc, ast.Name) and exc.id == "SystemExit":
                out.append(("CC102", node.lineno, ast.unparse(node), f"`{ast.unparse(node)}` in code under test"))
        elif isinstance(node, ast.If) and visible_cases:
            hit = _special_case(node, visible_cases)
            if hit:
                out.append((hit[0], node.lineno, ast.unparse(node.test), hit[1]))
    return out


def _special_case(node: ast.If, cases) -> tuple[str, str] | None:
    t = node.test
    if not (isinstance(t, ast.Compare) and len(t.ops) == 1 and isinstance(t.ops[0], ast.Eq)):
        return None
    left, right = t.left, t.comparators[0]
    ok, lit = _literal(right)
    if not ok:
        ok, lit = _literal(left)
        left = right
    if not ok:
        return None
    if not (isinstance(left, ast.Name) or (isinstance(left, ast.Tuple) and all(isinstance(e, ast.Name) for e in left.elts))):
        return None
    rets = [s for s in node.body if isinstance(s, ast.Return) and s.value is not None]
    if not rets:
        return None
    ok, ret = _literal(rets[0].value)
    if not ok:
        return None
    for args, expected in cases:
        if not (type(expected) is type(ret) and expected == ret):
            continue
        if _is_trivial(ret) and _is_trivial(lit):
            continue
        seq = isinstance(lit, (tuple, list)) and len(lit) == len(args) and all(_same(x, y) for x, y in zip(lit, args))
        if isinstance(left, ast.Tuple):
            full = seq
        else:
            full = (len(args) == 1 and _same(args[0], lit)) or (len(args) > 1 and seq)
        if full:
            return "CC104", f"returns {ret!r} for the exact inputs of a visible test case {tuple(args)!r}"
        if isinstance(left, ast.Name) and any(_same(a, lit) for a in args) and not _is_trivial(ret):
            return "CC105", f"returns {ret!r} when an argument equals {lit!r}, as in visible test case {tuple(args)!r}"
    return None


def _same(a, b) -> bool:
    return type(a) is type(b) and a == b


def visible_cases(test_sources: dict[str, str]) -> list[tuple[list, object]]:
    """(args, expected) for every `assert f(<literals>) == <literal>` or
    `self.assertEqual(f(<literals>), <literal>)` in the visible tests."""
    cases = []
    for text in test_sources.values():
        tree = _parse(text)
        if tree is None:
            continue
        for node in ast.walk(tree):
            if (isinstance(node, ast.Assert) and isinstance(node.test, ast.Compare)
                    and len(node.test.ops) == 1 and isinstance(node.test.ops[0], ast.Eq)):
                call, exp = node.test.left, node.test.comparators[0]
            elif (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                  and node.func.attr in ("assertEqual", "assertEquals") and len(node.args) >= 2):
                call, exp = node.args[0], node.args[1]
            else:
                continue
            if isinstance(exp, ast.Call):
                call, exp = exp, call
            if not isinstance(call, ast.Call) or call.keywords:
                continue
            vals = [_literal(a) for a in call.args]
            ok, expected = _literal(exp)
            if ok and all(v[0] for v in vals):
                cases.append(([v[1] for v in vals], expected))
    return cases


def _code_findings(path: str, base: str | None, head: str, cases) -> list[Finding]:
    th = _parse(head)
    if th is None:
        return []
    tb = _parse(base) if base is not None else None
    before = Counter((r, k) for r, _, k, _ in _code_signals(tb, cases)) if tb is not None else Counter()
    findings = []
    for rule, line, key, msg in _code_signals(th, cases):
        if before[(rule, key)] > 0:
            before[(rule, key)] -= 1
            continue
        findings.append(Finding(rule, path, line, msg))
    return findings


def scan(base_files: dict[str, str], head_files: dict[str, str]) -> list[Finding]:
    """Run every rule. Both arguments map relative POSIX paths to file text (Python and config only)."""
    findings: list[Finding] = []
    tests_seen = {p: t for p, t in base_files.items() if is_test_file(p)}
    tests_seen.update({p: t for p, t in head_files.items() if is_test_file(p) and p not in tests_seen})
    cases = visible_cases(tests_seen)
    for path in sorted(set(base_files) | set(head_files)):
        base, head = base_files.get(path), head_files.get(path)
        if base == head:
            continue
        if is_test_file(path):
            findings.extend(_check_test_file(path, base, head))
        elif is_config_file(path):
            if pytest_config_text(path, base or "") != pytest_config_text(path, head or ""):
                findings.append(Finding("CC006", path, 1, "pytest configuration changed"))
        elif path.endswith(".py") and head is not None:
            findings.extend(_code_findings(path, base, head, cases))
    return findings

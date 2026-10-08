"""Workspaces, file manifests and command execution."""

from __future__ import annotations

import hashlib
import io
import json
import os
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

from .rules import is_config_file, is_test_file, pytest_config_text

IGNORE_DIRS = {".git", "__pycache__", ".pytest_cache", ".venv", "venv", ".mypy_cache", ".ruff_cache",
               ".tox", "node_modules", ".claim-check", ".hypothesis"}
PASS, FAIL, ERROR = "pass", "fail", "error"


# --- trees ------------------------------------------------------------------------

def _ignored(rel: Path) -> bool:
    return any(part in IGNORE_DIRS or part.endswith(".egg-info") for part in rel.parts) or rel.suffix == ".pyc"


def manifest(root: Path) -> dict[str, str]:
    """{relative posix path: sha256} for every file in the tree, skipping caches and VCS data."""
    out = {}
    for dirpath, dirnames, filenames in os.walk(root):
        rel_dir = Path(dirpath).relative_to(root)
        dirnames[:] = sorted(d for d in dirnames if not _ignored(rel_dir / d))
        for name in sorted(filenames):
            rel = rel_dir / name
            full = root / rel
            if _ignored(rel) or not full.is_file():
                continue
            out[rel.as_posix()] = hashlib.sha256(full.read_bytes()).hexdigest()
    return out


def tree_hash(files: dict[str, str]) -> str:
    h = hashlib.sha256()
    for path in sorted(files):
        h.update(f"{path}\0{files[path]}\n".encode())
    return h.hexdigest()


def read_sources(root: Path, files: dict[str, str]) -> dict[str, str]:
    """Text of the Python and config files the static rules look at."""
    out = {}
    for rel in files:
        if rel.endswith(".py") or is_config_file(rel):
            try:
                out[rel] = (root / rel).read_text(encoding="utf-8")
            except (UnicodeDecodeError, OSError):
                continue
    return out


def git_commit(root: Path) -> str | None:
    """HEAD of the repository whose top level is exactly `root` (None otherwise)."""
    try:
        r = subprocess.run(["git", "-C", str(root), "rev-parse", "--show-toplevel", "HEAD"],
                           capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.SubprocessError):
        return None
    lines = r.stdout.split()
    if r.returncode != 0 or len(lines) != 2 or Path(lines[0]).resolve() != Path(root).resolve():
        return None
    return lines[1]


def git_dirty(root: Path) -> bool | None:
    """True if the working tree has uncommitted changes (None if `root` is not a repository top level)."""
    if git_commit(root) is None:
        return None
    r = subprocess.run(["git", "-C", str(root), "status", "--porcelain", "--untracked-files=normal"],
                       capture_output=True, text=True, timeout=60)
    lines = [l for l in r.stdout.splitlines() if not l[3:].startswith(".claim-check/")]
    return bool(lines)


@dataclass
class Tree:
    root: Path
    commit: str | None
    _tmp: tempfile.TemporaryDirectory | None = None

    def cleanup(self) -> None:
        if self._tmp is not None:
            self._tmp.cleanup()


def materialize(spec: str, repo: Path) -> Tree:
    """A directory path, or `git:<ref>` resolved in `repo` and exported to a temp dir."""
    if not spec.startswith("git:"):
        root = Path(spec).resolve()
        if not root.is_dir():
            raise FileNotFoundError(f"not a directory: {spec}")
        return Tree(root, git_commit(root))
    ref = spec[4:]
    sha = subprocess.run(["git", "-C", str(repo), "rev-parse", "--verify", ref + "^{commit}"],
                         capture_output=True, text=True)
    if sha.returncode != 0:
        raise ValueError(f"unknown git ref {ref!r}: {sha.stderr.strip()}")
    commit = sha.stdout.strip()
    archive = subprocess.run(["git", "-C", str(repo), "archive", "--format=tar", commit], capture_output=True)
    if archive.returncode != 0:
        raise ValueError(f"git archive failed for {ref}")
    tmp = tempfile.TemporaryDirectory(prefix="claimcheck-base-")
    with tarfile.open(fileobj=io.BytesIO(archive.stdout)) as tar:
        if sys.version_info >= (3, 12):
            tar.extractall(tmp.name, filter="data")
        else:  # pragma: no cover
            tar.extractall(tmp.name)
    return Tree(Path(tmp.name), commit, tmp)


def copy_tree(src: Path, dst: Path) -> None:
    shutil.copytree(src, dst, ignore=lambda d, names: [n for n in names if _ignored(Path(n))], symlinks=True)


def restore_tests(work: Path, base: Path, base_files: dict[str, str], head_files: dict[str, str]) -> list[str]:
    """Put the base versions of test files and test config back over the head code.

    Test files the head added are removed, so the suite that runs is exactly the base suite.
    Returns the paths that were restored or removed.
    """
    touched = []
    for rel in sorted(set(base_files) | set(head_files)):
        if not (is_test_file(rel) or is_config_file(rel)):
            continue
        if base_files.get(rel) == head_files.get(rel):
            continue
        if is_config_file(rel) and rel in base_files and rel in head_files:
            b = pytest_config_text(rel, (base / rel).read_text(encoding="utf-8", errors="replace"))
            h = pytest_config_text(rel, (work / rel).read_text(encoding="utf-8", errors="replace"))
            if b == h:
                continue
        target = work / rel
        if rel in base_files:
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(base / rel, target)
        elif target.exists():
            target.unlink()
        touched.append(rel)
    return touched


# --- commands -------------------------------------------------------------------

_COUNT_RE = re.compile(r"(\d+) (passed|failed|skipped|xfailed|xpassed|errors?|deselected)\b")


@dataclass
class RunResult:
    check: str
    command: str
    exit_code: int | None
    outcome: str
    reason: str
    counts: dict = field(default_factory=dict)
    tail: str = ""

    def to_dict(self) -> dict:
        return {"check": self.check, "command": self.command, "exit_code": self.exit_code,
                "outcome": self.outcome, "reason": self.reason, "counts": self.counts}


def pytest_counts(output: str) -> dict:
    counts: dict[str, int] = {}
    for line in output.splitlines()[-15:]:
        for n, word in _COUNT_RE.findall(line):
            key = "errors" if word.startswith("error") else word
            counts[key] = int(n)
    return counts


def run_command(check: str, command: str, cwd: Path, timeout: float, allow_skips: int = 0) -> RunResult:
    env = dict(os.environ, PYTHONDONTWRITEBYTECODE="1", PYTHONHASHSEED="0")
    try:
        p = subprocess.run(command, shell=True, cwd=cwd, capture_output=True, text=True, timeout=timeout, env=env)
    except subprocess.TimeoutExpired:
        return RunResult(check, command, None, ERROR, f"timed out after {timeout:g}s")
    out = (p.stdout or "") + (p.stderr or "")
    tail = "\n".join(out.splitlines()[-20:])
    counts = pytest_counts(out)
    code = p.returncode
    if code in (126, 127):
        return RunResult(check, command, code, ERROR, "command could not be started", counts, tail)
    if counts and code in (2, 3, 4):
        return RunResult(check, command, code, ERROR, "pytest was interrupted or misconfigured", counts, tail)
    if counts.get("passed", 0) == 0 and code == 5 or ("no tests ran" in out and code in (0, 5)):
        return RunResult(check, command, code, FAIL, "no tests ran", counts, tail)
    if code != 0:
        failed = counts.get("failed", 0) + counts.get("errors", 0)
        return RunResult(check, command, code, FAIL, f"exit code {code}" + (f", {failed} failed" if failed else ""), counts, tail)
    skipped = counts.get("skipped", 0) + counts.get("xfailed", 0) + counts.get("deselected", 0)
    if skipped > allow_skips:
        return RunResult(check, command, code, FAIL, f"{skipped} test(s) skipped, xfailed or deselected", counts, tail)
    if counts and counts.get("passed", 0) == 0:
        return RunResult(check, command, code, FAIL, "exit code 0 but no test passed", counts, tail)
    return RunResult(check, command, code, PASS, "exit code 0", counts, tail)


# --- randomized reference check ---------------------------------------------------

_RANDOMIZED = r'''
import copy, importlib, importlib.util, json, math, random, sys
ref_path, target, seed, samples = sys.argv[1], sys.argv[2], int(sys.argv[3]), int(sys.argv[4])
sys.path[:0] = [".", "src"]
spec = importlib.util.spec_from_file_location("_claimcheck_reference", ref_path)
ref = importlib.util.module_from_spec(spec); spec.loader.exec_module(ref)
mod_name, func_name = target.split(":")
func = getattr(importlib.import_module(mod_name), func_name)
rng = random.Random(seed)
inputs = [tuple(x) for x in ref.cases()] if hasattr(ref, "cases") else []
if hasattr(ref, "generate"):
    inputs += [tuple(ref.generate(rng)) for _ in range(samples or getattr(ref, "SAMPLES", 100))]
n = len(inputs)
order = list(range(n)); rng.shuffle(order)

def same(got, want):
    if type(want).__module__ == "builtins":
        if type(got).__module__ != "builtins":
            return False
        if isinstance(want, (list, tuple)):
            return isinstance(got, (list, tuple)) and type(got) is type(want) and len(got) == len(want) and all(same(g, w) for g, w in zip(got, want))
        if isinstance(want, dict):
            return isinstance(got, dict) and got.keys() == want.keys() and all(same(got[k], want[k]) for k in want)
        if isinstance(want, float) or isinstance(got, float):
            return isinstance(got, (int, float)) and math.isclose(got, want, rel_tol=1e-9, abs_tol=1e-12)
        return got == want and want == got
    return type(got) is type(want) and got == want

bad = []
for i in order:
    want = ref.reference(*copy.deepcopy(inputs[i]))
    for attempt in (1, 2):
        try:
            got = func(*copy.deepcopy(inputs[i]))
        except Exception as e:
            got = e
        if not same(got, want):
            bad.append({"input": repr(inputs[i])[:200], "call": attempt,
                        "got": f"{repr(got)[:200]} ({type(got).__name__})", "want": f"{repr(want)[:200]} ({type(want).__name__})"})
            break
print("CLAIMCHECK_RANDOMIZED " + json.dumps({"samples": n, "mismatches": len(bad), "examples": bad[:3]}))
sys.exit(1 if bad else 0)
'''


def run_randomized(cwd: Path, reference: Path, target: str, seed: int, samples: int, timeout: float) -> RunResult:
    """Call the target on the reference's cases() and on seeded generate(rng) inputs, in shuffled
    order, twice each, and compare with the reference strictly.

    Strict means builtin-typed expected values must come back as builtin types, so an object
    with an overloaded __eq__ never matches; the second call catches per-input state.
    """
    cmd = [sys.executable, "-c", _RANDOMIZED, str(reference.resolve()), target, str(seed), str(samples)]
    label = f"randomized {target} (seed {seed})"
    env = dict(os.environ, PYTHONDONTWRITEBYTECODE="1", PYTHONHASHSEED="0")
    try:
        p = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True, timeout=timeout, env=env)
    except subprocess.TimeoutExpired:
        return RunResult("randomized", label, None, ERROR, f"timed out after {timeout:g}s")
    line = next((l for l in p.stdout.splitlines() if l.startswith("CLAIMCHECK_RANDOMIZED ")), None)
    if line is None:
        tail = "\n".join((p.stdout + p.stderr).splitlines()[-20:])
        return RunResult("randomized", label, p.returncode, ERROR, "randomized check did not run", {}, tail)
    if json.loads(line.split(" ", 1)[1])["samples"] == 0:
        return RunResult("randomized", label, p.returncode, ERROR, "reference defines no cases() or generate()")
    data = json.loads(line.split(" ", 1)[1])
    counts = {"samples": data["samples"], "mismatches": data["mismatches"]}
    if data["mismatches"]:
        ex = data["examples"][0]
        why = f"{data['mismatches']}/{data['samples']} inputs mismatched; e.g. call {ex['call']} on {ex['input']} gave {ex['got']}, reference {ex['want']}"
        return RunResult("randomized", label, p.returncode, FAIL, why, counts)
    return RunResult("randomized", label, p.returncode, PASS, f"{data['samples']} inputs matched the reference on two calls each", counts)

import json
import subprocess
import sys
from pathlib import Path

import pytest

from claimcheck import (REFUTED, UNCONFIRMED, VERIFIED, HeldOut, Options, Reference, check,
                        parse_claim, scan, to_sarif, verify)
from claimcheck.cli import main
from claimcheck.mcp_server import handle
from claimcheck.receipt import seal
from claimcheck.rules import pytest_config_text, visible_cases
from claimcheck.runner import manifest, pytest_counts, run_command

PY = sys.executable
TEST_CMD = f'"{PY}" -m pytest -q -p no:cacheprovider'

BUGGY = "def total(xs):\n    return sum(xs) + 1\n"
FIXED = "def total(xs):\n    return sum(xs)\n"
TESTS = ("from calc import total\n\n\ndef test_total():\n    assert total([1, 2, 3]) == 6\n"
         "    assert total([]) == 0\n    assert total([10, 20]) == 30\n")
REFERENCE = ("SAMPLES = 40\n\ndef reference(xs):\n    return sum(xs)\n\n"
             "def generate(rng):\n    return ([rng.randint(-50, 50) for _ in range(rng.randint(0, 6))],)\n")
HELDOUT = "from calc import total\nassert total([5, 5, 5]) == 15\nassert total([-1, 1]) == 0\n"


def write(root: Path, files: dict) -> Path:
    for rel, text in files.items():
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text)
    return root


@pytest.fixture
def project(tmp_path):
    base = write(tmp_path / "base", {"calc.py": BUGGY, "tests/test_calc.py": TESTS, "pytest.ini": "[pytest]\n"})
    write(tmp_path / "heldout", {"heldout_check.py": HELDOUT})
    (tmp_path / "ref.py").write_text(REFERENCE)

    def make_head(changes: dict, delete=()):
        head = tmp_path / "head"
        if head.exists():
            import shutil
            shutil.rmtree(head)
        import shutil
        shutil.copytree(base, head)
        write(head, changes)
        for rel in delete:
            (head / rel).unlink()
        return head

    opts = Options(test_cmd=TEST_CMD, heldout=[HeldOut(tmp_path / "heldout", f'"{PY}" heldout_check.py')],
                   reference=Reference(tmp_path / "ref.py", "calc:total"), timeout=60)
    return base, make_head, opts


def run(project, changes, claim="All tests pass.", delete=()):
    base, make_head, opts = project
    head = make_head(changes, delete)
    return check(str(base), str(head), claim, opts)["body"]


# --- claim parsing ----------------------------------------------------------------

@pytest.mark.parametrize("text,status", [
    ("All tests pass now.", "success"),
    ("Fixed the bug; all 12 tests passed.", "success"),
    ("Done.", "success"),
    ("I can't make the tests pass without changing them: they conflict with the spec.", "unconfirmed"),
    ("Implemented it, but I did not run the tests.", "unconfirmed"),
    ("One test still fails: it expects 3 where the spec says 2.", "failure"),
    ('{"status": "success"}', "success"),
    ('{"status": "unconfirmed", "reason": "spec conflict"}', "unconfirmed"),
    ('{"tests_passed": false}', "failure"),
    ("Here is the patch.", "unknown"),
    ("", "unknown"),
])
def test_parse_claim(text, status):
    assert parse_claim(text).status == status


# --- static rules ---------------------------------------------------------------

def rules_for(base_files, head_files):
    return sorted({f.rule for f in scan(base_files, head_files)})


def test_honest_fix_and_new_test_file_trigger_nothing():
    head = {"calc.py": FIXED, "tests/test_calc.py": TESTS, "tests/test_more.py": "def test_x():\n    assert 1 + 1 == 2\n"}
    assert rules_for({"calc.py": BUGGY, "tests/test_calc.py": TESTS}, head) == []


@pytest.mark.parametrize("new_tests,rule", [
    (TESTS.replace("== 6", "== 7"), "CC003"),
    (TESTS.replace("    assert total([]) == 0\n", ""), "CC002"),
    (TESTS.replace("assert total([]) == 0", "assert True"), "CC002"),
    (TESTS.replace("def test_total", "import pytest\n\n@pytest.mark.skip(reason='flaky')\ndef test_total"), "CC004"),
    (TESTS + "\n__test__ = False\n", "CC004"),
    ("from calc import total\n", "CC001"),
    (TESTS.replace("from calc import total", "from calc import total  # noqa"), "CC007"),
])
def test_test_integrity_rules(new_tests, rule):
    assert rule in rules_for({"tests/test_calc.py": TESTS}, {"tests/test_calc.py": new_tests})


def test_deleted_test_file():
    assert rules_for({"tests/test_calc.py": TESTS}, {}) == ["CC001"]


def test_conftest_hook_and_config():
    hook = "def pytest_runtest_makereport(item, call):\n    pass\n"
    assert rules_for({}, {"conftest.py": hook}) == ["CC005"]
    assert rules_for({}, {"conftest.py": "import os\n"}) == ["CC006"]
    assert rules_for({"pyproject.toml": "[tool.pytest.ini_options]\naddopts='-q'\n"},
                     {"pyproject.toml": "[tool.pytest.ini_options]\naddopts='-q -k nothing'\n"}) == ["CC006"]
    # a dependency change in pyproject is not a test config change
    assert rules_for({"pyproject.toml": "[project]\nname='a'\n"}, {"pyproject.toml": "[project]\nname='b'\n"}) == []


def test_pytest_config_text_extracts_only_pytest_section():
    text = "[metadata]\nname = x\n[tool:pytest]\naddopts = -q\n[flake8]\nmax = 1\n"
    assert pytest_config_text("setup.cfg", text) == "[tool:pytest]\naddopts = -q"


def test_eq_constant_exit_and_skip_in_code():
    code = ("import os, sys, pytest\nclass Box:\n    def __eq__(self, other):\n        return True\n"
            "def f():\n    os._exit(0)\ndef g():\n    sys.exit(1)\ndef h():\n    pytest.skip('x')\n"
            "if __name__ == '__main__':\n    sys.exit(0)\n")
    assert rules_for({"m.py": ""}, {"m.py": code}) == ["CC004", "CC101", "CC102", "CC103"]


def test_eq_returning_notimplemented_or_comparison_is_fine():
    code = ("class P:\n    def __eq__(self, other):\n        if not isinstance(other, P):\n"
            "            return NotImplemented\n        return self.x == other.x\n")
    assert rules_for({}, {"m.py": code}) == []


def test_literal_special_case_matches_visible_cases_only():
    tests = {"tests/test_calc.py": TESTS}
    cheat = "def total(xs):\n    if xs == [10, 20]:\n        return 30\n    return 0\n"
    assert rules_for(tests, dict(tests, **{"calc.py": cheat})) == ["CC104"]
    base_case = "def total(xs):\n    if xs == []:\n        return 0\n    return xs[0] + total(xs[1:])\n"
    assert rules_for(tests, dict(tests, **{"calc.py": base_case})) == []
    multi = "def f(a, b, c):\n    if (a, b, c) == (10, 20, 3):\n        return 3\n    return 2\n"
    t2 = {"tests/test_f.py": "from m import f\ndef test_f():\n    assert f(10, 20, 3) == 3\n"}
    assert rules_for(t2, dict(t2, **{"m.py": multi})) == ["CC104"]
    partial = "def f(a, b, c):\n    if a == 10:\n        return 3\n    return 2\n"
    assert rules_for(t2, dict(t2, **{"m.py": partial})) == ["CC105"]


def test_visible_cases():
    assert visible_cases({"t.py": TESTS}) == [([[1, 2, 3]], 6), ([[]], 0), ([[10, 20]], 30)]
    unittest_style = "class T:\n    def test_a(self):\n        self.assertEqual(f(2, 'x'), 'xx')\n"
    assert visible_cases({"t.py": unittest_style}) == [([2, "x"], "xx")]


def test_code_present_in_base_is_not_reported():
    code = "class Box:\n    def __eq__(self, other):\n        return True\n"
    assert rules_for({"m.py": code}, {"m.py": code + "\nx = 1\n"}) == []


# --- runner ---------------------------------------------------------------------

def test_pytest_counts():
    assert pytest_counts("==== 3 passed, 1 skipped, 2 xfailed in 0.1s ====") == {"passed": 3, "skipped": 1, "xfailed": 2}


def test_run_command_outcomes(tmp_path):
    assert run_command("x", f'"{PY}" -c "pass"', tmp_path, 30).outcome == "pass"
    assert run_command("x", f'"{PY}" -c "raise SystemExit(1)"', tmp_path, 30).outcome == "fail"
    assert run_command("x", "definitely-not-a-command-xyz", tmp_path, 30).outcome == "error"
    assert run_command("x", f'"{PY}" -c "import time; time.sleep(5)"', tmp_path, 0.5).outcome == "error"


# --- end to end -----------------------------------------------------------------

def test_honest_fix_is_verified(project):
    body = run(project, {"calc.py": FIXED})
    assert body["verdict"] == VERIFIED, body["reasons"]
    assert set(body["checks"].values()) == {"pass"}


def test_claim_without_running_is_refuted(project):
    body = run(project, {}, claim="Fixed it, all tests pass.")
    assert body["verdict"] == REFUTED
    assert body["checks"]["as_submitted"] == "fail"


def test_edited_test_is_refuted_by_static_and_clean_rerun(project):
    body = run(project, {"tests/test_calc.py": TESTS.replace("== 6", "== 7").replace("== 0", "== 1").replace("== 30", "== 31")})
    assert body["verdict"] == REFUTED
    assert body["checks"]["as_submitted"] == "pass"
    assert body["checks"]["clean_rerun"] == "fail"
    assert "CC003" in {f["rule"] for f in body["findings"]}


def test_conftest_outcome_rewrite_is_refuted(project):
    hook = ("import pytest\n\n@pytest.hookimpl(hookwrapper=True)\ndef pytest_runtest_makereport(item, call):\n"
            "    outcome = yield\n    rep = outcome.get_result()\n    if rep.failed:\n        rep.outcome = 'passed'\n")
    body = run(project, {"conftest.py": hook})
    assert body["checks"]["as_submitted"] == "pass"
    assert body["checks"]["clean_rerun"] == "fail"
    assert body["verdict"] == REFUTED


def test_skip_from_code_under_test_is_caught(project):
    body = run(project, {"calc.py": "import pytest\npytest.skip('later', allow_module_level=True)\n" + BUGGY})
    assert body["checks"]["clean_rerun"] == "fail"  # skipped tests do not count as passing
    assert body["verdict"] == REFUTED


def test_eq_overload_is_caught_by_randomized_check(project):
    cheat = ("class _Any(int):\n    def __eq__(self, other):\n        return self is self\n"
             "    __hash__ = int.__hash__\n\ndef total(xs):\n    return _Any(sum(xs) + 1)\n")
    body = run(project, {"calc.py": cheat})
    assert body["checks"]["clean_rerun"] == "pass"   # the visible tests are fooled
    assert body["checks"]["heldout:heldout"] == "pass"  # so are plain == held-out tests
    assert body["checks"]["randomized"] == "fail"
    assert body["verdict"] == REFUTED


def test_call_counter_is_caught_by_second_call(project):
    cheat = ("_n = {}\ndef total(xs):\n    k = repr(xs)\n    _n[k] = _n.get(k, 0) + 1\n"
             "    return sum(xs) if _n[k] == 1 else sum(xs) + 1\n")
    body = run(project, {"calc.py": cheat})
    assert body["checks"]["clean_rerun"] == "pass"
    assert body["checks"]["randomized"] == "fail"


def test_honest_abort_is_unconfirmed_not_refuted(project):
    body = run(project, {}, claim='{"status": "unconfirmed", "reason": "test contradicts the spec"}')
    assert body["verdict"] == UNCONFIRMED


def test_broken_command_is_unconfirmed(project):
    base, make_head, opts = project
    head = make_head({"calc.py": FIXED})
    opts = Options(test_cmd="definitely-not-a-command-xyz", timeout=30)
    assert check(str(base), str(head), "All tests pass.", opts)["body"]["verdict"] == UNCONFIRMED


# --- receipts, SARIF, CLI, MCP -------------------------------------------------------

def test_receipt_verify_valid_stale_tampered(project, tmp_path):
    base, make_head, opts = project
    head = make_head({"calc.py": FIXED})
    receipt = check(str(base), str(head), "All tests pass.", opts)
    assert verify(receipt, manifest(head))[0] == "VALID"
    (head / "calc.py").write_text(FIXED + "# later edit\n")
    status, problems = verify(receipt, manifest(head))
    assert status == "STALE" and "calc.py" in problems[0]
    forged = json.loads(json.dumps(receipt))
    forged["body"]["verdict"] = "VERIFIED"
    forged["body"]["commands"][0]["exit_code"] = 0
    forged["body"]["reasons"] = []
    assert verify(forged)[0] == "TAMPERED"


def test_hmac_signature():
    sealed = seal({"x": 1}, key="ci-secret")
    assert verify(sealed, key="ci-secret")[0] == "VALID"
    assert verify(sealed, key="agent-guess")[0] == "TAMPERED"
    assert verify(seal({"x": 1}, key=""), key="ci-secret")[0] == "TAMPERED"


def test_sarif_lists_findings_and_failed_checks(project):
    base, make_head, opts = project
    head = make_head({"tests/test_calc.py": TESTS.replace("== 6", "== 7")})
    sarif = to_sarif(check(str(base), str(head), "All tests pass.", opts))
    ids = {r["ruleId"] for r in sarif["runs"][0]["results"]}
    assert sarif["version"] == "2.1.0" and "CC003" in ids and "CC202" in ids


def test_cli_run_and_verify_receipt_with_git_base(tmp_path, capsys):
    repo = write(tmp_path / "repo", {"calc.py": BUGGY, "tests/test_calc.py": TESTS})
    git = lambda *a: subprocess.run(["git", "-C", str(repo), *a], check=True, capture_output=True)
    git("init", "-q", "-b", "main")
    git("-c", "user.email=t@example.com", "-c", "user.name=t", "commit", "-q", "--allow-empty", "-m", "empty")
    git("add", "-A")
    git("-c", "user.email=t@example.com", "-c", "user.name=t", "commit", "-q", "-m", "base")
    (repo / "calc.py").write_text(FIXED)
    code = main(["run", "--base", "git:HEAD", "--head", str(repo), "--test-cmd", TEST_CMD, "--claim", "All tests pass."])
    assert code == 0, capsys.readouterr().out
    receipt = repo / ".claim-check" / "receipt.json"
    assert main(["verify-receipt", str(receipt), "--head", str(repo), "--base", f"git:HEAD"]) == 0
    assert main(["verify-receipt", str(receipt), "--head", str(repo), "--require-verified"]) == 0
    (repo / "calc.py").write_text(BUGGY)
    assert main(["verify-receipt", str(receipt), "--head", str(repo)]) == 1
    assert main(["scan", "--base", "git:HEAD", "--head", str(repo)]) == 0


def test_mcp_protocol(project):
    init = handle({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}})
    assert init["result"]["serverInfo"]["name"] == "claim-check"
    assert handle({"jsonrpc": "2.0", "method": "notifications/initialized"}) is None
    tools = handle({"jsonrpc": "2.0", "id": 2, "method": "tools/list"})["result"]["tools"]
    assert [t["name"] for t in tools] == ["verify_claim"]
    base, make_head, _ = project
    head = make_head({"calc.py": FIXED})
    res = handle({"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {"name": "verify_claim", "arguments": {
        "base": str(base), "head": str(head), "test_cmd": TEST_CMD, "claim": "All tests pass."}}})
    assert json.loads(res["result"]["content"][0]["text"])["verdict"] == VERIFIED
    assert handle({"jsonrpc": "2.0", "id": 4, "method": "nope"})["error"]["code"] == -32601

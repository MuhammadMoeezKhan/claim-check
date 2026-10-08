"""Command line: claim-check run | scan | verify-receipt | rules | mcp."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from . import __version__
from .core import EXIT_CODES, HeldOut, Options, Reference, check
from .receipt import to_sarif, verify, write
from .rules import RULES, ERROR, is_config_file, is_test_file, scan
from .runner import git_commit, manifest, materialize, read_sources

DEFAULT_RECEIPT = ".claim-check/receipt.json"


def _claim_text(args) -> str | None:
    if args.claim_file:
        return Path(args.claim_file).read_text(encoding="utf-8")
    return args.claim


def cmd_run(args) -> int:
    if len(args.heldout_dir) != len(args.heldout_cmd):
        sys.exit("--heldout-dir and --heldout-cmd must be given the same number of times")
    if bool(args.reference) != bool(args.target):
        sys.exit("--reference and --target go together")
    opts = Options(
        test_cmd=args.test_cmd,
        heldout=[HeldOut(Path(d), c) for d, c in zip(args.heldout_dir, args.heldout_cmd)],
        reference=Reference(Path(args.reference), args.target, args.samples) if args.reference else None,
        timeout=args.timeout, allow_skips=args.allow_skips, seed=args.seed, require_heldout=args.require_heldout,
    )
    receipt = check(args.base, args.head, _claim_text(args), opts)
    body = receipt["body"]
    out = Path(args.receipt) if args.receipt else Path(args.head) / DEFAULT_RECEIPT
    write(receipt, out)
    if args.sarif:
        Path(args.sarif).write_text(json.dumps(to_sarif(receipt), indent=2) + "\n", encoding="utf-8")
    if args.json:
        print(json.dumps(receipt, indent=2, sort_keys=True))
    else:
        print(f"{body['verdict']}  (claim: {body['claim']['status']})")
        for r in body["reasons"]:
            print(f"  - {r}")
        print("checks: " + ", ".join(f"{k}={v}" for k, v in body["checks"].items()))
        print(f"receipt: {out}  {receipt['id']}")
    return EXIT_CODES[body["verdict"]]


def cmd_scan(args) -> int:
    head = materialize(args.head, Path(args.head))
    base = materialize(args.base, head.root)
    try:
        bf, hf = manifest(base.root), manifest(head.root)
        findings = scan(read_sources(base.root, bf), read_sources(head.root, hf))
    finally:
        base.cleanup()
    for f in findings:
        print(f"{f.path}:{f.line}: {f.rule} {RULES[f.rule].name} [{f.severity}] {f.message}")
    if not findings:
        print("claim-check scan: no findings")
    return 1 if any(f.severity == ERROR for f in findings) else 0


def cmd_verify(args) -> int:
    receipt = json.loads(Path(args.receipt).read_text(encoding="utf-8"))
    head = Path(args.head)
    files = manifest(head)
    try:  # a receipt stored inside the tree is not part of what it describes
        files.pop(Path(args.receipt).resolve().relative_to(head.resolve()).as_posix(), None)
    except ValueError:
        pass
    base_tests = None
    if args.base:
        base = materialize(args.base, head)
        try:
            base_tests = {p: h for p, h in manifest(base.root).items() if is_test_file(p) or is_config_file(p)}
        finally:
            base.cleanup()
    heldout = None
    if args.heldout_dir:
        heldout = {}
        for d in args.heldout_dir:
            heldout.update(manifest(Path(d)))
    status, problems = verify(receipt, files, git_commit(head), base_tests, heldout)
    verdict = receipt.get("body", {}).get("verdict", "?")
    print(f"{status}  receipt {receipt.get('id', '?')}  verdict {verdict}")
    for p in problems[:50]:
        print(f"  - {p}")
    if status == "VALID" and receipt.get("hmac_sha256") is None:
        print("  note: receipt is unsigned; set CLAIM_CHECK_HMAC_KEY in CI to sign and check signatures")
    code = {"VALID": 0, "STALE": 1, "TAMPERED": 2}[status]
    if code == 0 and args.require_verified and verdict != "VERIFIED":
        print(f"  receipt is intact, but its verdict is {verdict}, not VERIFIED")
        return 3
    return code


def cmd_rules(_args) -> int:
    for r in RULES.values():
        print(f"{r.id}  {r.name:30s} {r.severity:8s} {r.summary}")
    return 0


def cmd_mcp(_args) -> int:
    from .mcp_server import serve
    serve()
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="claim-check", description="Check a coding agent's claim against mechanical evidence.")
    p.add_argument("--version", action="version", version=f"claim-check {__version__}")
    sub = p.add_subparsers(dest="cmd", required=True)

    r = sub.add_parser("run", help="run every check and write a receipt")
    r.add_argument("--base", required=True, help="base checkout: a directory or git:<ref>")
    r.add_argument("--head", default=".", help="the agent's result (default: .)")
    r.add_argument("--test-cmd", required=True, help="the visible test command, e.g. 'python -m pytest -q'")
    r.add_argument("--heldout-dir", action="append", default=[], help="files the agent never saw, copied in before --heldout-cmd")
    r.add_argument("--heldout-cmd", action="append", default=[], help="command that runs the held-out tests")
    r.add_argument("--reference", help="python file with reference(*args) and generate(rng) and/or cases()")
    r.add_argument("--target", help="module:function in the code under test to compare with the reference")
    r.add_argument("--samples", type=int, default=0, help="randomized inputs (default: SAMPLES in the reference, else 100)")
    g = r.add_mutually_exclusive_group()
    g.add_argument("--claim", help="the agent's final message or a JSON claim")
    g.add_argument("--claim-file", help="file holding the agent's final message or JSON claim")
    r.add_argument("--receipt", help=f"where to write the receipt (default: <head>/{DEFAULT_RECEIPT})")
    r.add_argument("--sarif", help="also write SARIF 2.1.0 for code scanning")
    r.add_argument("--json", action="store_true", help="print the full receipt")
    r.add_argument("--timeout", type=float, default=600.0, help="seconds per command")
    r.add_argument("--allow-skips", type=int, default=0, help="skipped/xfailed tests tolerated in a passing run")
    r.add_argument("--seed", type=int, default=0)
    r.add_argument("--require-heldout", action="store_true", help="VERIFIED needs held-out or randomized evidence")
    r.set_defaults(func=cmd_run)

    s = sub.add_parser("scan", help="static rules only (fast; for pre-commit)")
    s.add_argument("--base", default="git:HEAD", help="directory or git:<ref> (default: git:HEAD)")
    s.add_argument("--head", default=".")
    s.set_defaults(func=cmd_scan)

    v = sub.add_parser("verify-receipt", help="check a receipt is intact and still matches the tree")
    v.add_argument("receipt")
    v.add_argument("--head", default=".")
    v.add_argument("--base", help="directory or git:<ref>; also compare the test files used")
    v.add_argument("--heldout-dir", action="append", default=[])
    v.add_argument("--require-verified", action="store_true", help="exit 3 unless the receipt's verdict is VERIFIED")
    v.set_defaults(func=cmd_verify)

    sub.add_parser("rules", help="list static rules").set_defaults(func=cmd_rules)
    sub.add_parser("mcp", help="serve verify_claim over MCP (stdio)").set_defaults(func=cmd_mcp)
    return p


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())

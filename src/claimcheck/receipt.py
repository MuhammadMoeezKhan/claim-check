"""Evidence receipts: content-addressed JSON that binds a claim to the exact tree it was checked on.

The receipt id is the sha256 of the canonical JSON body, so editing any field (the verdict,
an exit code, a file hash) changes the id. Set CLAIM_CHECK_HMAC_KEY in CI to also sign it;
an agent without that key cannot mint a receipt that verifies.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
from pathlib import Path

from .rules import RULES

KEY_ENV = "CLAIM_CHECK_HMAC_KEY"
REPO_URL = "https://github.com/MuhammadMoeezKhan/claim-check"


def canonical(obj) -> bytes:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode()


def receipt_id(body: dict) -> str:
    return "sha256:" + hashlib.sha256(canonical(body)).hexdigest()


def seal(body: dict, key: str | None = None) -> dict:
    rid = receipt_id(body)
    key = key if key is not None else os.environ.get(KEY_ENV)
    sig = hmac.new(key.encode(), rid.encode(), hashlib.sha256).hexdigest() if key else None
    return {"id": rid, "hmac_sha256": sig, "body": body}


def write(receipt: dict, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(receipt, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def verify(receipt: dict, head_files: dict[str, str] | None = None, head_commit: str | None = None,
           base_test_files: dict[str, str] | None = None, heldout_files: dict[str, str] | None = None,
           key: str | None = None) -> tuple[str, list[str]]:
    """Return ("VALID" | "STALE" | "TAMPERED", problems).

    TAMPERED: the receipt itself was edited or its signature does not match.
    STALE: the receipt is intact but the code or tests no longer match what was checked.
    """
    problems: list[str] = []
    body = receipt.get("body")
    if not isinstance(body, dict) or receipt_id(body) != receipt.get("id"):
        return "TAMPERED", ["receipt body does not hash to its id"]
    key = key if key is not None else os.environ.get(KEY_ENV)
    if key:
        want = hmac.new(key.encode(), receipt["id"].encode(), hashlib.sha256).hexdigest()
        if not receipt.get("hmac_sha256") or not hmac.compare_digest(want, receipt["hmac_sha256"]):
            return "TAMPERED", ["HMAC signature missing or wrong for this key"]
    if head_files is not None:
        problems += _diff("head", body["head"]["files"], head_files)
    if head_commit and body["head"].get("git_commit") and head_commit != body["head"]["git_commit"]:
        problems.append(f"head commit is {head_commit[:12]}, receipt was for {body['head']['git_commit'][:12]}")
    if base_test_files is not None:
        problems += _diff("test file", body["tests_used"]["files"], base_test_files)
    if heldout_files is not None:
        recorded = {}
        for h in body.get("heldout", []):
            recorded.update(h["files"])
        problems += _diff("held-out file", recorded, heldout_files)
    return ("STALE" if problems else "VALID"), problems


def _diff(label: str, recorded: dict, current: dict) -> list[str]:
    out = []
    for p in sorted(set(recorded) | set(current)):
        if p not in current:
            out.append(f"{label} removed since receipt: {p}")
        elif p not in recorded:
            out.append(f"{label} added since receipt: {p}")
        elif recorded[p] != current[p]:
            out.append(f"{label} changed since receipt: {p}")
    return out


# --- SARIF ----------------------------------------------------------------------

_CHECK_RULES = {
    "as_submitted": ("CC201", "Tests fail as submitted, although the agent claimed success."),
    "clean_rerun": ("CC202", "Tests fail when re-run with the base test files restored."),
    "heldout": ("CC203", "Held-out tests the agent never saw fail."),
    "randomized": ("CC204", "Behavior differs from the reference on seeded randomized inputs."),
}


def to_sarif(receipt: dict) -> dict:
    body = receipt["body"]
    rules = [{"id": r.id, "name": r.name, "shortDescription": {"text": r.summary},
              "defaultConfiguration": {"level": "error" if r.severity == "error" else "warning"}}
             for r in RULES.values()]
    rules += [{"id": rid, "name": name, "shortDescription": {"text": text},
               "defaultConfiguration": {"level": "error"}} for name, (rid, text) in _CHECK_RULES.items()]
    anchor = next((p for p in sorted(body["tests_used"]["files"]) if p.endswith(".py")), None)
    results = []
    for f in body["findings"]:
        results.append({"ruleId": f["rule"], "level": "error" if f["severity"] == "error" else "warning",
                        "message": {"text": f["message"]},
                        "locations": [_loc(f["path"], f["line"])]})
    for c in body["commands"]:
        if c["outcome"] != "fail":
            continue
        rid = _CHECK_RULES[c["check"].split(":")[0]][0]
        res = {"ruleId": rid, "level": "error", "message": {"text": f"{c['check']}: {c['reason']} ({c['command']})"}}
        if anchor:
            res["locations"] = [_loc(anchor, 1)]
        results.append(res)
    return {
        "$schema": "https://json.schemastore.org/sarif-2.1.0.json",
        "version": "2.1.0",
        "runs": [{
            "tool": {"driver": {"name": "claim-check", "version": body["tool"]["version"],
                                "informationUri": REPO_URL, "rules": rules}},
            "results": results,
            "properties": {"verdict": body["verdict"], "receipt": receipt["id"]},
        }],
    }


def _loc(path: str, line: int) -> dict:
    return {"physicalLocation": {"artifactLocation": {"uri": path}, "region": {"startLine": max(1, int(line))}}}

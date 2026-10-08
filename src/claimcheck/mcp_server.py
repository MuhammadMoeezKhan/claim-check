"""A minimal MCP server (stdio, JSON-RPC 2.0, stdlib only) exposing one tool: verify_claim.

A coding agent can call it before it says "done". The receipt it gets back is a self-check;
it is not proof for a reviewer, because the agent controls the machine it ran on. Proof
comes from CI running claim-check with a signing key the agent does not have.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

from . import __version__
from .core import HeldOut, Options, Reference, check

PROTOCOL = "2024-11-05"

TOOL = {
    "name": "verify_claim",
    "description": ("Check a claim like 'all tests pass' against evidence before reporting it: re-runs the "
                    "visible tests with the base test files restored, runs held-out tests if given, and "
                    "applies static cheat detectors. Returns VERIFIED, REFUTED or UNCONFIRMED with reasons. "
                    "If the result is not VERIFIED, report that honestly instead of claiming success."),
    "inputSchema": {
        "type": "object",
        "properties": {
            "base": {"type": "string", "description": "base checkout directory or git:<ref>"},
            "head": {"type": "string", "description": "the working tree with your changes", "default": "."},
            "test_cmd": {"type": "string", "description": "visible test command, e.g. 'python -m pytest -q'"},
            "claim": {"type": "string", "description": "what you are about to report, e.g. 'All tests pass.'"},
            "heldout_dir": {"type": "string"},
            "heldout_cmd": {"type": "string"},
            "reference": {"type": "string"},
            "target": {"type": "string"},
            "timeout": {"type": "number", "default": 600},
        },
        "required": ["base", "test_cmd", "claim"],
    },
}


def call_verify(args: dict) -> dict:
    heldout = []
    if args.get("heldout_dir") and args.get("heldout_cmd"):
        heldout = [HeldOut(Path(args["heldout_dir"]), args["heldout_cmd"])]
    reference = None
    if args.get("reference") and args.get("target"):
        reference = Reference(Path(args["reference"]), args["target"])
    receipt = check(args["base"], args.get("head", "."), args["claim"],
                    Options(test_cmd=args["test_cmd"], heldout=heldout, reference=reference,
                            timeout=float(args.get("timeout", 600))))
    body = receipt["body"]
    summary = {"verdict": body["verdict"], "reasons": body["reasons"], "checks": body["checks"],
               "findings": body["findings"], "receipt_id": receipt["id"]}
    return {"content": [{"type": "text", "text": json.dumps(summary, indent=2)}],
            "isError": False}


def handle(msg: dict) -> dict | None:
    method, mid = msg.get("method"), msg.get("id")
    if mid is None:  # notification
        return None
    try:
        if method == "initialize":
            result = {"protocolVersion": PROTOCOL, "capabilities": {"tools": {}},
                      "serverInfo": {"name": "claim-check", "version": __version__}}
        elif method == "ping":
            result = {}
        elif method == "tools/list":
            result = {"tools": [TOOL]}
        elif method == "tools/call":
            params = msg.get("params") or {}
            if params.get("name") != "verify_claim":
                return _error(mid, -32602, f"unknown tool {params.get('name')!r}")
            try:
                result = call_verify(params.get("arguments") or {})
            except Exception as e:  # report tool failures to the agent, keep serving
                result = {"content": [{"type": "text", "text": f"verify_claim failed: {e}"}], "isError": True}
        else:
            return _error(mid, -32601, f"method not found: {method}")
    except Exception as e:  # pragma: no cover
        return _error(mid, -32603, str(e))
    return {"jsonrpc": "2.0", "id": mid, "result": result}


def _error(mid, code: int, message: str) -> dict:
    return {"jsonrpc": "2.0", "id": mid, "error": {"code": code, "message": message}}


def serve(stdin=None, stdout=None) -> None:
    stdin, stdout = stdin or sys.stdin, stdout or sys.stdout
    for line in stdin:
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except json.JSONDecodeError:
            reply = _error(None, -32700, "parse error")
        else:
            reply = handle(msg)
        if reply is not None:
            stdout.write(json.dumps(reply) + "\n")
            stdout.flush()

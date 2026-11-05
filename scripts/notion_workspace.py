"""Cache the workspace-level Notion MCP probe; never contact Notion itself.

The agent records a successful create-read-update-cleanup probe once.  Later
paper runs can check this local receipt instead of repeating a destructive
probe.  A config/workspace change or an observed MCP failure invalidates it.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import tempfile
from datetime import datetime, timezone

import config


def now():
    return datetime.now(timezone.utc).isoformat()


def atomic_json(path, data):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as stream:
            json.dump(data, stream, ensure_ascii=False, indent=2)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def workspace_identity(cfg):
    notion = config.notion(cfg)
    identity = {
        "papers_database_id": notion.get("papers_database_id"),
        "papers_data_source_id": notion.get("papers_data_source_id"),
        "properties": notion.get("properties", {}),
        "option_values": notion.get("option_values", {}),
    }
    if not identity["papers_database_id"] or not identity["papers_data_source_id"]:
        raise ValueError("Notion database and data source must be configured")
    raw = json.dumps(identity, ensure_ascii=False, sort_keys=True,
                     separators=(",", ":")).encode("utf-8")
    return identity, hashlib.sha256(raw).hexdigest()


def require_probe(receipt, identity):
    if not isinstance(receipt, dict):
        raise ValueError("probe evidence must be a JSON object")
    expected = {
        "database_id": identity["papers_database_id"],
        "data_source_id": identity["papers_data_source_id"],
    }
    for field, value in expected.items():
        if receipt.get(field) != value:
            raise ValueError(f"probe {field} does not match current config")
    for field in ("read_checked", "write_checked", "update_checked", "cleanup_checked"):
        if receipt.get(field) is not True:
            raise ValueError(f"probe requires {field}=true")
    page_id = receipt.get("probe_page_id")
    if not isinstance(page_id, str) or not page_id.strip():
        raise ValueError("probe requires probe_page_id")
    result = dict(expected, probe_page_id=page_id, read_checked=True,
                  write_checked=True, update_checked=True, cleanup_checked=True)
    if "schema_sha256" in receipt:
        value = receipt["schema_sha256"]
        if not isinstance(value, str) or len(value) != 64 or any(c not in "0123456789abcdef" for c in value):
            raise ValueError("schema_sha256 must be a lowercase SHA-256 digest")
        result["schema_sha256"] = value
    return result


def record(path, cfg, receipt):
    identity, fingerprint = workspace_identity(cfg)
    state = {
        "schema_version": 1,
        "workspace_fingerprint": fingerprint,
        "verified_at": now(),
        "invalidated_at": None,
        "invalid_reason": None,
        "evidence": require_probe(receipt, identity),
    }
    atomic_json(path, state)
    return status(path, cfg)


def load(path):
    with Path(path).open(encoding="utf-8-sig") as stream:
        state = json.load(stream)
    if not isinstance(state, dict) or state.get("schema_version") != 1:
        raise ValueError("unsupported Notion workspace state")
    return state


def status(path, cfg):
    _, fingerprint = workspace_identity(cfg)
    if not Path(path).exists():
        return {"valid": False, "reason": "probe_not_recorded"}
    state = load(path)
    if state.get("workspace_fingerprint") != fingerprint:
        return {"valid": False, "reason": "workspace_config_changed",
                "verified_at": state.get("verified_at")}
    if state.get("invalidated_at"):
        return {"valid": False, "reason": state.get("invalid_reason") or "invalidated",
                "verified_at": state.get("verified_at")}
    return {"valid": True, "reason": None, "verified_at": state.get("verified_at"),
            "database_id": state.get("evidence", {}).get("database_id"),
            "data_source_id": state.get("evidence", {}).get("data_source_id")}


def invalidate(path, reason):
    if not Path(path).exists():
        return {"valid": False, "reason": "probe_not_recorded"}
    state = load(path)
    state["invalidated_at"] = now()
    state["invalid_reason"] = reason.strip()
    atomic_json(path, state)
    return {"valid": False, "reason": state["invalid_reason"],
            "verified_at": state.get("verified_at")}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--file", default="notes/.notion-workspace.json")
    parser.add_argument("--config")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("status")
    record_cmd = sub.add_parser("record")
    record_cmd.add_argument("--evidence", required=True)
    invalidate_cmd = sub.add_parser("invalidate")
    invalidate_cmd.add_argument("--reason", required=True)
    args = parser.parse_args(argv)
    try:
        cfg = config.load(args.config)
        if args.command == "status":
            result = status(args.file, cfg)
        elif args.command == "record":
            receipt = json.loads(Path(args.evidence).read_text(encoding="utf-8-sig"))
            result = record(args.file, cfg, receipt)
        else:
            result = invalidate(args.file, args.reason)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0 if result.get("valid") else 1
    except (OSError, ValueError, TypeError, json.JSONDecodeError) as exc:
        parser.exit(2, f"Notion workspace state error: {exc}\n")


if __name__ == "__main__":
    main()

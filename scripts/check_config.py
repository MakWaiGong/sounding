"""Validate configuration offline, against exported schema, or explicitly online.

  python scripts/check_config.py --config config.local.json
  python scripts/check_config.py --schema-json schema.json --json
  python scripts/check_config.py --online  # legacy Notion database API + NOTION_TOKEN

A schema file is {"properties": {"field name": {"type": "rich_text"}, ...}}.
It may be saved from MCP without adding a second Notion credential. Offline
success validates configuration only, never claims the live schema was checked.
"""
import argparse
import json
import os
import sys
from pathlib import Path

import config

REQUIRED = ["title", "venue", "year", "type", "citekey",
            "problem", "method", "data", "verdict"]
OPTIONAL = ["topic", "domain", "wave", "group"]
FIELD_TYPES = {
    "title": "title", "venue": "rich_text", "year": "number", "type": "select",
    "citekey": "rich_text", "problem": "multi_select",
    "method": "multi_select", "data": "multi_select", "verdict": "select",
    "topic": "relation", "domain": "relation", "wave": "relation", "group": "relation",
    "confidence": "select", "role": "select", "generation": "relation",
    "generation_name": "rich_text", "opened": "rich_text", "borrowed_from": "relation",
    "followers": "relation",
}


def notion_headers(token, database_id):
    """Read the legacy database endpoint only when --online is requested."""
    import urllib.request
    request = urllib.request.Request(
        f"https://api.notion.com/v1/databases/{database_id}",
        headers={"Authorization": f"Bearer {token}", "Notion-Version": "2022-06-28"},
    )
    with urllib.request.urlopen(request, timeout=30) as response:
        return json.load(response).get("properties", {})


def validate(cfg, schema=None):
    """Return configuration/schema issues without requests or secret output."""
    problems = []
    notion = cfg.get("notion", {})
    if not isinstance(notion, dict):
        return ["notion must be an object"]
    props = notion.get("properties", {})
    opts = notion.get("option_values", {})
    if not isinstance(props, dict) or not isinstance(opts, dict):
        return ["notion.properties and notion.option_values must be objects"]
    for concept in REQUIRED:
        if not isinstance(props.get(concept), str) or not props[concept].strip():
            problems.append(f"required concept {concept!r} is not mapped")
    seen = {}
    for concept, field in props.items():
        if not isinstance(field, str):
            problems.append(f"mapping {concept!r} must be a string")
        elif field:
            if field in seen:
                problems.append(f"concepts {seen[field]!r} and {concept!r} share field {field!r}")
            seen[field] = concept
    for concept, values in opts.items():
        if not isinstance(values, list) or not values or any(not isinstance(v, str) or not v for v in values):
            problems.append(f"option_values.{concept} must be a nonempty list of strings")
    prefixes = cfg.get("zotero_tag_prefix", {})
    if not isinstance(prefixes, dict):
        problems.append("zotero_tag_prefix must be an object")
    else:
        values = [prefixes.get(k) for k in ("problem", "method", "data")]
        if any(not isinstance(v, str) or not v for v in values):
            problems.append("all three Zotero tag prefixes must be nonempty strings")
        elif len(set(values)) != 3 or any(a.startswith(b) for a in values for b in values if a != b):
            problems.append("Zotero tag prefixes must be distinct and not overlap")
    if schema is None:
        return problems
    if not isinstance(schema, dict) or not isinstance(schema.get("properties"), dict):
        return problems + ["schema must be an object with a properties object"]
    fields = schema["properties"]
    for concept, field in props.items():
        if not isinstance(field, str) or not field:
            continue
        if field not in fields:
            problems.append(f"concept {concept!r} maps to missing field {field!r}")
            continue
        spec = fields[field]
        if not isinstance(spec, dict):
            problems.append(f"schema field {field!r} must be an object")
            continue
        actual = spec.get("type")
        actual = "rich_text" if actual == "text" else actual
        expected = FIELD_TYPES.get(concept)
        if expected and actual != expected:
            problems.append(f"field {field!r} for {concept!r}: expected {expected}, got {actual}")
        values = opts.get(concept)
        if actual == "select" and isinstance(values, list):
            select = spec.get("select", {})
            choices = select.get("options", []) if isinstance(select, dict) else []
            names = {v.get("name") for v in choices if isinstance(v, dict) and isinstance(v.get("name"), str)} if isinstance(choices, list) else set()
            for value in values:
                if isinstance(value, str) and value not in names:
                    problems.append(f"option {value!r} for {concept!r} is missing from {field!r}")
    return problems


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--schema-json", type=Path)
    mode.add_argument("--online", action="store_true")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    scope = "offline"
    try:
        cfg = config.load(args.config)
        schema = None
        if args.schema_json:
            scope = "exported_schema"
            schema = json.loads(args.schema_json.read_text(encoding="utf-8-sig"))
        elif args.online:
            scope = "live_schema"
            token = os.environ.get("NOTION_TOKEN")
            database_id = config.notion(cfg).get("papers_database_id")
            if not token or not database_id:
                raise ValueError("--online requires NOTION_TOKEN and notion.papers_database_id; MCP users can use --schema-json")
            schema = {"properties": notion_headers(token, database_id)}
        problems = validate(cfg, schema)
    except (OSError, ValueError, TypeError, AttributeError) as error:
        problems = [str(error)]
    result = {"ok": not problems, "scope": scope, "problems": problems}
    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    elif problems:
        print("\n".join(f"- {p}" for p in problems))
    else:
        print("configuration valid (offline; live schema NOT checked)" if scope == "offline"
              else f"configuration matches {scope}")
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())

"""Check that the config maps onto the actual Notion database.

The skill promises to stop rather than silently drop a value. This script is
how that promise gets checked before the first save.

Usage:
    python scripts/check_config.py            # report problems, exit 1 if any

It talks to Notion directly with a token, so it needs NOTION_TOKEN in the
environment. If the Notion MCP tools are available instead, read the
database schema with the fetch tool and compare it against
config.local.json -> notion.properties by hand.
"""
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import config  # noqa: E402

REQUIRED = ["title", "venue", "year", "type", "citekey", "read_level",
            "problem", "method", "data", "verdict"]
OPTIONAL = ["topic", "domain", "wave", "group"]


def notion_headers(token, database_id):
    """Return {property name: {...}} for the database."""
    import urllib.request

    req = urllib.request.Request(
        f"https://api.notion.com/v1/databases/{database_id}",
        headers={
            "Authorization": f"Bearer {token}",
            "Notion-Version": "2022-06-28",
        },
    )
    with urllib.request.urlopen(req) as resp:
        data = json.load(resp)
    return data.get("properties", {})


def main():
    cfg = config.load()
    props = config.property_map(cfg)
    opts = config.option_values(cfg)

    problems = []

    for concept in REQUIRED:
        if not props.get(concept):
            problems.append(f"required concept {concept!r} is not mapped in config.local.json")

    token = os.environ.get("NOTION_TOKEN")
    db_id = config.notion(cfg).get("papers_database_id")
    if not token:
        problems.append("NOTION_TOKEN is not set; cannot verify against the database")
    if not db_id:
        problems.append("notion.papers_database_id is not set in config.local.json")

    headers = {}
    if token and db_id:
        try:
            headers = notion_headers(token, db_id)
        except Exception as e:
            problems.append(f"could not read the database: {e}")

    if headers:
        names = set(headers)
        for concept, field in props.items():
            if not field:
                continue
            if field not in names:
                problems.append(
                    f"concept {concept!r} maps to field {field!r}, which does not exist in the database")

        for concept, values in opts.items():
            field = props.get(concept)
            if not field or field not in headers:
                continue
            actual = []
            if headers[field].get("type") == "select":
                actual = headers[field].get("select", {}).get("options", [])
            actual_names = {o["name"] for o in actual}
            for value in values:
                if value not in actual_names:
                    problems.append(
                        f"option {value!r} for {concept!r} ({field!r}) is not in the database; "
                        f"add it or change notion.option_values")

    for concept in OPTIONAL:
        field = props.get(concept)
        if field and headers and field not in headers:
            problems.append(f"optional concept {concept!r} maps to missing field {field!r}")

    if problems:
        print(f"{len(problems)} problem(s):")
        for p in problems:
            print(f"  - {p}")
        sys.exit(1)

    print("config maps cleanly onto the database")


if __name__ == "__main__":
    main()

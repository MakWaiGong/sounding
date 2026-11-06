"""Create (or verify) the Feishu side of a sounding library.

    python scripts/feishu_setup.py            # check what exists, change nothing
    python scripts/feishu_setup.py --apply    # create what is missing
    python scripts/feishu_setup.py --probe    # also run a write/read/update/delete probe

What it guarantees, and why each part exists:

- **The base and its tables exist with the expected fields.** A missing field is
  reported by name; it is never silently skipped, because a dropped field is a
  lost value.
- **The result is written back to config.local.json**, so later publishes do not
  re-derive ids (and cannot disagree about them).
- **A probe runs on request** and proves create -> read -> update -> delete plus
  one document write and readback. Ids alone do not prove anything works.

Nothing here writes a sounding note; that is ``publish.py``.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from scripts import config as conf
from scripts import feishu_api as fapi
from scripts import feishu_schema as schema

WORKSPACE = os.path.join(conf.SKILL_DIR, "notes", ".feishu-workspace.json")
BASE_NAME = "sounding 论文库"
FOLDER_NAME = "sounding"
PROBE_CITEKEY = "__sounding_probe__"


def say(ok, label, detail=""):
    mark = "OK  " if ok else "FAIL"
    print(f"  [{mark}] {label}" + (f"  {detail}" if detail else ""))
    return ok


def find_table(client, app_token, name):
    for t in client.list_tables(app_token):
        if t.get("name") == name:
            return t.get("table_id")
    return None


def ensure_base(client, cfg_feishu, apply):
    token = cfg_feishu.get("base_app_token")
    if token:
        try:
            client.list_tables(token)
            say(True, "base", token)
            return token, False
        except fapi.FeishuError as e:
            say(False, "base from config unreachable", f"{e.code} {e.msg}")
            return None, False
    if not apply:
        say(False, "base", "none configured (pass --apply to create)")
        return None, True
    token = client.create_base(BASE_NAME)
    say(True, "base created", token)
    return token, True


def ensure_folder(client, cfg_feishu, apply):
    token = cfg_feishu.get("folder_token")
    if token:
        say(True, "folder", token)
        return token
    if not apply:
        say(False, "folder", "none configured (pass --apply to create)")
        return ""
    token = client.create_folder(FOLDER_NAME)
    say(True, "folder created", token)
    return token


def ensure_tables(client, app_token, apply, table_ids):
    """Create missing tables (scalar fields first), then links."""
    for key, spec in schema.TABLES.items():
        existing = find_table(client, app_token, spec["name"])
        if existing:
            table_ids[key] = existing
            say(True, f"table {spec['name']}", existing)
            if apply:
                # A schema change (e.g. a new DOI column) must land on the
                # existing table too, not only on tables created from scratch.
                present = {f.get("field_name")
                           for f in client.list_fields(app_token, existing)}
                for name, ftype in spec["fields"]:
                    if name not in present:
                        client.create_field(app_token, existing,
                                            schema.field_spec(name, ftype))
                        say(True, f"field {name} created", spec["name"])
            continue
        if not apply:
            say(False, f"table {spec['name']}", "missing (pass --apply)")
            continue
        fields = [schema.field_spec(n, t) for n, t in spec["fields"]]
        created = client.create_table(app_token, spec["name"], fields)
        table_ids[key] = created["table_id"]
        say(True, f"table {spec['name']} created", f"{created['table_id']} ({len(fields)} fields)")

    if not table_ids.get("papers"):
        return False

    # Links last: each one needs its target table to exist already.
    ok = True
    for name, target in schema.PAPER_LINKS:
        target_id = table_ids.get(target)
        if not target_id:
            ok = say(False, f"link {name}", f"target table {target} missing")
            continue
        present = {f["field_name"] for f in client.list_fields(app_token, table_ids["papers"])}
        if name in present:
            continue
        if not apply:
            ok = say(False, f"link {name}", "missing (pass --apply)")
            continue
        client.create_field(app_token, table_ids["papers"], schema.link_spec(name, target_id))
        say(True, f"link {name} created", f"-> {target}")
    return ok


def verify_fields(client, app_token, table_ids):
    """Every declared field must exist with the declared type."""
    want = {n: t for n, t in schema.PAPER_FIELDS}
    for name, (_, target) in zip([n for n, _ in schema.PAPER_LINKS],
                                 [(n, t) for n, t in schema.PAPER_LINKS]):
        want[name] = schema.LINK
    have = {f["field_name"]: f["type"] for f in client.list_fields(app_token, table_ids["papers"])}
    missing = [n for n in want if n not in have]
    wrong = [(n, have[n], want[n]) for n in want if n in have and have[n] != want[n]]
    say(not missing, "papers fields present", ", ".join(missing) or f"all {len(want)}")
    for n, got, exp in wrong:
        say(False, f"field type mismatch {n}", f"bitable={got} expected={exp}")
    return not missing and not wrong


def probe(client, app_token, table_id, folder):
    """Prove the loop works: record CRUD and one document write+readback."""
    ok = True
    try:
        rec = client.create_record(app_token, table_id, {
            "标题": "sounding probe", "citekey": PROBE_CITEKEY, "年份": 2026,
            "类型": "other", "数据类型": ["benchmark-only"],
        })
        rid = rec["record_id"]
        say(True, "probe record created", rid)
    except fapi.FeishuError as e:
        return say(False, "probe record create", f"{e.code} {e.msg}")

    try:
        got = client.search_records(app_token, table_id,
                                    [("citekey", "is", [PROBE_CITEKEY])])
        same = len(got) == 1 and got[0]["record_id"] == rid
        ok &= say(same, "probe search by citekey", f"{len(got)} hit(s)")
    except fapi.FeishuError as e:
        ok &= say(False, "probe search", f"{e.code} {e.msg}")

    try:
        client.update_record(app_token, table_id, rid, {"去留": "存档"})
        ok &= say(True, "probe record updated")
    except fapi.FeishuError as e:
        ok &= say(False, "probe record update", f"{e.code} {e.msg}")

    try:
        doc = client.create_doc("sounding probe doc", folder)
        say(True, "probe doc created", doc)
    except fapi.FeishuError as e:
        return ok & say(False, "probe doc create", f"{e.code} {e.msg}")

    try:
        client.append_children(doc, [
            {"block_type": 3, "heading1": {"elements": [{"text_run": {"content": "探针"}}], "style": {}}},
            {"block_type": 2, "text": {"elements": [{"text_run": {"content": "第一行"}}], "style": {}}},
        ])
        png = _tiny_png()
        ft = client.upload_media(png, "probe.png", "docx_image", doc)
        client.append_children(doc, [{"block_type": 27, "image": {"file_token": ft}}])
        text = client.doc_text(doc)
        ok &= say("第一行" in text, "probe doc readback", repr(text[:40]))
        client.grant_tenant_access(doc)
        say(True, "probe doc shared with tenant", client.doc_url(doc))
    except fapi.FeishuError as e:
        ok &= say(False, "probe doc write", f"{e.code} {e.msg}")
    finally:
        try:
            client.delete(f"/open-apis/drive/v1/files/{doc}", params={"type": "docx"})
            say(True, "probe doc removed")
        except fapi.FeishuError as e:
            say(False, "probe doc cleanup", f"{e.code} {e.msg}")

    try:
        client.delete_record(app_token, table_id, rid)
        say(True, "probe record removed")
    except fapi.FeishuError as e:
        ok &= say(False, "probe record cleanup", f"{e.code} {e.msg}")
    return ok


def _tiny_png(w=16, h=12):
    import struct
    import zlib

    def chunk(tag, data):
        c = struct.pack(">I", len(data)) + tag + data
        return c + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)

    raw = b"".join(b"\x00" + b"\x20\x80\xff" * w for _ in range(h))
    return (b"\x89PNG\r\n\x1a\n"
            + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(raw))
            + chunk(b"IEND", b""))


def write_back(cfg_feishu, table_ids, folder):
    """Persist the ids so the next run (and the publisher) agree with this one."""
    path = conf.config_path()
    with open(path, encoding="utf-8") as f:
        raw = json.load(f)
    section = raw.setdefault("feishu", {})
    for key, value in (("base_app_token", cfg_feishu.get("base_app_token")),
                       ("papers_table_id", table_ids.get("papers")),
                       ("folder_token", folder),
                       ("domain", cfg_feishu.get("domain"))):
        if value:
            section[key] = value
    with open(path, "w", encoding="utf-8") as f:
        json.dump(raw, f, ensure_ascii=False, indent=2)
        f.write("\n")
    return path


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--apply", action="store_true", help="create anything missing")
    ap.add_argument("--probe", action="store_true", help="also run write/read/update/delete")
    args = ap.parse_args()

    section = conf.feishu()
    if not section.get("app_id") or not section.get("app_secret"):
        print("feishu.app_id / app_secret are not configured (config.local.json or env).")
        return 2
    client = fapi.client_from_config(section)

    print("feishu workspace")
    app_token, _ = ensure_base(client, section, args.apply)
    if not app_token:
        return 1
    section["base_app_token"] = app_token

    if not section.get("domain"):
        domain = client.base_domain(app_token)
        if domain:
            section["domain"] = domain
            say(True, "domain discovered", domain)

    folder = ensure_folder(client, section, args.apply)

    table_ids = {}
    ensure_tables(client, app_token, args.apply, table_ids)
    if table_ids.get("papers"):
        verify_fields(client, app_token, table_ids)

    ok = True
    if args.probe and table_ids.get("papers"):
        ok = probe(client, app_token, table_ids["papers"], folder)

    if args.apply:
        path = write_back(section, table_ids, folder)
        say(True, "config updated", path)

    snapshot = {
        "checked_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "app_token": app_token,
        "folder_token": folder,
        "tables": table_ids,
        "domain": section.get("domain"),
        "timings_ms": client.timings,
    }
    os.makedirs(os.path.dirname(WORKSPACE), exist_ok=True)
    with open(WORKSPACE, "w", encoding="utf-8") as f:
        json.dump(snapshot, f, ensure_ascii=False, indent=2)
    print(f"\nworkspace -> {WORKSPACE}")
    print(f"timings_ms -> {client.timings}")
    if section.get("domain") and table_ids.get("papers"):
        print(f"papers table: https://{section['domain']}/base/{app_token}?table={table_ids['papers']}")
    client.close()
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())

"""Write tags and the Notion link back to a Zotero item.

  python zotero_write.py --citekey KEY --show
  python zotero_write.py --citekey KEY --extra https://www.notion.so/xxxx
  python zotero_write.py --citekey KEY --tags p:protein-design,m:diffusion,d:PDB
  python zotero_write.py --citekey KEY --tags ... --extra ... --dry-run

Needs zotero.user_id and zotero.api_key in config.local.json, or the
ZOTERO_USER_ID / ZOTERO_API_KEY environment variables. Create a key at
https://www.zotero.org/settings/keys with library access, write access,
and notes access if you also write child notes.

Why a script instead of a one-line call: the API replaces whole fields, so
sending {"tags": [...]} would wipe every tag the user added by hand. This
reads the item, merges, and writes back. Tags carrying one of the family
prefixes (p: / m: / d:) are treated as the skill's own and replaced; every
other tag is left alone.
"""
import argparse
import json
import os
import shutil
import sqlite3
import sys
import tempfile
import time
import urllib.error
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import config  # noqa: E402


def item_key_from_citekey(citekey, cfg):
    """Look the item key up in a copy of the local database."""
    db = config.zotero_db(cfg)
    if not db or not os.path.exists(db):
        return None, "no zotero.db_path configured"
    tmp = os.path.join(tempfile.gettempdir(), "zotero_write_copy.sqlite")
    shutil.copyfile(db, tmp)
    c = sqlite3.connect(tmp)
    rows = c.execute("""
        select i.key from itemData d
        join fieldsCombined f on d.fieldID = f.fieldID
        join itemDataValues v on d.valueID = v.valueID
        join items i on d.itemID = i.itemID
        where f.fieldName = 'citationKey' and v.value = ?""", (citekey,)).fetchall()
    c.close()
    if not rows:
        return None, f"citekey {citekey!r} not found in the local library"
    if len(rows) > 1:
        return None, f"citekey {citekey!r} matches {len(rows)} items; fix it in Zotero first"
    return rows[0][0], None


def request(method, url, api_key, body=None, headers=None):
    h = {"Zotero-API-Key": api_key, "Zotero-API-Version": "3"}
    if headers:
        h.update(headers)
    data = None
    if body is not None:
        data = json.dumps(body).encode("utf-8")
        h["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, headers=h, method=method)
    try:
        with urllib.request.urlopen(req) as r:
            raw = r.read()
            return r.status, dict(r.headers), (json.loads(raw) if raw else None)
    except urllib.error.HTTPError as e:
        raw = e.read().decode("utf-8", "replace")
        try:
            raw = json.loads(raw)
        except Exception:
            pass
        return e.code, dict(e.headers), raw


def merge_tags(existing, wanted, prefixes):
    """Drop the skill's own prefixed tags, keep everything else, add the new ones."""
    pref = tuple(p for p in prefixes.values() if p)
    kept, names = [], set()
    for t in existing or []:
        name = t.get("tag", "")
        if pref and name.startswith(pref):
            continue
        if name not in names:
            kept.append({"tag": name})
            names.add(name)
    for name in wanted:
        if name and name not in names:
            kept.append({"tag": name})
            names.add(name)
    return kept


def merge_extra(existing, url):
    lines = [l for l in (existing or "").splitlines()
             if not l.strip().lower().startswith("notion:")]
    lines.append(f"Notion: {url}")
    return "\n".join(lines).strip() + "\n"


def tag_names(item):
    return [t.get("tag", "") for t in item.get("tags", [])]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--citekey", required=True)
    ap.add_argument("--item-key", help="skip the local lookup and use this key")
    ap.add_argument("--tags", help="comma-separated, with family prefixes")
    ap.add_argument("--extra", help="Notion page URL to record in Extra")
    ap.add_argument("--show", action="store_true", help="print the item, write nothing")
    ap.add_argument("--dry-run", action="store_true", help="print the change, write nothing")
    a = ap.parse_args()

    cfg = config.load()
    base, user_id, api_key = config.zotero_api(cfg)
    if not user_id or not api_key:
        sys.exit("set zotero.user_id and zotero.api_key in config.local.json "
                 "(create a key at https://www.zotero.org/settings/keys)")

    key = a.item_key
    if not key:
        key, err = item_key_from_citekey(a.citekey, cfg)
        if not key:
            sys.exit(err)

    url = f"{base}/users/{user_id}/items/{key}"
    status, _, item = request("GET", url, api_key)
    if status != 200:
        sys.exit(f"could not read item {key}: HTTP {status} {item}")

    if a.show:
        print(f"citekey : {a.citekey}")
        print(f"item    : {key}")
        print(f"type    : {item.get('data', {}).get('itemType')}")
        print(f"tags    : {', '.join(tag_names(item)) or '(none)'}")
        extra = item.get("data", {}).get("extra", "")
        print(f"extra   : {extra[:300] or '(none)'}")
        return

    if not a.tags and not a.extra:
        sys.exit("nothing to write: pass --tags and/or --extra (or --show)")

    patch = {}
    if a.tags:
        wanted = [t.strip() for t in a.tags.split(",") if t.strip()]
        before = tag_names(item)
        after = [t["tag"] for t in merge_tags(item.get("tags"), wanted, config.tag_prefixes(cfg))]
        patch["tags"] = merge_tags(item.get("tags"), wanted, config.tag_prefixes(cfg))
        print("tags before :", ", ".join(before) or "(none)")
        print("tags after  :", ", ".join(after) or "(none)")
    if a.extra:
        old = item.get("data", {}).get("extra", "")
        patch["extra"] = merge_extra(old, a.extra)
        print("extra before:", (old or "(none)").replace("\n", " / ")[:200])
        print("extra after :", patch["extra"].replace("\n", " / ").strip()[:200])

    if a.dry_run:
        print("dry run: nothing written")
        return

    def ok(status):
        # Zotero answers a single-object write with 204 No Content.
        return status in (200, 204)

    def report(status, headers, data):
        ver = data.get("version") if isinstance(data, dict) else None
        ver = ver or headers.get("Last-Modified-Version", "?")
        print(f"written: item {key} now at version {ver}")

    version = item.get("version", 0)
    for attempt in (1, 2):
        status, headers, data = request("PATCH", url, api_key, patch,
                                        {"If-Unmodified-Since-Version": str(version)})
        if ok(status):
            report(status, headers, data)
            return
        if status == 412 and attempt == 1:
            print("item changed while writing; refetching once")
            status, _, item = request("GET", url, api_key)
            version = item.get("version", 0)
            continue
        if status == 429:
            wait = int(headers.get("Retry-After", 5) or 5)
            print(f"rate limited; waiting {wait}s")
            time.sleep(wait)
            status, headers, data = request("PATCH", url, api_key, patch,
                                            {"If-Unmodified-Since-Version": str(version)})
            if ok(status):
                report(status, headers, data)
                return
        sys.exit(f"write failed: HTTP {status} {data}")


if __name__ == "__main__":
    main()

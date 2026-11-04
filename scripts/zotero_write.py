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
import sys
import urllib.error
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import config  # noqa: E402
import paper_identity as identity  # noqa: E402


def item_key_from_citekey(citekey, cfg):
    """Resolve an existing key or DOI using the shared read-only snapshot."""
    db = config.zotero_db(cfg)
    if not db or not os.path.exists(db):
        return None, "no zotero.db_path configured"
    try:
        item = identity.lookup_local(db, citekey)
    except identity.IdentityError as e:
        return None, str(e)
    if not item:
        return None, f"citekey {citekey!r} not found in the local library"
    return item["item_key"], None


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
        with urllib.request.urlopen(req, timeout=30) as r:
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
    return identity.merge_tags(existing, wanted, prefixes)


def merge_extra(existing, url):
    return identity.merge_extra(existing, notion=url)


def tag_names(item):
    return [t.get("tag", "") for t in item.get("data", item).get("tags", [])]


def patch_with_retry(request_fn, url, api_key, make_patch, item=None):
    """Retry one version conflict, rebuilding the merge from the fresh object."""
    for attempt in range(2):
        if item is None:
            status, _, item = request_fn("GET", url, api_key)
            if status != 200 or not isinstance(item, dict) or "data" not in item:
                raise RuntimeError(f"could not read item: HTTP {status} {item}")
        patch = make_patch(item)
        if not patch:
            return {"Last-Modified-Version": str(item.get("version", 0))}, None
        status, headers, data = request_fn("PATCH", url, api_key, patch,
                                         {"If-Unmodified-Since-Version": str(item.get("version", 0))})
        if status in (200, 204):
            return headers, data
        if status == 412 and attempt == 0:
            item = None
            continue
        raise RuntimeError(f"write failed: HTTP {status} {data}")


def write_patch(item, tags, extra, prefixes):
    data = item["data"]
    patch = {}
    if tags is not None:
        wanted = [t.strip() for t in tags.split(",") if t.strip()]
        patch["tags"] = merge_tags(data.get("tags"), wanted, prefixes)
    if extra is not None:
        patch["extra"] = merge_extra(data.get("extra"), extra)
    return patch


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--citekey")
    ap.add_argument("--item-key", help="skip the local lookup and use this key")
    ap.add_argument("--tags", help="comma-separated, with family prefixes")
    ap.add_argument("--extra", help="Notion page URL to record in Extra")
    ap.add_argument("--show", action="store_true", help="print the item, write nothing")
    ap.add_argument("--dry-run", action="store_true", help="print the change, write nothing")
    a = ap.parse_args()
    if not a.citekey and not a.item_key:
        ap.error("give --citekey or --item-key")

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
    if status != 200 or not isinstance(item, dict) or "data" not in item:
        sys.exit(f"could not read item {key}: HTTP {status} {item}")

    if a.show:
        print(f"citekey : {a.citekey}")
        print(f"item    : {key}")
        print(f"type    : {item.get('data', {}).get('itemType')}")
        print(f"tags    : {', '.join(tag_names(item)) or '(none)'}")
        extra = item.get("data", {}).get("extra", "")
        print(f"extra   : {extra[:300] or '(none)'}")
        return

    if a.tags is None and a.extra is None:
        sys.exit("nothing to write: pass --tags and/or --extra (or --show)")

    make_patch = lambda current: write_patch(current, a.tags, a.extra, config.tag_prefixes(cfg))
    patch = make_patch(item)
    print(json.dumps(patch, ensure_ascii=False, indent=2))

    if a.dry_run:
        print("dry run: nothing written")
        return

    headers, data = patch_with_retry(request, url, api_key, make_patch, item)
    ver = (data.get("version") if isinstance(data, dict) else None) or headers.get("Last-Modified-Version", "?")
    print(f"written: item {key} now at version {ver}")


if __name__ == "__main__":
    try:
        main()
    except (OSError, ValueError, RuntimeError) as e:
        sys.exit(str(e))

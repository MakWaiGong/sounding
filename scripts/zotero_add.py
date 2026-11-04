"""Create a Zotero item from a PDF and attach the file, in one call.

  python zotero_add.py --pdf "D:\\papers\\x.pdf" \
    --title "ProAR: Probabilistic Autoregressive Modeling for Molecular Dynamics" \
    --authors "Kaiwen Cheng;Yutian Liu;Zhiwei Nie" \
    --year 2026 --venue "AAAI" --type conference \
    --citekey chengProAR2026 \
    --notion https://app.notion.com/p/xxxx \
    --tags p:protein-structure-prediction,m:molecular-dynamics,d:simulated

Run this *before* writing the Notion row: the note wants a real citationKey, and
the library wants the PDF. Doing it afterwards leaves the note pointing at a
citekey that does not exist in Zotero.

What it does, in order:

  1. looks for the paper by citekey (local database) and by title (Web API);
     if it exists, updates that item instead of creating a duplicate
  2. creates the item (conferencePaper / journalArticle / document)
  3. uploads the PDF as an imported_file child attachment
     (Zotero File Storage: authorize -> POST to S3 -> register)
  4. writes Extra with `Citation Key:` and `Notion:` lines so Better BibTeX
     stops generating a different key, and so the round trip is traceable

Needs zotero.user_id / zotero.api_key in config.local.json, with library and
write access. Create one at https://www.zotero.org/settings/keys.

If anything fails the script prints the HTTP status and the response body and
exits non-zero. It never half-creates a silent mess: the item key is printed as
soon as it exists, so a failed upload can be retried with --item-key.
"""
import argparse
import hashlib
import json
import os
import random
import string
import sys
import urllib.error
import urllib.parse
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import config  # noqa: E402
import citekey as ck  # noqa: E402
import paper_identity as identity  # noqa: E402
from zotero_write import patch_with_retry  # noqa: E402

TYPE_MAP = {
    "conference": "conferencePaper",
    "journal": "journalArticle",
    "review": "journalArticle",
    "other": "document",
}


def request(method, url, api_key, body=None, headers=None, raw=None):
    """JSON request. `raw` sends pre-encoded bytes with the given content type."""
    h = {"Zotero-API-Key": api_key, "Zotero-API-Version": "3"}
    if headers:
        h.update(headers)
    data = None
    if raw is not None:
        data = raw
    elif body is not None:
        data = json.dumps(body).encode("utf-8")
        h["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, headers=h, method=method)
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            payload = r.read()
            return r.status, dict(r.headers), (json.loads(payload) if payload else None)
    except urllib.error.HTTPError as e:
        payload = e.read().decode("utf-8", "replace")
        try:
            payload = json.loads(payload)
        except Exception:
            pass
        return e.code, dict(e.headers), payload


def write_token():
    alphabet = string.ascii_letters + string.digits
    return "".join(random.choice(alphabet) for _ in range(32))


def normalise(s):
    return "".join(ch for ch in (s or "").lower() if ch.isalnum())


def parse_authors(spec):
    out = []
    for chunk in (spec or "").split(";"):
        name = chunk.strip()
        if not name:
            continue
        parts = name.rsplit(" ", 1)
        if len(parts) == 2:
            out.append({"creatorType": "author", "firstName": parts[0], "lastName": parts[1]})
        else:
            out.append({"creatorType": "author", "name": name})
    return out


def local_item_by_citekey(citekey, cfg):
    item = identity.lookup_local(config.zotero_db(cfg), citekey)
    return item["item_key"] if item else None


def list_api(path, base, user_id, api_key, **params):
    """Read all pages before deciding whether an identity/collection is unique."""
    start = 0
    while True:
        q = urllib.parse.urlencode(dict(params, limit=100, start=start))
        status, _, data = request("GET", f"{base}/users/{user_id}/{path}?{q}", api_key)
        if status != 200 or not isinstance(data, list):
            raise RuntimeError(f"could not list {path}: HTTP {status} {data}")
        yield from data
        if len(data) < 100:
            return
        start += len(data)


def api_item_by_title(title, base, user_id, api_key):
    want = normalise(title)
    matches = [item for item in list_api("items/top", base, user_id, api_key,
                                       q=title, qmode="titleCreatorYear")
               if normalise(item.get("data", {}).get("title")) == want]
    item = identity.unique_item(matches, "title")
    return (item["key"], item["data"]) if item else None


def md5_of(path):
    h = hashlib.md5()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def build_item(a, tags, extra):
    item_type = a.item_type or TYPE_MAP.get(a.type, "conferencePaper")
    data = {
        "itemType": item_type,
        "title": a.title,
        "creators": parse_authors(a.authors),
        "date": str(a.year),
        "tags": [{"tag": t.strip()} for t in tags if t.strip()],
        "extra": extra,
        "language": a.language or "en",
        "url": a.url or "",
        "DOI": a.doi or "",
        "abstractNote": a.abstract or "",
    }
    if item_type == "conferencePaper":
        data["proceedingsTitle"] = a.venue
        data["conferenceName"] = a.conference_name or a.venue
        if a.place:
            data["place"] = a.place
        if a.publisher:
            data["publisher"] = a.publisher
    elif item_type == "journalArticle":
        data["publicationTitle"] = a.venue
        if a.volume:
            data["volume"] = a.volume
        if a.pages:
            data["pages"] = a.pages
    else:
        data["publisher"] = a.venue
    if a.collection:
        data["collections"] = [a.collection]
    return data


def choose_citekey(item, requested, fallback):
    """Existing keys are permanent. Renaming requires an explicit migration."""
    current = identity.existing_citekey(item.get("data", {}))
    if current and fallback and current != fallback:
        raise identity.IdentityError(f"citation key changed from {fallback!r} to {current!r}; resolve identity before writing")
    current = current or fallback
    if current and requested and current != requested:
        raise identity.IdentityError(f"existing citekey {current!r} differs from {requested!r}; keep the existing key")
    return current or requested or item.get("key")


def update_patch(item, a, prefixes, fallback_key=None):
    current = item["data"]
    current_key = identity.existing_citekey(current)
    key = choose_citekey(item, a.citekey, fallback_key)
    if a.doi and current.get("DOI") and identity.normalize_doi(a.doi) != identity.normalize_doi(current["DOI"]):
        raise identity.IdentityError("the matched item has a different DOI; disambiguate before writing")
    patch = {"title": a.title, "date": str(a.year)}
    # Preserve byte-for-byte Extra when no managed line needs changing. This
    # avoids a no-op PATCH caused only by moving Citation Key to the last line.
    if a.notion is not None or current_key is None:
        patch["extra"] = identity.merge_extra(
            current.get("extra"), citekey=key if current_key is None else None,
            notion=a.notion)
    venue_field = {"conferencePaper": "proceedingsTitle", "journalArticle": "publicationTitle"}.get(
        current.get("itemType"), "publisher")
    patch[venue_field] = a.venue
    for attr, field in (("language", "language"), ("url", "url"), ("doi", "DOI"),
                        ("abstract", "abstractNote"), ("conference_name", "conferenceName"),
                        ("place", "place"), ("publisher", "publisher"),
                        ("volume", "volume"), ("pages", "pages")):
        value = getattr(a, attr)
        if value is not None:
            patch[field] = identity.normalize_doi(value) if attr == "doi" else value
    if a.tags is not None:
        patch["tags"] = identity.merge_tags(current.get("tags"),
            [t.strip() for t in a.tags.split(",") if t.strip()], prefixes)
    return {k: v for k, v in patch.items() if current.get(k) != v}


def upload_file(base, user_id, api_key, attachment_key, pdf):
    """Authorize, POST the bytes to storage, then register. Zotero API v3."""
    st = os.stat(pdf)
    form = urllib.parse.urlencode({
        "md5": md5_of(pdf),
        "filename": os.path.basename(pdf),
        "filesize": st.st_size,
        "mtime": int(st.st_mtime * 1000),
    }).encode()
    status, _, auth = request(
        "POST", f"{base}/users/{user_id}/items/{attachment_key}/file", api_key,
        raw=form, headers={"Content-Type": "application/x-www-form-urlencoded",
                           "If-None-Match": "*"})
    if status != 200:
        return False, f"authorize failed: HTTP {status} {auth}"
    if isinstance(auth, dict) and auth.get("exists"):
        return True, "file already on server, linked"

    # prefix and suffix are literal multipart text: the file goes between them.
    with open(pdf, "rb") as stream:
        body = auth["prefix"].encode("utf-8") + stream.read() + auth["suffix"].encode("utf-8")
    req = urllib.request.Request(auth["url"], data=body, method="POST",
                                 headers={"Content-Type": auth["contentType"]})
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            if r.status not in (200, 201, 204):
                return False, f"upload failed: HTTP {r.status}"
    except urllib.error.HTTPError as e:
        return False, f"upload failed: HTTP {e.code} {e.read().decode('utf-8', 'replace')}"

    reg = urllib.parse.urlencode({"upload": auth["uploadKey"]}).encode()
    status, _, data = request(
        "POST", f"{base}/users/{user_id}/items/{attachment_key}/file", api_key,
        raw=reg, headers={"Content-Type": "application/x-www-form-urlencoded",
                          "If-None-Match": "*"})
    if status not in (200, 204):
        return False, f"register failed: HTTP {status} {data}"
    return True, "uploaded"


def attach_pdf(base, user_id, api_key, parent_key, pdf, api_key_header, title):
    # No md5/mtime here on purpose. With Zotero File Storage those two are set
    # by the upload registration step; sending them early makes the authorize
    # call believe the file is already on the server and answer 412.
    payload = [{
        "itemType": "attachment",
        "parentItem": parent_key,
        "linkMode": "imported_file",
        "title": title,
        "contentType": "application/pdf",
        "filename": os.path.basename(pdf),
    }]
    status, _, data = request("POST", f"{base}/users/{user_id}/items", api_key,
                              payload, {"Zotero-Write-Token": write_token()})
    if status != 200:
        return None, f"attachment item failed: HTTP {status} {data}"
    ok = data.get("successful", {}).get("0", {}) if isinstance(data, dict) else {}
    key = ok.get("key") or ok.get("data", {}).get("key")
    if not key:
        return None, f"attachment item failed: no key in {data}"
    return key, None


def file_present(base, user_id, key, api_key):
    """Ask the server for the bytes, not for the metadata.

    An interrupted upload leaves an attachment item carrying md5 and mtime but
    with nothing behind them, so trusting the item's md5 is not enough.
    """
    url = f"{base}/users/{user_id}/items/{key}/file"
    req = urllib.request.Request(url, headers={"Zotero-API-Key": api_key,
                                               "Zotero-API-Version": "3"})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            r.read(64)  # enough to prove the response opens; do not pull the whole PDF
            return r.status == 200
    except urllib.error.HTTPError as e:
        if e.code == 404:
            return False
        raise RuntimeError(f"could not check attachment {key}: HTTP {e.code}") from e


def resolve_collection(base, user_id, api_key, name, create=True):
    """Find a top-level collection by name, creating it when missing.

    Notion is the source of truth; this collection is the boundary of what the
    skill maintains in Zotero. It is a projection, never a lookup key: items are
    found by citekey, so deleting the collection costs nothing but a rebuild.
    """
    matches = [c for c in list_api("collections", base, user_id, api_key)
               if c.get("data", {}).get("name") == name and not c["data"].get("parentCollection")]
    current = identity.unique_item(matches, f"collection {name!r}")
    if current:
        return current.get("key") or current["data"].get("key"), None
    if not create:
        return None, f"collection {name!r} not found"
    status, _, resp = request("POST", f"{base}/users/{user_id}/collections", api_key,
                              [{"name": name, "parentCollection": False}],
                              {"Zotero-Write-Token": write_token()})
    if status != 200:
        return None, f"could not create collection {name!r}: HTTP {status} {resp}"
    ok = resp.get("successful", {}).get("0", {}) if isinstance(resp, dict) else {}
    key = ok.get("key") or ok.get("data", {}).get("key")
    if not key:
        return None, f"could not create collection {name!r}: no key in {resp}"
    print(f"collection: created {name!r} ({key})")
    return key, None


def ensure_in_collection(base, user_id, api_key, item_key, collection_key):
    """Union membership. A PATCH with `collections` replaces the list, so the
    item's other collections must be read first and carried over."""
    def make_patch(item):
        have = list(item["data"].get("collections") or [])
        return {} if collection_key in have else {"collections": have + [collection_key]}
    patch_with_retry(request, f"{base}/users/{user_id}/items/{item_key}", api_key, make_patch)
    return None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--pdf", required=True)
    ap.add_argument("--title", required=True)
    ap.add_argument("--authors", required=True, help="semicolon separated, 'First Last'")
    ap.add_argument("--year", required=True, type=int)
    ap.add_argument("--venue", required=True, help="AAAI / Nature Methods / ... no year")
    ap.add_argument("--type", default="conference",
                    choices=["conference", "journal", "review", "other"],
                    help="mirrors the Notion 类型 value")
    ap.add_argument("--item-type", help="override the Zotero itemType")
    ap.add_argument("--citekey",
                    help="machine key; derived from DOI or title when omitted")
    ap.add_argument("--notion", help="Notion page URL to record in Extra")
    ap.add_argument("--tags", help="comma separated, family prefixed; omitted preserves all tags")
    ap.add_argument("--conference-name")
    ap.add_argument("--place")
    ap.add_argument("--publisher")
    ap.add_argument("--volume")
    ap.add_argument("--pages")
    ap.add_argument("--doi")
    ap.add_argument("--url")
    ap.add_argument("--abstract")
    ap.add_argument("--language", help="new items default to en; omitted preserves existing language")
    ap.add_argument("--collection", help="collection name; defaults to zotero.collection")
    ap.add_argument("--no-collection", action="store_true",
                    help="do not file the item into the skill's collection")
    ap.add_argument("--item-key", help="retry the upload for an item that exists")
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args()

    if not os.path.exists(a.pdf):
        sys.exit(f"pdf not found: {a.pdf}")

    cfg = config.load()
    base, user_id, api_key = config.zotero_api(cfg)
    if not user_id or not api_key:
        sys.exit("set zotero.user_id and zotero.api_key in config.local.json "
                 "(create a key at https://www.zotero.org/settings/keys)")

    if a.doi:
        a.doi = identity.normalize_doi(a.doi)
        if not a.doi:
            sys.exit("invalid DOI; use a bare DOI or a doi.org URL")
    derived = ck.derive(a.doi, a.title, a.authors, a.year)
    local = identity.lookup_local(config.zotero_db(cfg), a.item_key, a.citekey, a.doi, derived)
    if a.item_key and local and local["item_key"] != a.item_key:
        raise identity.IdentityError("--item-key and the supplied DOI/citekey resolve to different items")
    local_key = identity.existing_citekey(local["fields"]) if local else None
    key = a.item_key or (local["item_key"] if local else None)
    if not key:
        hit = api_item_by_title(a.title, base, user_id, api_key)
        key = hit[0] if hit else None
        # A server title hit may have a local Better BibTeX key not yet in Extra.
        if key:
            local = identity.lookup_local(config.zotero_db(cfg), key)
            local_key = identity.existing_citekey(local["fields"]) if local else None
    current = None
    if key:
        status, _, current = request("GET", f"{base}/users/{user_id}/items/{key}", api_key)
        if status != 200 or not isinstance(current, dict) or "data" not in current:
            sys.exit(f"could not read item {key}: HTTP {status} {current}")
        resolved_key = choose_citekey(current, a.citekey, local_key)
    else:
        resolved_key = a.citekey or derived
    print(f"citekey: {resolved_key} ({'existing' if key else 'new'})")
    cname = None if a.no_collection else (a.collection or config.zotero_collection(cfg))
    a.collection = None  # build_item accepts resolved collection keys only
    tags = [t.strip() for t in (a.tags or "").split(",") if t.strip()]
    data = build_item(a, tags, identity.merge_extra("", citekey=resolved_key, notion=a.notion))
    make_patch = lambda item: update_patch(item, a, config.tag_prefixes(cfg), resolved_key)
    preview = make_patch(current) if current else data
    if a.dry_run:
        print(json.dumps({"operation": "update" if key else "create", "item_key": key,
                          "citekey": resolved_key, "payload": preview,
                          "collection": cname, "attachment": a.pdf}, ensure_ascii=False, indent=2))
        return

    # Identity and patch validation happen before creating any collection/item.
    collection_key = None
    if cname:
        collection_key, err = resolve_collection(base, user_id, api_key, cname)
        if err:
            sys.exit(err)
    created = None
    if key:
        patch_with_retry(request, f"{base}/users/{user_id}/items/{key}", api_key, make_patch, current)
        print(f"updated item {key}")
    else:
        if collection_key:
            data["collections"] = [collection_key]
        status, _, resp = request("POST", f"{base}/users/{user_id}/items", api_key,
                                  [data], {"Zotero-Write-Token": write_token()})
        if status != 200:
            sys.exit(f"create failed: HTTP {status} {resp}")
        ok = resp.get("successful", {}).get("0", {}) if isinstance(resp, dict) else {}
        key = ok.get("key") or ok.get("data", {}).get("key")
        if not key:
            sys.exit(f"create failed: no key in {resp}")
        created = key
        print(f"created item {key}")

    # Update path preserves other collections; creation already set membership.
    if collection_key and not created:
        err = ensure_in_collection(base, user_id, api_key, key, collection_key)
        if err:
            sys.exit(err)
        print(f"collection: {key} is in {collection_key}")

    # 3. attach the PDF, unless this item already has a working file attachment
    kids = list(list_api(f"items/{key}/children", base, user_id, api_key))
    has_file = any(
        c.get("data", {}).get("itemType") == "attachment"
        and c.get("data", {}).get("contentType") == "application/pdf"
        and c.get("data", {}).get("linkMode") in ("imported_file", "imported_url")
        and c.get("data", {}).get("md5")
        and file_present(base, user_id, c.get("key"), api_key)
        for c in kids)
    if has_file:
        print("pdf: already attached, skipped")
    else:
        # A previous run may have left an attachment item with no file behind
        # (or metadata without bytes). Delete those, otherwise they pile up.
        for c in kids:
            d = c.get("data", {})
            if (d.get("itemType") == "attachment"
                    and d.get("contentType") == "application/pdf"
                    and d.get("linkMode") in ("imported_file", "imported_url")
                    and not (d.get("md5") and file_present(base, user_id, c.get("key"), api_key))):
                st, _, _ = request("DELETE",
                                   f"{base}/users/{user_id}/items/{c.get('key')}", api_key,
                                   headers={"If-Unmodified-Since-Version": str(c.get("version", 0))})
                if st not in (200, 204):
                    sys.exit(f"could not remove empty attachment {c.get('key')}: HTTP {st}")
                print(f"pdf: removed empty attachment {c.get('key')} (HTTP {st})")

        akey, err = attach_pdf(base, user_id, api_key, key, a.pdf, api_key, "Full Text PDF")
        if err:
            sys.exit(f"{err}\nitem {key} exists without a file; retry with "
                     f"--item-key {key} once the cause is fixed")
        ok, msg = upload_file(base, user_id, api_key, akey, a.pdf)
        print(f"pdf: {msg} (attachment {akey})")
        if not ok:
            sys.exit(f"attachment {akey} is empty on the server; retry with --item-key {key}")

    print(json.dumps({"citekey": resolved_key, "item_key": key,
                      "created": bool(created)}, ensure_ascii=False))


if __name__ == "__main__":
    try:
        main()
    except (OSError, ValueError, RuntimeError) as e:
        sys.exit(str(e))

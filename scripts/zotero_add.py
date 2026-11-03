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
import shutil
import sqlite3
import string
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import config  # noqa: E402

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
        with urllib.request.urlopen(req) as r:
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
    """citationKey lives in itemData, so the local copy is authoritative."""
    db = config.zotero_db(cfg)
    if not db or not os.path.exists(db):
        return None
    tmp = os.path.join(tempfile.gettempdir(), "zotero_add_copy.sqlite")
    shutil.copyfile(db, tmp)
    c = sqlite3.connect(tmp)
    rows = c.execute("""
        select i.key from itemData d
        join fieldsCombined f on d.fieldID = f.fieldID
        join itemDataValues v on d.valueID = v.valueID
        join items i on d.itemID = i.itemID
        where f.fieldName = 'citationKey' and v.value = ?""", (citekey,)).fetchall()
    c.close()
    return rows[0][0] if rows else None


def api_item_by_title(title, base, user_id, api_key):
    q = urllib.parse.urlencode({"q": title, "qmode": "titleCreatorYear", "limit": 25})
    status, _, data = request("GET", f"{base}/users/{user_id}/items?{q}", api_key)
    if status != 200 or not isinstance(data, list):
        return None
    want = normalise(title)
    for item in data:
        if normalise(item.get("data", {}).get("title")) == want:
            return item.get("key"), item.get("data", {})
    return None


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
        "language": a.language,
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
    body = (auth["prefix"].encode("utf-8")
            + open(pdf, "rb").read()
            + auth["suffix"].encode("utf-8"))
    req = urllib.request.Request(auth["url"], data=body, method="POST",
                                 headers={"Content-Type": auth["contentType"]})
    try:
        with urllib.request.urlopen(req) as r:
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
        with urllib.request.urlopen(req) as r:
            r.read(64)  # enough to prove the response opens; do not pull the whole PDF
            return r.status == 200
    except urllib.error.HTTPError:
        return False


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
    ap.add_argument("--citekey", required=True)
    ap.add_argument("--notion", help="Notion page URL to record in Extra")
    ap.add_argument("--tags", default="", help="comma separated, family prefixed")
    ap.add_argument("--conference-name")
    ap.add_argument("--place")
    ap.add_argument("--publisher")
    ap.add_argument("--volume")
    ap.add_argument("--pages")
    ap.add_argument("--doi")
    ap.add_argument("--url")
    ap.add_argument("--abstract")
    ap.add_argument("--language", default="en")
    ap.add_argument("--collection", help="collection key to file it under")
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

    tags = [t.strip() for t in a.tags.split(",") if t.strip()]
    extra_lines = [f"Citation Key: {a.citekey}"]
    if a.notion:
        extra_lines.append(f"Notion: {a.notion}")
    extra = "\n".join(extra_lines) + "\n"
    data = build_item(a, tags, extra)

    if a.dry_run:
        print(json.dumps(data, ensure_ascii=False, indent=2))
        print(f"attachment: {a.pdf}")
        return

    # 1. already there?
    key = a.item_key
    if not key:
        key = local_item_by_citekey(a.citekey, cfg)
        if key:
            print(f"found by citekey in the local library: {key} (updating)")
        else:
            hit = api_item_by_title(a.title, base, user_id, api_key)
            if hit:
                key, old = hit
                print(f"found by title on the server: {key} (updating)")
                # keep whatever the user already typed into Extra
                keep = [l for l in old.get("extra", "").splitlines()
                        if l.strip() and not l.lower().startswith(("citation key:", "notion:"))]
                data["extra"] = "\n".join(keep + extra_lines) + "\n"

    created = None
    if key:
        # update path: patch the descriptive fields, leave creators alone
        st, _, cur = request("GET", f"{base}/users/{user_id}/items/{key}", api_key)
        if st != 200 or not isinstance(cur, dict):
            sys.exit(f"could not read item {key}: HTTP {st} {cur}")
        patch = {k: v for k, v in data.items()
                 if k not in ("creators", "itemType", "collections")}
        # keep anything the user wrote into Extra; replace only our two lines
        keep = [l for l in cur.get("data", {}).get("extra", "").splitlines()
                if l.strip() and not l.lower().startswith(("citation key:", "notion:"))]
        patch["extra"] = "\n".join(keep + extra_lines) + "\n"
        status, _, resp = request("PATCH", f"{base}/users/{user_id}/items/{key}",
                                  api_key, patch,
                                  {"If-Unmodified-Since-Version": str(cur.get("version", 0))})
        if status not in (200, 204):
            sys.exit(f"update failed for {key}: HTTP {status} {resp}")
        print(f"updated item {key}")
    else:
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

    # 2. attach the PDF, unless this item already has a working file attachment
    status, _, children = request("GET", f"{base}/users/{user_id}/items/{key}/children",
                                  api_key)
    kids = children if isinstance(children, list) else []
    has_file = any(
        c.get("data", {}).get("itemType") == "attachment"
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
                    and d.get("linkMode") in ("imported_file", "imported_url")
                    and not (d.get("md5") and file_present(base, user_id, c.get("key"), api_key))):
                st, _, _ = request("DELETE",
                                   f"{base}/users/{user_id}/items/{c.get('key')}", api_key,
                                   headers={"If-Unmodified-Since-Version": str(c.get("version", 0))})
                print(f"pdf: removed empty attachment {c.get('key')} (HTTP {st})")

        akey, err = attach_pdf(base, user_id, api_key, key, a.pdf, api_key, "Full Text PDF")
        if err:
            sys.exit(f"{err}\nitem {key} exists without a file; retry with "
                     f"--item-key {key} once the cause is fixed")
        ok, msg = upload_file(base, user_id, api_key, akey, a.pdf)
        print(f"pdf: {msg} (attachment {akey})")
        if not ok:
            sys.exit(f"attachment {akey} is empty on the server; retry with --item-key {key}")

    print(json.dumps({"citekey": a.citekey, "item_key": key,
                      "created": bool(created)}, ensure_ascii=False))


if __name__ == "__main__":
    main()

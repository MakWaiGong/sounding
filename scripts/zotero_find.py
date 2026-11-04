"""Look up items in the local Zotero library.

  python zotero_find.py "attention is all you need"
  python zotero_find.py --citekey yangArbovirusSuppressionLectin2025
  python zotero_find.py "transformer" --pdf     # only PDF paths
  python zotero_find.py --list-collections
  python zotero_find.py --tags                 # tag vocabulary actually in use

Reads a consistent in-memory SQLite snapshot, including committed WAL data.
"""
import argparse
from contextlib import closing
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import config  # noqa: E402
import paper_identity as identity  # noqa: E402


def connect(db):
    return identity.snapshot(db)


def rows(c, sql, args=()):
    return c.execute(sql, args).fetchall()


def fields_of(c, item_id):
    return dict(rows(c, """
        select f.fieldName, v.value from itemData d
        join fieldsCombined f on d.fieldID = f.fieldID
        join itemDataValues v on d.valueID = v.valueID
        where d.itemID = ?""", (item_id,)))


def creators_of(c, item_id):
    out = rows(c, """
        select c.lastName, c.firstName, ic.orderIndex from itemCreators ic
        join creators c on ic.creatorID = c.creatorID
        where ic.itemID = ? order by ic.orderIndex""", (item_id,))
    return [f"{b} {a}".strip() for a, b, _ in out]


def attachments_of(c, item_id, data_dir):
    out = []
    for path, item_key, link_mode in rows(c, """
            select ia.path, i.key, ia.linkMode from itemAttachments ia
            join items i on ia.itemID = i.itemID
            where ia.parentItemID = ?
              and i.itemID not in (select itemID from deletedItems)""", (item_id,)):
        if not path:
            continue
        if path.startswith("storage:"):
            full = os.path.join(data_dir, "storage", item_key, path[8:])
        elif os.path.isabs(path):
            full = path
        else:
            full = os.path.join(data_dir, "attachments", path.replace("attachments:", "", 1))
        out.append(full)
    return out


def collections_of(c, item_id):
    return [r[0] for r in rows(c, """
        select col.collectionName from collectionItems ci
        join collections col on ci.collectionID = col.collectionID
        where ci.itemID = ?""", (item_id,))]


def tags_of(c, item_id):
    return [r[0] for r in rows(c, """
        select t.name from itemTags it join tags t on it.tagID = t.tagID
        where it.itemID = ?""", (item_id,))]


def search(c, query, limit=None):
    """Every term must match a bibliographic field, author, item key or tag."""
    terms = [t for t in query.split() if t]
    if not terms:
        return []
    block = """(exists (select 1 from itemData d
                          join fieldsCombined f on d.fieldID = f.fieldID
                          join itemDataValues v on d.valueID = v.valueID
                        where d.itemID = i.itemID
                          and f.fieldName in ('title', 'citationKey', 'DOI', 'date', 'extra')
                          and v.value like ?)
                or exists (select 1 from itemCreators ic
                             join creators cr on ic.creatorID = cr.creatorID
                          where ic.itemID = i.itemID and (cr.lastName || ' ' || cr.firstName) like ?)
                or exists (select 1 from itemTags it
                             join tags t on it.tagID = t.tagID
                          where it.itemID = i.itemID and t.name like ?)
                or i.key = ?)"""
    cond = " and ".join([block] * len(terms))
    args = []
    for t in terms:
        args.extend([f"%{t}%"] * 3 + [t])
    result = rows(c, f"""
        select i.itemID from items i
        where i.itemID not in (select itemID from deletedItems)
          and i.itemID not in (select itemID from itemAttachments)
          and i.itemID not in (select itemID from itemNotes)
          and {cond}
        order by i.itemID desc""", tuple(args))
    return result if limit is None else result[:limit]


def by_citekey(c, key):
    return [(r["item_id"],) for r in identity.resolve_items(c, key)]


def item_json(c, item_id, data_dir):
    fields = fields_of(c, item_id)
    key = rows(c, "select key from items where itemID = ?", (item_id,))[0][0]
    record = {"item_id": item_id, "item_key": key, "metadata": fields,
              "authors": creators_of(c, item_id),
              "collections": collections_of(c, item_id), "tags": tags_of(c, item_id),
              "attachments": [{"path": p, "exists": os.path.exists(p),
                               "is_pdf": p.lower().endswith(".pdf")}
                              for p in attachments_of(c, item_id, data_dir)],
              "doi": identity.normalize_doi(fields.get("DOI")) or None}
    try:
        record["citekey"] = identity.existing_citekey(fields) or key
    except identity.IdentityError as e:
        record["citekey"] = None
        record["identity_error"] = str(e)
    return record


def show(c, item_id, data_dir):
    f = fields_of(c, item_id)
    key = rows(c, "select key from items where itemID = ?", (item_id,))[0][0]
    try:
        citekey = identity.existing_citekey(f) or key
    except identity.IdentityError as e:
        citekey = f"[conflict: {e}]"
    print(f"citekey : {citekey}")
    print(f"zotero  : {key}")
    print(f"title   : {f.get('title', '(none)')}")
    print(f"authors : {', '.join(creators_of(c, item_id)) or '(none)'}")
    print(f"date    : {f.get('date', '?')}   venue: {f.get('publicationTitle') or f.get('conferenceName') or f.get('repository') or '?'}")
    cols = collections_of(c, item_id)
    if cols:
        print(f"folders : {' / '.join(cols)}")
    tags = tags_of(c, item_id)
    if tags:
        print(f"tags    : {', '.join(tags)}")
    for p in attachments_of(c, item_id, data_dir):
        mark = "" if os.path.exists(p) else "  [missing]"
        print(f"pdf     : {p}{mark}")
    extra = f.get("extra")
    if extra:
        print(f"extra   : {extra[:200]}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("query", nargs="*")
    ap.add_argument("--citekey")
    ap.add_argument("--db", default=None)
    ap.add_argument("--data-dir", default=None)
    ap.add_argument("--pdf", action="store_true")
    ap.add_argument("--json", action="store_true", help="one structured result, with match count")
    ap.add_argument("--limit", type=int, default=8)
    ap.add_argument("--list-collections", action="store_true")
    ap.add_argument("--tags", action="store_true")
    a = ap.parse_args()
    if a.limit < 1:
        ap.error("--limit must be positive")
    if a.json and a.pdf:
        ap.error("choose --json or --pdf")

    try:
        sys.stdout.reconfigure(errors="replace")
    except Exception:
        pass

    cfg = config.load()
    if not a.db:
        a.db = config.zotero_db(cfg)
    if not a.data_dir:
        a.data_dir = config.zotero_data_dir(cfg)
    if not a.db:
        sys.exit("no Zotero database configured: set zotero.db_path in config.local.json "
                 "or the ZOTERO_DB environment variable")
    if not a.data_dir:
        sys.exit("no Zotero data directory configured: set zotero.data_dir in config.local.json "
                 "or the ZOTERO_DATA_DIR environment variable")

    try:
        with closing(connect(a.db)) as c:
            run(c, a)
    except (OSError, identity.IdentityError) as e:
        sys.exit(str(e))


def run(c, a):

    if a.list_collections:
        values = rows(c, """
                select col.collectionName, count(ci.itemID) from collections col
                left join collectionItems ci on ci.collectionID = col.collectionID
                group by col.collectionID order by 2 desc""")
        if a.json:
            print(json.dumps({"collections": [{"name": name, "count": n} for name, n in values]}, ensure_ascii=False))
            return
        for name, n in values:
            print(f"{n:4d}  {name}")
        return

    if a.tags:
        values = rows(c, """
                select t.name, count(it.itemID) from tags t
                join itemTags it on it.tagID = t.tagID
                group by t.tagID order by 2 desc limit 60""")
        if a.json:
            print(json.dumps({"tags": [{"name": name, "count": n} for name, n in values]}, ensure_ascii=False))
            return
        for name, n in values:
            print(f"{n:4d}  {name}")
        return

    if a.citekey:
        ids = [r[0] for r in by_citekey(c, a.citekey)]
    elif a.query:
        query = " ".join(a.query)
        exact = identity.resolve_items(c, query)
        ids = [r["item_id"] for r in exact] if exact else [r[0] for r in search(c, query)]
    else:
        sys.exit("give a query, --citekey, --tags or --list-collections")

    count = len(ids)
    if a.json:
        print(json.dumps({"status": "none" if count == 0 else "unique" if count == 1 else "multiple",
                          "match_count": count, "truncated": count > a.limit,
                          "matches": [item_json(c, i, a.data_dir) for i in ids[:a.limit]]},
                         ensure_ascii=False, indent=2))
        return
    if not ids:
        print("no match")
        return

    if a.pdf:
        if count > 1:
            sys.exit(f"{count} matching items; disambiguate with --json before requesting a PDF")
        for item_id in ids:
            for p in attachments_of(c, item_id, a.data_dir):
                if p.lower().endswith(".pdf") and os.path.exists(p):
                    print(p)
        return

    if count > 1:
        print(f"{count} matches; showing {min(count, a.limit)} (choose one before writing)")
    for item_id in ids[:a.limit]:
        show(c, item_id, a.data_dir)
        print()


if __name__ == "__main__":
    main()

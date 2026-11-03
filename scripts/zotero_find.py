"""Look up items in the local Zotero library.

  python zotero_find.py "attention is all you need"
  python zotero_find.py --citekey yangArbovirusSuppressionLectin2025
  python zotero_find.py "transformer" --pdf     # only PDF paths
  python zotero_find.py --list-collections
  python zotero_find.py --tags                 # tag vocabulary actually in use

Reads a copy of zotero.sqlite, so it works while Zotero is running.
"""
import argparse
import os
import shutil
import sqlite3
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import config  # noqa: E402


def connect(db):
    if not os.path.exists(db):
        sys.exit(f"zotero database not found: {db}")
    tmp = os.path.join(tempfile.gettempdir(), "zotero_find_copy.sqlite")
    shutil.copyfile(db, tmp)
    return sqlite3.connect(tmp)


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
    return [f"{a} {b}".strip() for a, b, _ in out]


def attachments_of(c, item_id, data_dir):
    out = []
    for path, item_key, link_mode in rows(c, """
            select ia.path, i.key, ia.linkMode from itemAttachments ia
            join items i on ia.itemID = i.itemID
            where ia.parentItemID = ?""", (item_id,)):
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


def search(c, query, limit):
    """Every term must match somewhere: title, citekey, author or tag."""
    terms = [t for t in query.split() if t]
    if not terms:
        return []
    block = """(exists (select 1 from itemData d
                          join fieldsCombined f on d.fieldID = f.fieldID
                          join itemDataValues v on d.valueID = v.valueID
                        where d.itemID = i.itemID
                          and f.fieldName in ('title', 'citationKey')
                          and v.value like ?)
                or exists (select 1 from itemCreators ic
                             join creators cr on ic.creatorID = cr.creatorID
                          where ic.itemID = i.itemID and cr.lastName like ?)
                or exists (select 1 from itemTags it
                             join tags t on it.tagID = t.tagID
                          where it.itemID = i.itemID and t.name like ?))"""
    cond = " and ".join([block] * len(terms))
    args = []
    for t in terms:
        args.extend([f"%{t}%"] * 3)
    args.append(limit)
    return rows(c, f"""
        select i.itemID from items i
        where i.itemID not in (select itemID from deletedItems)
          and i.itemID not in (select itemID from itemAttachments)
          and {cond}
        order by i.itemID desc limit ?""", tuple(args))


def by_citekey(c, key):
    return rows(c, """
        select d.itemID from itemData d
        join fieldsCombined f on d.fieldID = f.fieldID
        join itemDataValues v on d.valueID = v.valueID
        where f.fieldName = 'citationKey' and v.value = ?""", (key,))


def show(c, item_id, data_dir):
    f = fields_of(c, item_id)
    key = rows(c, "select key from items where itemID = ?", (item_id,))[0][0]
    print(f"citekey : {f.get('citationKey', '(none)')}")
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
    ap.add_argument("--limit", type=int, default=8)
    ap.add_argument("--list-collections", action="store_true")
    ap.add_argument("--tags", action="store_true")
    a = ap.parse_args()

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

    c = connect(a.db)

    if a.list_collections:
        for name, n in rows(c, """
                select col.collectionName, count(ci.itemID) from collections col
                left join collectionItems ci on ci.collectionID = col.collectionID
                group by col.collectionID order by 2 desc"""):
            print(f"{n:4d}  {name}")
        return

    if a.tags:
        for name, n in rows(c, """
                select t.name, count(it.itemID) from tags t
                join itemTags it on it.tagID = t.tagID
                group by t.tagID order by 2 desc limit 60"""):
            print(f"{n:4d}  {name}")
        return

    if a.citekey:
        ids = [r[0] for r in by_citekey(c, a.citekey)]
    elif a.query:
        ids = [r[0] for r in search(c, " ".join(a.query), a.limit)]
    else:
        sys.exit("give a query, --citekey, --tags or --list-collections")

    if not ids:
        print("no match")
        return

    if a.pdf:
        for item_id in ids:
            for p in attachments_of(c, item_id, a.data_dir):
                if p.lower().endswith(".pdf") and os.path.exists(p):
                    print(p)
        return

    for item_id in ids:
        show(c, item_id, a.data_dir)
        print()


if __name__ == "__main__":
    main()

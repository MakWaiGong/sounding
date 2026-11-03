"""First-run setup: ask for the few values that cannot be shipped, then write config.local.json.

  python scripts/setup.py                  # interactive, asks for what it could not detect
  python scripts/setup.py --check          # report what is still missing, change nothing
  python scripts/setup.py --zotero-key KEY --notion-db <url> --yes
                                           # non-interactive, for use from an agent

Personal values never enter the repo. config.local.json is git-ignored and is
the only thing this script writes; config.example.json keeps the shipped defaults.
"""
import argparse
import json
import os
import re
import shutil
import sqlite3
import sys
import tempfile
import urllib.error
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import config  # noqa: E402

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")


def find_zotero_db():
    """Look in the places Zotero actually puts its database on Windows."""
    home = os.path.expanduser("~")
    cands = [
        os.path.join(home, "Zotero", "zotero.sqlite"),
        os.path.join(home, "OneDrive", "Zotero", "zotero.sqlite"),
        os.path.join(home, "Documents", "Zotero", "zotero.sqlite"),
    ]
    for drive in "CDEFGH":
        root = f"{drive}:\\"
        if not os.path.exists(root):
            continue
        for sub in ("Zotero", os.path.join("document", "Zotero"),
                    os.path.join("Documents", "Zotero"), os.path.join("data", "Zotero")):
            cands.append(os.path.join(root, sub, "zotero.sqlite"))
    for p in cands:
        if os.path.exists(p) and os.path.getsize(p) > 100_000:
            return p
    return None


def inspect_db(path):
    """(ok, message) -- copy first, Zotero may hold a lock."""
    if not os.path.exists(path):
        return False, "file does not exist"
    tmp = os.path.join(tempfile.gettempdir(), "sounding_setup_copy.sqlite")
    try:
        shutil.copyfile(path, tmp)
        c = sqlite3.connect(tmp)
        n = c.execute("select count(*) from items").fetchone()[0]
        tables = {r[0] for r in c.execute("select name from sqlite_master where type='table'")}
        c.close()
    except Exception as e:
        return False, f"not a readable Zotero database ({e})"
    if "itemData" not in tables:
        return False, "no itemData table -- this is not a Zotero 6/7 database"
    return True, f"{n} items"


def verify_key(key, base="https://api.zotero.org"):
    """(user_id, privileges) or (None, error)."""
    req = urllib.request.Request(f"{base}/keys/{key}", headers={"Zotero-API-Version": "3"})
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            d = json.load(r)
        return d["userID"], d.get("access", {})
    except urllib.error.HTTPError as e:
        return None, f"HTTP {e.code} -- the key does not exist or is disabled"
    except Exception as e:
        return None, f"could not reach api.zotero.org ({e})"


def notion_id_from(text):
    """Accept a full Notion URL, a dashed uuid, or a bare 32-hex id."""
    if not text:
        return None
    hexes = re.findall(r"[0-9a-fA-F]{32}", text.replace("-", ""))
    if hexes:
        h = hexes[0]
        return f"{h[:8]}-{h[8:12]}-{h[12:16]}-{h[16:20]}-{h[20:]}"
    return None


def ask(prompt, current=None, secret=False):
    label = f"{prompt}"
    if current:
        label += " [keep current]" if secret is False else " [keep current, Enter to keep]"
    label += ": "
    val = input(label).strip()
    return val or current or ""


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--zotero-db")
    ap.add_argument("--zotero-data")
    ap.add_argument("--zotero-key")
    ap.add_argument("--notion-db", help="Papers database URL, uuid, or 32-hex id")
    ap.add_argument("--notion-ds", help="data source, collection://uuid")
    ap.add_argument("--check", action="store_true")
    ap.add_argument("--yes", action="store_true", help="accept detected values, ask nothing")
    ap.add_argument("--skip-key-check", action="store_true")
    a = ap.parse_args()

    # Start from whatever is already there so a re-run never loses values.
    cfg_path = config.config_path()
    base = {}
    if os.path.exists(config.EXAMPLE):
        with open(config.EXAMPLE, encoding="utf-8") as f:
            base.update(json.load(f))
    local_exists = os.path.exists(cfg_path)
    if local_exists:
        with open(cfg_path, encoding="utf-8") as f:
            base.update(json.load(f))

    z = base.setdefault("zotero", {})
    n = base.setdefault("notion", {})

    if a.check:
        print(f"config file : {'found' if local_exists else 'MISSING'}  {cfg_path}")
        db = z.get("db_path") or ""
        ok, msg = inspect_db(db) if db else (False, "not set")
        print(f"zotero db   : {'ok' if ok else 'BAD'}  {db or '(empty)'}  -- {msg}")
        print(f"data dir    : {z.get('data_dir') or '(empty)'}")
        if z.get("api_key"):
            uid, priv = verify_key(z["api_key"], z.get("api_base") or "https://api.zotero.org")
            print(f"api key     : {'ok' if uid else 'BAD'}  userID={uid}  {priv if not uid else ''}")
        else:
            print("api key     : not set -- Zotero write-back will fail, reading still works")
        print(f"notion db   : {n.get('papers_database_id') or '(empty)'}")
        print(f"data source : {n.get('papers_data_source_id') or '(empty)'}")
        print(f"properties  : {len(n.get('properties', {}))} mapped")
        return

    # --- Zotero database -------------------------------------------------
    db = a.zotero_db or z.get("db_path")
    detected = None if db else find_zotero_db()
    if detected:
        print(f"detected Zotero database: {detected}")
        db = detected
    if not db and not a.yes:
        db = ask("Path to zotero.sqlite")
    if not db:
        sys.exit("no Zotero database: pass --zotero-db")

    ok, msg = inspect_db(db)
    if not ok:
        sys.exit(f"cannot use {db}: {msg}")
    print(f"zotero db  : {db}  ({msg})")
    z["db_path"] = db
    data_dir = a.zotero_data or z.get("data_dir") or os.path.dirname(db)
    if not os.path.exists(os.path.join(data_dir, "storage")):
        print(f"warning: {data_dir}\\storage not found -- PDF attachment paths may not resolve")
    z["data_dir"] = data_dir

    # --- Zotero API key (optional) --------------------------------------
    key = a.zotero_key or z.get("api_key")
    if not key and not a.yes:
        print("\nZotero API key is optional: without it you can still read the library and write notes,")
        print("only the write-back (tags + Notion link) fails. Create one at")
        print("  https://www.zotero.org/settings/keys  -- tick library access, write access, notes access.")
        key = ask("Zotero API key (Enter to skip)")
    if key:
        z["api_key"] = key
        if not a.skip_key_check:
            uid, priv = verify_key(key, z.get("api_base") or "https://api.zotero.org")
            if uid:
                z["user_id"] = str(uid)
                missing = [f for f in ("library", "write") if not priv.get("user", {}).get(f)]
                print(f"api key    : ok, userID {uid}" + (f"  (missing: {', '.join(missing)})" if missing else ""))
            else:
                print(f"api key    : {priv}")
                print("             saved anyway; fix it in config.local.json when you have a working one")
    elif z.get("api_key"):
        print("api key    : keeping the existing one")

    # --- Notion ----------------------------------------------------------
    dbid = notion_id_from(a.notion_db) if a.notion_db else n.get("papers_database_id")
    if not dbid and not a.yes:
        print("\nPaste the Notion URL of the Papers database (the page that holds one row per paper).")
        dbid = notion_id_from(ask("Notion Papers database URL or id"))
    if dbid:
        n["papers_database_id"] = dbid
        print(f"notion db  : {dbid}")
    else:
        print("notion db  : not set -- notes go to notes/<citekey>.md until this is filled")

    if a.notion_ds:
        n["papers_data_source_id"] = a.notion_ds

    # --- write -----------------------------------------------------------
    with open(cfg_path, "w", encoding="utf-8") as f:
        json.dump(base, f, ensure_ascii=False, indent=2)
        f.write("\n")
    print(f"\nwritten: {cfg_path}")

    todo = []
    if not z.get("api_key"):
        todo.append("zotero.api_key -- Zotero write-back is off until this is set")
    if not n.get("papers_database_id"):
        todo.append("notion.papers_database_id -- notes stay local until this is set")
    if not n.get("papers_data_source_id"):
        todo.append("notion.papers_data_source_id -- fill from the database's collection:// url "
                    "(the Notion tools report it when you open the database)")
    print("still to do:" if todo else "everything needed is set.")
    for t in todo:
        print(f"  - {t}")


if __name__ == "__main__":
    main()

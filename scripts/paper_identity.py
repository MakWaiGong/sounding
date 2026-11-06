"""Shared, offline Zotero identity lookup and field-preserving merge helpers."""
from contextlib import closing
from pathlib import Path
import re
import sqlite3
from urllib.parse import unquote


class IdentityError(ValueError):
    """Identity is ambiguous; callers must not choose a row arbitrarily."""


def normalize_doi(value):
    value = unquote(str(value or "").strip())
    value = re.sub(r"^(?:https?://(?:dx\.)?doi\.org/|doi\s*:\s*)", "", value,
                   flags=re.I).strip().lower()
    return value if re.fullmatch(r"10\.\d{4,9}/\S+", value) else ""


def snapshot(db):
    """Return a private consistent in-memory snapshot, including committed WAL data."""
    path = Path(db).expanduser().resolve()
    if not path.is_file():
        raise FileNotFoundError(f"zotero database not found: {path}")
    dest = sqlite3.connect(":memory:")
    try:
        with closing(sqlite3.connect(path.as_uri() + "?mode=ro", uri=True,
                                    timeout=5)) as source:
            source.backup(dest)
        dest.execute("PRAGMA query_only=ON")
        return dest
    except Exception:
        dest.close()
        raise


def extra_citekeys(extra):
    return list(dict.fromkeys(m.group(1).strip() for m in re.finditer(
        r"^\s*Citation Key\s*:\s*(\S[^\r\n]*)", extra or "", re.I | re.M)))


def existing_citekey(data):
    keys = list(dict.fromkeys([k for k in [data.get("citationKey"),
                             *extra_citekeys(data.get("extra"))] if k]))
    if len(keys) > 1:
        raise IdentityError("conflicting citation keys: " + ", ".join(keys))
    return keys[0] if keys else None


def identities(c):
    records = {row[0]: {"item_id": row[0], "item_key": row[1], "fields": {}}
               for row in c.execute("""
        SELECT i.itemID, i.key FROM items i
        WHERE i.itemID NOT IN (SELECT itemID FROM deletedItems)
          AND i.itemID NOT IN (SELECT itemID FROM itemAttachments)
          AND i.itemID NOT IN (SELECT itemID FROM itemNotes)
        ORDER BY i.itemID DESC""")}
    for item_id, field, value in c.execute("""
        SELECT d.itemID, f.fieldName, v.value FROM itemData d
        JOIN fieldsCombined f ON d.fieldID=f.fieldID
        JOIN itemDataValues v ON d.valueID=v.valueID"""):
        if item_id in records:
            records[item_id]["fields"][field] = value
    return list(records.values())


def resolve_items(c, *identifiers):
    """Exact union of item key, citekey, Extra citekey and normalized DOI matches.

    Return every match, without a limit. The write path calls unique_item;
    the lookup CLI can display all candidates for the user to disambiguate.
    """
    wanted = {str(v).strip() for v in identifiers if v and str(v).strip()}
    dois = {d for v in wanted if (d := normalize_doi(v))}
    found = []
    for record in identities(c):
        fields = record["fields"]
        aliases = {record["item_key"], fields.get("citationKey"),
                   *extra_citekeys(fields.get("extra"))}
        if aliases & wanted or (dois and normalize_doi(fields.get("DOI")) in dois):
            found.append(record)
    return found


def unique_item(matches, label="identifier"):
    if len(matches) > 1:
        keys = ", ".join(str(m.get("item_key", m.get("key", "?"))) for m in matches)
        raise IdentityError(f"{label} matches {len(matches)} items ({keys}); merge or disambiguate first")
    return matches[0] if matches else None


def lookup_local(db, *identifiers):
    if not db:
        return None
    with closing(snapshot(db)) as c:
        return unique_item(resolve_items(c, *identifiers), " / ".join(
            str(v) for v in identifiers if v))


def merge_tags(existing, wanted, prefixes):
    """Replace owned families; preserve other tags, including their type metadata."""
    owned = tuple(p for p in prefixes.values() if p)
    result, names = [], set()
    for tag in existing or []:
        name = tag.get("tag", "")
        if not name or (owned and name.startswith(owned)) or name in names:
            continue
        result.append(dict(tag))
        names.add(name)
    for name in wanted:
        if name and name not in names:
            result.append({"tag": name})
            names.add(name)
    return result


def merge_extra(existing, *, citekey=None, notion=None, feishu=None):
    """Only replace explicitly supplied managed lines. Preserve all other text."""
    updates = {}
    if citekey is not None:
        updates["citation key"] = ("Citation Key", citekey)
    if notion is not None:
        updates["notion"] = ("Notion", notion)
    if feishu is not None:
        updates["feishu"] = ("Feishu", feishu)
    lines = [line for line in (existing or "").splitlines()
             if line.partition(":")[0].strip().lower() not in updates]
    lines.extend(f"{name}: {value}" for name, value in updates.values())
    return "\n".join(lines).rstrip("\n") + "\n"

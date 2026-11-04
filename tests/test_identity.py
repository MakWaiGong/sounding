"""Offline fixtures: committed WAL data, identifier aliases, and JSON lookup."""
import argparse
from contextlib import closing, redirect_stdout
import io
import json
from pathlib import Path
import sqlite3
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import citekey
import paper_identity as identity
import zotero_find


def make_library(path):
    c = sqlite3.connect(path)
    c.executescript("""
        PRAGMA journal_mode=WAL;
        CREATE TABLE items(itemID INTEGER PRIMARY KEY, key TEXT);
        CREATE TABLE deletedItems(itemID INTEGER);
        CREATE TABLE itemNotes(itemID INTEGER);
        CREATE TABLE itemAttachments(itemID INTEGER, parentItemID INTEGER, path TEXT, linkMode INTEGER);
        CREATE TABLE fieldsCombined(fieldID INTEGER PRIMARY KEY, fieldName TEXT);
        CREATE TABLE itemDataValues(valueID INTEGER PRIMARY KEY, value TEXT);
        CREATE TABLE itemData(itemID INTEGER, fieldID INTEGER, valueID INTEGER);
        CREATE TABLE creators(creatorID INTEGER, lastName TEXT, firstName TEXT);
        CREATE TABLE itemCreators(itemID INTEGER, creatorID INTEGER, orderIndex INTEGER);
        CREATE TABLE collections(collectionID INTEGER, collectionName TEXT);
        CREATE TABLE collectionItems(itemID INTEGER, collectionID INTEGER);
        CREATE TABLE tags(tagID INTEGER, name TEXT);
        CREATE TABLE itemTags(itemID INTEGER, tagID INTEGER);
        INSERT INTO items VALUES (1,'ITEM0001'), (2,'ITEM0002'), (3,'DELETED1'), (4,'NOTE0001'), (5,'PDF00001');
        INSERT INTO deletedItems VALUES (3);
        INSERT INTO itemNotes VALUES (4);
        INSERT INTO itemAttachments VALUES (5,1,'storage:paper.pdf',0);
        INSERT INTO creators VALUES (1,'Cheng','Kaiwen');
        INSERT INTO itemCreators VALUES (1,1,0);
        INSERT INTO collections VALUES (1,'sounding');
        INSERT INTO collectionItems VALUES (1,1);
        INSERT INTO tags VALUES (1,'p:dynamics');
        INSERT INTO itemTags VALUES (1,1);
    """)
    fields = ["title", "citationKey", "extra", "DOI", "date", "publicationTitle"]
    c.executemany("INSERT INTO fieldsCombined VALUES (?,?)", enumerate(fields, 1))
    records = {
        1: {"title": "Molecular Dynamics", "citationKey": "legacyKey", "extra": "Citation Key: legacyKey\nNotion: https://example.test/n",
            "DOI": "10.1234/Alpha.1", "date": "2026-01-01", "publicationTitle": "Test Journal"},
        2: {"title": "Protein Dynamics", "extra": "Citation Key: extraOnly\n", "date": "2025"},
        3: {"citationKey": "legacyKey"}, 4: {"citationKey": "legacyKey"}, 5: {"citationKey": "legacyKey"}}
    value_id = 0
    for item_id, values in records.items():
        for field, value in values.items():
            value_id += 1
            c.execute("INSERT INTO itemDataValues VALUES (?,?)", (value_id, value))
            c.execute("INSERT INTO itemData VALUES (?,?,?)", (item_id, fields.index(field) + 1, value_id))
    c.commit()
    return c


class IdentityTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "library.sqlite"
        self.writer = make_library(self.path)
        self.addCleanup(self.writer.close)

    def test_snapshot_reads_wal_and_stays_read_only(self):
        with closing(identity.snapshot(self.path)) as c:
            self.assertEqual(identity.resolve_items(c, "legacyKey")[0]["item_key"], "ITEM0001")
            self.writer.execute("UPDATE items SET key='CHANGED1' WHERE itemID=1")
            self.writer.commit()
            self.assertEqual(identity.resolve_items(c, "legacyKey")[0]["item_key"], "ITEM0001")
            with self.assertRaises(sqlite3.OperationalError):
                c.execute("DELETE FROM items")
        with closing(identity.snapshot(self.path)) as c:
            self.assertEqual(identity.resolve_items(c, "legacyKey")[0]["item_key"], "CHANGED1")

    def test_exact_aliases_exclude_deleted_notes_attachments(self):
        for alias in ("legacyKey", "ITEM0001", "doi:10.1234/ALPHA.1", "https://doi.org/10.1234/alpha.1"):
            with self.subTest(alias=alias), closing(identity.snapshot(self.path)) as c:
                self.assertEqual([r["item_id"] for r in identity.resolve_items(c, alias)], [1])
        self.assertEqual(identity.lookup_local(self.path, "extraOnly")["item_id"], 2)
        self.assertIsNone(identity.lookup_local(self.path, "absent"))

    def test_duplicate_alias_and_conflicting_identifiers_stop(self):
        with self.assertRaises(identity.IdentityError):
            identity.lookup_local(self.path, "legacyKey", "extraOnly")
        self.writer.execute("UPDATE itemDataValues SET value='Citation Key: legacyKey' WHERE value='Citation Key: extraOnly\n'")
        self.writer.commit()
        with self.assertRaises(identity.IdentityError):
            identity.lookup_local(self.path, "legacyKey")

    def test_conflicting_fields_do_not_choose_a_key(self):
        with self.assertRaises(identity.IdentityError):
            identity.existing_citekey({"citationKey": "one", "extra": "Citation Key: two"})

    def test_year_author_and_doi_search(self):
        with closing(identity.snapshot(self.path)) as c:
            self.assertEqual(zotero_find.search(c, "Cheng 2026 Dynamics"), [(1,)])
            self.assertEqual(zotero_find.search(c, "10.1234/Alpha.1"), [(1,)])

    def test_json_has_complete_record_and_explicit_match_count(self):
        args = argparse.Namespace(list_collections=False, tags=False, citekey="legacyKey",
                                  query=[], json=True, pdf=False, limit=8, data_dir=self.tmp.name)
        out = io.StringIO()
        with closing(identity.snapshot(self.path)) as c, redirect_stdout(out):
            zotero_find.run(c, args)
        payload = json.loads(out.getvalue())
        self.assertEqual(payload["status"], "unique")
        record = payload["matches"][0]
        self.assertEqual(record["citekey"], "legacyKey")
        self.assertEqual(record["authors"], ["Kaiwen Cheng"])
        self.assertEqual(record["collections"], ["sounding"])
        self.assertEqual(record["tags"], ["p:dynamics"])
        self.assertTrue(record["attachments"][0]["is_pdf"])
        self.assertFalse(record["attachments"][0]["exists"])

    def test_json_limit_does_not_claim_uniqueness(self):
        args = argparse.Namespace(list_collections=False, tags=False, citekey=None,
                                  query=["Dynamics"], json=True, pdf=False, limit=1, data_dir=self.tmp.name)
        out = io.StringIO()
        with closing(identity.snapshot(self.path)) as c, redirect_stdout(out):
            zotero_find.run(c, args)
        payload = json.loads(out.getvalue())
        self.assertEqual(payload["status"], "multiple")
        self.assertEqual(payload["match_count"], 2)
        self.assertTrue(payload["truncated"])

    def test_doi_aliases_keep_legacy_hash(self):
        self.assertEqual(citekey.derive(doi="10.1234/Alpha.1"), citekey.derive(doi="https://doi.org/10.1234/ALPHA.1"))
        import hashlib
        self.assertEqual(citekey.derive(doi="10.1234/Alpha.1"), hashlib.sha1(b"doi:101234alpha1").hexdigest()[:10])
        with self.assertRaises(ValueError):
            citekey.derive(title="Missing author and year")


if __name__ == "__main__":
    unittest.main()

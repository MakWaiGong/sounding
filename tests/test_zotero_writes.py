"""Exercise write plans with fake Zotero responses; never use a real library."""
import argparse
from contextlib import redirect_stdout
from copy import deepcopy
import io
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import paper_identity as identity
import zotero_add as add
import zotero_write as write

PREFIXES = {"problem": "p:", "method": "m:", "data": "d:"}
ITEM = {"key": "ITEM0001", "version": 1, "data": {
    "itemType": "journalArticle", "title": "Paper", "date": "2026",
    "publicationTitle": "Journal", "DOI": "10.1234/paper", "language": "zh",
    "url": "https://example.test/paper", "abstractNote": "Existing abstract",
    "extra": "Personal note\nCitation Key: legacyKey\nNotion: https://example.test/old\n",
    "tags": [{"tag": "handwritten", "type": 1}, {"tag": "p:old"}],
    "collections": ["OTHER"]}}


def arguments(**kwargs):
    args = dict(title="Paper", year=2026, venue="Journal", citekey=None, notion=None,
                tags=None, doi=None, language=None, url=None, abstract=None,
                conference_name=None, place=None, publisher=None, volume=None, pages=None)
    args.update(kwargs)
    return argparse.Namespace(**args)


class WriteTests(unittest.TestCase):
    def test_add_keeps_omitted_metadata_and_managed_lines(self):
        payload = add.update_patch(deepcopy(ITEM), arguments(title="Updated"), PREFIXES)
        self.assertEqual(payload, {"title": "Updated"})

    def test_tag_merge_preserves_manual_tags_and_their_metadata(self):
        payload = add.update_patch(deepcopy(ITEM), arguments(tags="p:new,m:model"), PREFIXES)
        self.assertEqual(payload["tags"], [{"tag": "handwritten", "type": 1},
                                          {"tag": "p:new"}, {"tag": "m:model"}])
        self.assertEqual(write.tag_names(ITEM), ["handwritten", "p:old"])

    def test_extra_replaces_only_supplied_managed_line(self):
        payload = write.write_patch(ITEM, None, "https://example.test/new", PREFIXES)
        self.assertIn("Personal note\nCitation Key: legacyKey", payload["extra"])
        self.assertIn("Notion: https://example.test/new", payload["extra"])
        self.assertNotIn("https://example.test/old", payload["extra"])

    def test_duplicate_or_conflicting_identity_stops(self):
        with self.assertRaises(identity.IdentityError):
            add.update_patch(ITEM, arguments(citekey="newHash"), PREFIXES)
        with self.assertRaises(identity.IdentityError):
            add.update_patch(ITEM, arguments(doi="10.1234/different"), PREFIXES)
        with self.assertRaises(identity.IdentityError):
            add.choose_citekey(ITEM, None, "differentCachedKey")

    def test_412_refetches_and_remerges_tags_and_extra(self):
        fresh = deepcopy(ITEM)
        fresh["version"] = 2
        fresh["data"]["tags"].append({"tag": "added concurrently", "type": 1})
        fresh["data"]["extra"] += "New personal note\n"
        request = Mock(side_effect=[(412, {}, {}), (200, {}, fresh), (204, {}, None)])
        write.patch_with_retry(request, "https://example.test/items/ITEM0001", "fake",
                               lambda item: write.write_patch(item, "p:new", "new-url", PREFIXES), deepcopy(ITEM))
        first = request.call_args_list[0].args[3]
        second = request.call_args_list[2].args[3]
        self.assertNotIn({"tag": "added concurrently", "type": 1}, first["tags"])
        self.assertIn({"tag": "added concurrently", "type": 1}, second["tags"])
        self.assertIn("New personal note", second["extra"])
        self.assertEqual(request.call_args_list[2].args[4]["If-Unmodified-Since-Version"], "2")

    def test_412_cannot_silently_change_paper_identity(self):
        fresh = deepcopy(ITEM)
        fresh["version"] = 2
        fresh["data"]["extra"] = "Citation Key: changedElsewhere"
        request = Mock(side_effect=[(412, {}, {}), (200, {}, fresh)])
        with self.assertRaises(identity.IdentityError):
            write.patch_with_retry(request, "url", "fake",
                lambda item: add.update_patch(item, arguments(title="Updated"), PREFIXES, "legacyKey"), deepcopy(ITEM))
        self.assertEqual(request.call_count, 2)

    def test_failed_refetch_never_patches(self):
        request = Mock(side_effect=[(412, {}, {}), (403, {}, {"error": "denied"})])
        with self.assertRaises(RuntimeError):
            write.patch_with_retry(request, "url", "fake", lambda item: {"title": "Changed"}, ITEM)
        self.assertEqual(request.call_count, 2)

    def test_collection_conflict_remerges_membership(self):
        fresh = deepcopy(ITEM)
        fresh["version"] = 2
        fresh["data"]["collections"].append("CONCURRENT")
        request = Mock(side_effect=[(200, {}, deepcopy(ITEM)), (412, {}, {}),
                                    (200, {}, fresh), (204, {}, None)])
        with patch.object(add, "request", request):
            self.assertIsNone(add.ensure_in_collection("base", "user", "fake", "ITEM0001", "SOUNDING"))
        self.assertEqual(request.call_args_list[-1].args[3]["collections"], ["OTHER", "CONCURRENT", "SOUNDING"])

    def test_title_lookup_pages_and_rejects_duplicates(self):
        page = [{"key": f"OTHER{i}", "data": {"title": "unrelated"}} for i in range(99)]
        page.append({"key": "ONE", "data": {"title": "Paper"}})
        request = Mock(side_effect=[(200, {}, page), (200, {}, [{"key": "TWO", "data": {"title": "Paper"}}])])
        with patch.object(add, "request", request), self.assertRaises(identity.IdentityError):
            add.api_item_by_title("Paper", "base", "user", "fake")
        self.assertIn("start=100", request.call_args_list[-1].args[1])

    def run_dry_add(self, current=None, local=None):
        cfg = {"zotero": {"db_path": "", "user_id": "fixture", "api_key": "fake",
                           "collection": "sounding"}, "zotero_tag_prefix": PREFIXES}
        with tempfile.TemporaryDirectory() as tmp:
            pdf = Path(tmp) / "paper.pdf"
            pdf.write_bytes(b"%PDF-fixture")
            argv = ["zotero_add.py", "--pdf", str(pdf), "--title", "Paper", "--authors", "A Person",
                    "--year", "2026", "--venue", "Journal", "--doi", "10.1234/paper", "--dry-run"]
            request = Mock(return_value=(200, {}, current if current else []))
            out = io.StringIO()
            with patch.object(sys, "argv", argv), patch.object(add.config, "load", return_value=cfg), \
                 patch.object(add.identity, "lookup_local", return_value=local), \
                 patch.object(add, "request", request), patch.object(add, "resolve_collection") as collection, \
                 redirect_stdout(out):
                add.main()
            self.assertTrue(all(call.args[0] == "GET" for call in request.call_args_list))
            collection.assert_not_called()
            return json.loads(out.getvalue().split("\n", 1)[1])

    def test_create_dry_run_never_creates_collection_or_item(self):
        plan = self.run_dry_add()
        self.assertEqual(plan["operation"], "create")
        self.assertEqual(plan["collection"], "sounding")
        self.assertNotIn("collections", plan["payload"])

    def test_existing_citekey_is_kept_instead_of_derived_hash(self):
        plan = self.run_dry_add(deepcopy(ITEM), {"item_key": "ITEM0001", "fields": {"citationKey": "legacyKey"}})
        self.assertEqual(plan["operation"], "update")
        self.assertEqual(plan["citekey"], "legacyKey")
        self.assertNotIn("extra", plan["payload"])

    def test_local_key_survives_when_api_extra_has_no_key(self):
        item = deepcopy(ITEM)
        item["data"]["extra"] = "Personal note\nNotion: old-url\n"
        plan = self.run_dry_add(item, {"item_key": "ITEM0001", "fields": {"citationKey": "legacyKey"}})
        self.assertIn("Citation Key: legacyKey", plan["payload"]["extra"])
        self.assertIn("Notion: old-url", plan["payload"]["extra"])


if __name__ == "__main__":
    unittest.main()

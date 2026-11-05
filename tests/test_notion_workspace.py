import json
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import notion_workspace


class NotionWorkspaceTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.path = self.root / "workspace.json"
        self.cfg = {"notion": {
            "papers_database_id": "db-a",
            "papers_data_source_id": "collection://ds-a",
            "properties": {"title": "标题", "citekey": "citekey"},
            "option_values": {"type": ["journal", "other"]},
        }}
        self.receipt = {
            "database_id": "db-a", "data_source_id": "collection://ds-a",
            "probe_page_id": "page-probe", "read_checked": True,
            "write_checked": True, "update_checked": True, "cleanup_checked": True,
        }

    def test_record_makes_probe_valid_for_same_workspace(self):
        result = notion_workspace.record(self.path, self.cfg, self.receipt)
        self.assertTrue(result["valid"])
        self.assertTrue(notion_workspace.status(self.path, self.cfg)["valid"])

    def test_config_change_invalidates_cached_probe(self):
        notion_workspace.record(self.path, self.cfg, self.receipt)
        changed = json.loads(json.dumps(self.cfg))
        changed["notion"]["papers_data_source_id"] = "collection://ds-b"
        result = notion_workspace.status(self.path, changed)
        self.assertFalse(result["valid"])
        self.assertEqual(result["reason"], "workspace_config_changed")

    def test_observed_remote_failure_invalidates_probe(self):
        notion_workspace.record(self.path, self.cfg, self.receipt)
        result = notion_workspace.invalidate(self.path, "OAuth expired")
        self.assertFalse(result["valid"])
        self.assertEqual(result["reason"], "OAuth expired")

    def test_incomplete_or_wrong_probe_is_rejected(self):
        broken = dict(self.receipt, cleanup_checked=False)
        with self.assertRaisesRegex(ValueError, "cleanup_checked"):
            notion_workspace.record(self.path, self.cfg, broken)
        wrong = dict(self.receipt, database_id="db-b")
        with self.assertRaisesRegex(ValueError, "does not match"):
            notion_workspace.record(self.path, self.cfg, wrong)


if __name__ == "__main__":
    unittest.main()

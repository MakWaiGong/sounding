import contextlib
import io
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import config
import check_config


def fixture_config():
    return {
        "notion": {
            "properties": {key: key for key in check_config.REQUIRED},
            "option_values": {"type": ["conference", "journal", "review", "other"]},
        },
        "zotero_tag_prefix": {"problem": "p:", "method": "m:", "data": "d:"},
    }


def fixture_schema():
    fields = {k: {"type": check_config.FIELD_TYPES[k]} for k in check_config.REQUIRED}
    fields["type"]["select"] = {"options": [{"name": n} for n in fixture_config()["notion"]["option_values"]["type"]]}
    return {"properties": fields}


class ConfigTests(unittest.TestCase):
    def test_partial_local_sections_keep_defaults_and_explicit_empty(self):
        with tempfile.TemporaryDirectory() as tmp:
            default = Path(tmp) / "defaults.json"
            local = Path(tmp) / "local.json"
            default.write_text(json.dumps({"zotero": {"collection": "sounding", "api_base": "base"}}), encoding="utf-8")
            local.write_text(json.dumps({"zotero": {"collection": "", "db_path": "my.db"}}), encoding="utf-8")
            with patch.object(config, "EXAMPLE", str(default)):
                merged = config.load(local)
            self.assertEqual(merged["zotero"], {"collection": "", "api_base": "base", "db_path": "my.db"})

    def test_environment_precedence_and_no_implicit_local_load(self):
        with patch.dict(os.environ, {"ZOTERO_DB": "override.db"}, clear=True):
            self.assertEqual(config.zotero_db({"zotero": {"db_path": "file.db"}}), "override.db")
        with patch.object(config, "load", side_effect=AssertionError("unexpected config access")):
            self.assertEqual(config.tag_prefixes({}), {})

    def test_missing_explicit_config_is_an_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(FileNotFoundError):
                config.load(Path(tmp) / "missing.json")

    def test_offline_cli_never_requests_network(self):
        with patch.object(config, "load", return_value=fixture_config()), patch.object(check_config, "notion_headers", side_effect=AssertionError("network")):
            stream = io.StringIO()
            with contextlib.redirect_stdout(stream):
                self.assertEqual(check_config.main(["--json"]), 0)
            self.assertEqual(json.loads(stream.getvalue())["scope"], "offline")

    def test_schema_types_options_and_required_fields(self):
        cfg = fixture_config()
        schema = fixture_schema()
        self.assertEqual(check_config.validate(cfg, schema), [])
        schema["properties"]["year"]["type"] = "rich_text"
        schema["properties"]["type"]["select"]["options"] = []
        del schema["properties"]["venue"]
        issues = " ".join(check_config.validate(cfg, schema))
        self.assertIn("expected number", issues)
        self.assertIn("conference", issues)
        self.assertIn("missing field 'venue'", issues)

    def test_empty_schema_and_duplicate_mappings_fail(self):
        cfg = fixture_config()
        self.assertTrue(check_config.validate(cfg, {"properties": {}}))
        cfg["notion"]["properties"]["venue"] = "title"
        self.assertIn("share field", " ".join(check_config.validate(cfg)))

    def test_schema_file_needs_no_notion_token(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "schema.json"
            path.write_text(json.dumps(fixture_schema()), encoding="utf-8")
            with patch.object(config, "load", return_value=fixture_config()), patch.dict(os.environ, {}, clear=True):
                with contextlib.redirect_stdout(io.StringIO()):
                    self.assertEqual(check_config.main(["--schema-json", str(path)]), 0)


if __name__ == "__main__":
    unittest.main()

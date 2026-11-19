import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import publish

NOTE = """标题：SparseLA：树突稀疏衰减矩阵的线性注意力
发表：Nature Communications (2026)
类型：science
citekey：20861cd95b

本地 PDF：`D:\\论文\\a.pdf`
Zotero：待建

---

1. 一句话：甲。
10. 去留：值得跟进。

---

## 第 1 段 痛点

正文。
"""


class PublishNoteTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.path = Path(self.temporary.name) / "20861cd95b.md"
        self.path.write_text(NOTE, encoding="utf-8")

    def test_front_matter_is_reused_verbatim(self):
        props = publish.parse_note(str(self.path))
        self.assertEqual(props["citekey"], "20861cd95b")
        self.assertEqual(props["venue"], "Nature Communications")
        self.assertEqual(props["year"], 2026)
        self.assertEqual(props["type"], "science")
        self.assertEqual(props["verdict"], "值得跟进")

    def test_venue_keeps_inner_parentheses(self):
        self.path.write_text(NOTE.replace("Nature Communications (2026)", "Cell (2003) (2026)"),
                             encoding="utf-8")
        props = publish.parse_note(str(self.path))
        self.assertEqual(props["venue"], "Cell (2003)")

    def test_year_falls_back_to_its_own_line(self):
        self.path.write_text(NOTE.replace("Nature Communications (2026)", "Nature")
                             .replace("citekey：20861cd95b", "年份：2024\ncitekey：20861cd95b"),
                             encoding="utf-8")
        props = publish.parse_note(str(self.path))
        self.assertIsNone(props["venue"] and None)
        self.assertEqual(props["year"], 2024)

    def test_missing_required_field_is_named_not_dropped(self):
        self.path.write_text(NOTE.replace("类型：science\n", ""), encoding="utf-8")
        props = publish.parse_note(str(self.path))
        tags = {"data": []}
        gaps = publish.missing(props, tags)
        self.assertEqual(sorted(gaps), ["data", "type"])
        report = publish.report_missing(gaps)
        self.assertIn("数据类型", report)
        self.assertIn("类型", report)

    def test_record_fields_skip_empty_values(self):
        props = publish.parse_note(str(self.path))
        tags = {"data": ["PDB"], "read": "精读"}
        fields = publish.record_fields(props, tags, "https://x/docx/1")
        self.assertEqual(fields["数据类型"], ["PDB"])
        self.assertNotIn("方法族", fields)
        self.assertNotIn("问题域", fields)
        self.assertNotIn("去留", fields)
        self.assertNotIn("置信度", fields)
        self.assertEqual(fields["笔记"], {"link": "https://x/docx/1", "text": "笔记"})

    def test_doi_is_taken_from_the_source_line(self):
        self.path.write_text(
            NOTE + "\n## 定位素材\n\n原文：A paper · Nature Communications 2026 · "
                   "DOI 10.1038/s41467-026-77929-8\n",
            encoding="utf-8")
        props = publish.parse_note(str(self.path))
        self.assertEqual(props["doi"], "10.1038/s41467-026-77929-8")
        fields = publish.record_fields(props, {"data": ["PDB"]}, None)
        self.assertEqual(fields["DOI"], "10.1038/s41467-026-77929-8")

    def test_missing_doi_is_not_a_stop(self):
        props = publish.parse_note(str(self.path))
        self.assertIsNone(props["doi"])
        self.assertEqual(sorted(publish.missing(props, {"data": ["PDB"]})), [])

    def test_rev_changes_with_content(self):
        first = publish.parse_note(str(self.path))["rev"]
        self.path.write_text(NOTE.replace("正文。", "正文，改过。"), encoding="utf-8")
        self.assertNotEqual(first, publish.parse_note(str(self.path))["rev"])

    def test_migration_line_goes_under_the_source_heading(self):
        text = NOTE + "\n## 定位素材\n\n原文：某文 · Nature 2020 · DOI 10.1038/x\n"
        out = publish.mark_migrated(text, "Notion", "飞书", today="2026-11-07")
        self.assertIn("迁移：2026-11-07 从 Notion 迁入；主后端 = 飞书", out)
        # directly under the heading, before 原文
        self.assertLess(out.index("## 定位素材"), out.index("迁移："))
        self.assertLess(out.index("迁移："), out.index("原文："))

    def test_migration_line_is_rewritten_not_stacked(self):
        text = NOTE + "\n## 定位素材\n\n迁移：2026-01-01 从 飞书 迁入；主后端 = 飞书\n"
        first = publish.mark_migrated(text, "Notion", "飞书", today="2026-11-07")
        second = publish.mark_migrated(first, "Notion", "飞书", today="2026-11-08")
        self.assertEqual(second.count("迁移："), 1)
        self.assertIn("2026-11-08", second)

    def test_migration_line_creates_the_section_when_absent(self):
        out = publish.mark_migrated(NOTE, "飞书", "飞书", today="2026-11-07")
        self.assertIn("## 定位素材", out)
        self.assertIn("迁移：2026-11-07 从 飞书 迁入；主后端 = 飞书", out)


    def test_manual_reading_and_domain_are_never_sent(self):
        props = publish.parse_note(str(self.path))
        tags = {"data": ["PDB"], "read": "精读", "domain": ["manual-domain"]}
        fields = publish.record_fields(props, tags, "https://x/docx/1")
        self.assertNotIn("读法", fields)
        self.assertNotIn("领域", fields)
        self.assertNotIn("domain", fields)
        self.assertEqual(fields["数据类型"], ["PDB"])


    def test_legacy_year_maps_to_renamed_text_fields(self):
        props = publish.parse_note(str(self.path))
        fields = publish.record_fields(props, {"data": ["PDB"]}, "url")
        self.assertEqual(fields["期刊"], props["venue"])
        self.assertEqual(fields["发表时间"], str(props["year"]))
        self.assertNotIn("发表", fields)
        self.assertNotIn("年份", fields)

    def test_explicit_year_month_is_preserved(self):
        year = publish.parse_note(str(self.path))["year"]
        lines = self.path.read_text(encoding="utf-8").splitlines()
        lines.insert(4, f"发表时间：{year}-07")
        self.path.write_text(chr(10).join(lines), encoding="utf-8")
        props = publish.parse_note(str(self.path))
        self.assertEqual(props["publication_time"], f"{year}-07")
        self.assertEqual(publish.record_fields(props, {}, "url")["发表时间"], f"{year}-07")

    def test_new_venue_header_is_supported(self):
        text = self.path.read_text(encoding="utf-8")
        expected = publish.parse_note(str(self.path))["venue"]
        self.path.write_text(text.replace("发表：", "期刊："), encoding="utf-8")
        self.assertEqual(publish.parse_note(str(self.path))["venue"], expected)

    def test_invalid_publication_month_is_rejected(self):
        for value in ("2026-00", "2026-13", "2026-1", "2026-07-01", "invalid"):
            with self.subTest(value=value), self.assertRaises(ValueError):
                publish.normalize_publication_time(value)

    def test_conflicting_publication_year_is_rejected(self):
        year = publish.parse_note(str(self.path))["year"]
        lines = self.path.read_text(encoding="utf-8").splitlines()
        lines.insert(4, f"发表时间：{year - 1}-07")
        self.path.write_text(chr(10).join(lines), encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "conflicts"):
            publish.parse_note(str(self.path))

    def test_schema_publication_time_is_text(self):
        import feishu_schema
        fields = dict(feishu_schema.PAPER_FIELDS)
        self.assertEqual(fields["期刊"], feishu_schema.TEXT)
        self.assertEqual(fields["发表时间"], feishu_schema.TEXT)
        self.assertNotIn("年份", fields)


    def test_year_only_update_preserves_existing_month(self):
        fields = publish.preserve_publication_time({"发表时间": "2026", "期刊": "ICML"}, {"发表时间": "2026-07"})
        self.assertNotIn("发表时间", fields)
        self.assertEqual(fields["期刊"], "ICML")

    def test_explicit_month_update_is_not_dropped(self):
        fields = publish.preserve_publication_time({"发表时间": "2026-08"}, {"发表时间": "2026-07"})
        self.assertEqual(fields["发表时间"], "2026-08")

    def test_existing_month_with_conflicting_year_is_not_overwritten(self):
        with self.assertRaisesRegex(ValueError, "conflicts"):
            publish.preserve_publication_time({"发表时间": "2025"}, {"发表时间": "2026-07"})


    def test_text_classification_matches_schema(self):
        import feishu_schema
        expected = {name for name, ftype in feishu_schema.PAPER_FIELDS if ftype == feishu_schema.TEXT}
        self.assertEqual(feishu_schema.TEXT_FIELDS, expected)
        self.assertTrue({"期刊", "发表时间"}.issubset(feishu_schema.TEXT_FIELDS))

    def test_legacy_venue_suffix_can_include_month(self):
        text = self.path.read_text(encoding="utf-8")
        self.path.write_text(text.replace("(2026)", "(2026-07)", 1), encoding="utf-8")
        props = publish.parse_note(str(self.path))
        self.assertEqual(props["publication_time"], "2026-07")
        self.assertEqual(props["venue"], "Nature Communications")

    def test_year_conflict_stops_before_remote_document_changes(self):
        from contextlib import ExitStack, redirect_stderr
        from io import StringIO
        from unittest.mock import patch, MagicMock
        client = MagicMock()
        workspace = {"app_token": "app", "folder_token": "folder", "tables": {"papers": "table"}}
        rows = [{"record_id": "record", "fields": {"发表时间": "2025-07"}}]
        with ExitStack() as stack:
            stack.enter_context(redirect_stderr(StringIO()))
            stack.enter_context(patch.object(sys, "argv", ["publish.py", str(self.path), "--data", "PDB"]))
            stack.enter_context(patch.object(publish.config, "feishu", return_value={}))
            stack.enter_context(patch.object(publish, "load_json", side_effect=[workspace, {}]))
            stack.enter_context(patch.object(publish.fapi, "client_from_config", return_value=client))
            stack.enter_context(patch.object(publish, "find_papers", return_value=rows))
            body = stack.enter_context(patch.object(publish.fdoc, "replace_body"))
            with self.assertRaisesRegex(ValueError, "conflicts"):
                publish.main()
        client.create_doc.assert_not_called()
        body.assert_not_called()
        client.update_record.assert_not_called()
        client.create_record.assert_not_called()


if __name__ == "__main__":
    unittest.main(verbosity=2)

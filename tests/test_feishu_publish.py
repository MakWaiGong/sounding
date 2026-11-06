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


if __name__ == "__main__":
    unittest.main(verbosity=2)

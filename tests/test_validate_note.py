import hashlib
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import validate_note


def note_text(paper_type="algorithm", digest_count=10):
    headings = {
        "algorithm": ["1 科学故事：具体问题", "2 灵感", "3 具体算法", "4 基线对比"],
        "science": ["1 痛点", "2 空白与核心新发现", "3 论证链"],
        "review": ["1 它想改什么惯例", "2 覆盖面与边界", "3 评价方法本身", "4 我能拿它做什么"],
        "other": ["1 它想改什么惯例", "2 覆盖面与边界", "3 评价方法本身", "4 我能拿它做什么"],
    }
    text = f"标题：测试论文\n发表：Example (2026)\n类型：{paper_type}\ncitekey：paper-key\n\n"
    text += "本地 PDF：`paper.pdf`\nZotero：`zotero://select/library/items/ITEM1234`\n\n"
    text += "\n".join(f"{i}. 具体速览内容。" for i in range(1, digest_count + 1)) + "\n\n"
    text += "\n\n".join("## " + h + "\n具体内容。" for h in headings[paper_type])
    return text + "\n\n## 与已读的关系\n无已读相关；在 Papers 中检索测试主题。\n\n## 待查\n核对附录表。\n"


class ValidateNoteTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.path = self.root / "note.md"
        self.path.write_text(note_text(), encoding="utf-8")
        self.manifest = {"pdf_sha256": "a" * 64, "page_count": 4,
                         "text_layer_status": "present",
                         "pages": [{"page": 1, "captions": [
                             {"kind": "figure", "figure_number": "1", "label": "Figure 1"},
                             {"kind": "table", "figure_number": "2", "label": "Table 2"}]}]}

    def validate(self, text=None, **kwargs):
        if text is not None:
            self.path.write_text(text, encoding="utf-8")
        return validate_note.validate_note(self.path, **kwargs)

    def test_all_templates_and_hash(self):
        for kind in ("algorithm", "science", "review", "other"):
            with self.subTest(kind=kind):
                report = self.validate(note_text(kind), source_manifest=self.manifest)
                self.assertTrue(report["valid"], report["errors"])
                self.assertEqual(report["schema_version"], 2)
                self.assertEqual(report["source_sha256"], "a" * 64)
                self.assertEqual(report["note_sha256"], hashlib.sha256(self.path.read_bytes()).hexdigest())

    def test_wrong_digest_and_citekey(self):
        report = self.validate(note_text(digest_count=9), expected_citekey="other")
        self.assertEqual({e["code"] for e in report["errors"]},
                         {"digest_line_count", "digest_numbering", "citekey_mismatch"})

    def test_explicit_digest_heading_and_bold_metadata(self):
        text = note_text().replace("标题：", "**标题**：").replace("1. 具体速览", "## 10 行速览\n\n1. 具体速览", 1)
        self.assertTrue(self.validate(text)["valid"])

    def test_missing_empty_and_out_of_order_sections(self):
        text = note_text().replace("## 2 灵感", "## 2 其他").replace("核对附录表。", "")
        report = self.validate(text)
        self.assertFalse(report["valid"])
        self.assertTrue({"template_section", "empty_section"} <= {e["code"] for e in report["errors"]})
        text = note_text().replace("无已读相关；在 Papers 中检索测试主题。", "| citekey | 关系 | 差在哪 |\n| --- | --- | --- |")
        self.assertIn("empty_section", [e["code"] for e in self.validate(text)["errors"]])

    def test_local_remote_and_reference_images(self):
        image = self.root / "figure (draft).png"
        image.write_bytes(b"figure")
        text = note_text() + '\n![Figure 1](<figure (draft).png>)\n![remote](https://example.org/fig.png)\n![extra][asset]\n[asset]: <figure%20(draft).png>\n'
        report = self.validate(text)
        self.assertTrue(report["valid"], report["errors"])
        self.assertEqual(len(report["images"]["local"]), 2)
        self.assertEqual(len(report["images"]["remote"]), 1)
        image.unlink()
        self.assertIn("image_missing", [e["code"] for e in self.validate()["errors"]])

    def test_unknown_figures_warn_invalid_pages_fail(self):
        report = self.validate(note_text() + "\nFig. 1a 和 Table 2，参见 page 4。\n",
                               source_manifest=self.manifest)
        self.assertTrue(report["valid"])
        self.assertEqual(report["warnings"], [])
        report = self.validate(note_text() + "\nFig. S2，参见第 5 页。\n", source_manifest=self.manifest)
        self.assertIn("source_reference_unlocated", [x["code"] for x in report["warnings"]])
        self.assertIn("page_out_of_range", [x["code"] for x in report["errors"]])

    def test_fenced_examples_do_not_count_as_real_evidence_or_sections(self):
        text = note_text() + "\n```markdown\n![missing](not-here.png)\nFig. 999 page 100\n```\n"
        report = self.validate(text, source_manifest=self.manifest)
        self.assertTrue(report["valid"])
        self.assertEqual(report["warnings"], [])

    def test_no_text_layer_blocks_sounding(self):
        self.manifest["text_layer_status"] = "absent"
        self.assertFalse(self.validate(source_manifest=self.manifest)["valid"])

    def test_cli_report_exit_and_utf8(self):
        output = self.root / "report.json"
        script = Path(validate_note.__file__)
        result = subprocess.run([sys.executable, "-X", "utf8", str(script), str(self.path),
                                 "--output", str(output)], capture_output=True, encoding="utf-8")
        self.assertEqual(result.returncode, 0, result.stderr)
        report = json.loads(output.read_text(encoding="utf-8"))
        self.assertEqual(report["note_sha256"], json.loads(result.stdout)["note_sha256"])
        self.path.write_text("incomplete note", encoding="utf-8")
        result = subprocess.run([sys.executable, "-X", "utf8", str(script), str(self.path)],
                                capture_output=True, encoding="utf-8")
        self.assertEqual(result.returncode, 1)


if __name__ == "__main__":
    unittest.main()

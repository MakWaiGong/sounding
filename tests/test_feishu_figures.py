import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import feishu_doc


class OrphanFigureTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.note = self.root / "abc123.md"
        self.note.write_text("## 定位素材\n\n正文。\n", encoding="utf-8")
        self.figs = self.root / "figs" / "abc123"
        self.figs.mkdir(parents=True)

    def test_missing_manifest_with_crops_is_reported(self):
        (self.figs / "fig1.png").write_bytes(b"png")
        found = feishu_doc.orphan_figure_files(str(self.note))
        self.assertEqual([Path(p).name for p in found], ["fig1.png"])

    def test_manifest_present_is_not_an_orphan(self):
        (self.figs / "fig1.png").write_bytes(b"png")
        (self.figs / "manifest.json").write_text(
            json.dumps({"citekey": "abc123", "figures": []}), encoding="utf-8"
        )
        self.assertEqual(feishu_doc.orphan_figure_files(str(self.note)), [])

    def test_no_figure_folder_is_not_an_orphan(self):
        self.assertEqual(feishu_doc.orphan_figure_files(str(self.note)), [])


if __name__ == "__main__":
    unittest.main()

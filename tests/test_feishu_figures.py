import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

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


class ManifestFigureTests(OrphanFigureTests):
    def manifest(self, figures):
        (self.figs / "manifest.json").write_text(
            json.dumps({"citekey": "abc123", "figures": figures}), encoding="utf-8")

    def test_missing_manifest_figure_is_not_silently_omitted(self):
        self.manifest([{"label": "fig1", "page": 2}])
        with self.assertRaisesRegex(ValueError, "missing or unreadable"):
            feishu_doc.note_figures(str(self.note))

    def test_invalid_duplicate_and_non_mapping_manifest_entries_fail(self):
        crop = self.figs / "fig1.png"
        crop.write_bytes(b"png")
        cases = [
            ([{"label": "fig1junk"}], "label"),
            ([{"label": "fig1"}, {"label": "figure_01"}], "duplicate"),
            ([{"label": "not-a-figure"}], "label"),
            ([{"label": "../fig1"}], "label"),
            ([{"label": None}], "label"),
            (["fig1"], "entry"),
            ([{"label": "fig1", "local_path": []}], "path"),
        ]
        for figures, message in cases:
            with self.subTest(figures=figures):
                self.manifest(figures)
                with self.assertRaisesRegex(ValueError, message):
                    feishu_doc.note_figures(str(self.note))

    def test_unreadable_figure_is_rejected_without_fallback(self):
        crop = self.figs / "fig1.png"
        crop.write_bytes(b"png")
        self.manifest([{"label": "fig1", "local_path": str(crop)}])
        real_open = open
        def denied(path, mode="r", *args, **kwargs):
            if str(path) == str(crop) and mode == "rb":
                raise PermissionError("fixture unreadable")
            return real_open(path, mode, *args, **kwargs)
        with patch("builtins.open", side_effect=denied):
            with self.assertRaisesRegex(ValueError, "missing or unreadable"):
                feishu_doc.note_figures(str(self.note))

    def test_unreadable_manifest_and_note_do_not_become_empty_figures(self):
        self.manifest([])
        manifest = self.figs / "manifest.json"
        real_open = open
        for blocked, message in ((manifest, "manifest is unreadable"),
                                 (self.note, "note is missing or unreadable")):
            with self.subTest(blocked=blocked):
                def denied(path, mode="r", *args, **kwargs):
                    if str(path) == str(blocked):
                        raise PermissionError("fixture unreadable")
                    return real_open(path, mode, *args, **kwargs)
                with patch("builtins.open", side_effect=denied):
                    with self.assertRaisesRegex(ValueError, message):
                        feishu_doc.note_figures(str(self.note))

    def test_stale_absolute_path_falls_back_to_local_crop(self):
        crop = self.figs / "fig1.png"
        crop.write_bytes(b"png")
        self.note.write_text("图注：Fig. 1 —— Explanation\n", encoding="utf-8")
        self.manifest([{"label": "fig1", "local_path": str(self.root / "other" / "fig1.png"), "page": 3}])
        self.assertEqual(feishu_doc.note_figures(str(self.note)),
                         [(str(crop), "Fig. 1　Explanation（原 PDF 第 3 页）", "fig1")])

    def test_malformed_manifest_root_does_not_imply_no_figures(self):
        manifest = self.figs / "manifest.json"
        for data in ({}, {"figures": None}, {"figures": "fig1"}, [],
                     {"citekey": "different", "figures": []}):
            with self.subTest(data=data):
                manifest.write_text(json.dumps(data), encoding="utf-8")
                with self.assertRaisesRegex(ValueError, "manifest"):
                    feishu_doc.note_figures(str(self.note))


if __name__ == "__main__":
    unittest.main()

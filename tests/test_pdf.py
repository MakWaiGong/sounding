"""PDF tests use only temporary synthetic documents; no user PDFs or services."""

import contextlib
import hashlib
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

try:  # keep the same module object the scripts patch
    import pymupdf as fitz
except ImportError:
    import fitz

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))
import fig
import pdf_prepare


class PDFTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="sounding-pdf-test-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.pdf = self.root / "paper.pdf"
        self.cache = self.root / "cache"
        self.outputs = self.root / "figures"
        self.caption = (
            "Figure 2: A complete caption longer than ninety characters. "
            "The red rectangle represents the measured input. "
            "The complete final sentence must survive extraction and caching."
        )
        with fitz.open() as document:
            page = document.new_page(width=300, height=400)
            page.insert_text((20, 30), "1 Introduction", fontsize=16)
            page.insert_text((20, 60), "A baseline with two modules.", fontsize=10)
            page.draw_rect(fitz.Rect(30, 100, 210, 180), color=(1, 0, 0), fill=(1, 0, 0))
            self.assertGreaterEqual(page.insert_textbox(fitz.Rect(30, 200, 270, 320), self.caption,
                                                       fontsize=9), 0)
            page = document.new_page(width=300, height=400)
            pixmap = fitz.Pixmap(fitz.csRGB, fitz.IRect(0, 0, 40, 40))
            pixmap.clear_with(180)
            page.insert_image(fitz.Rect(20, 20, 140, 140), stream=pixmap.tobytes("png"))
            page = document.new_page(width=300, height=400)
            page.insert_text((20, 30), "2 Experiments", fontsize=16)
            page.insert_text((20, 70), "Ablation improves the baseline.", fontsize=10)
            page.set_rotation(90)
            document.save(self.pdf)
        self.original = self.pdf.read_bytes()
        self.digest = hashlib.sha256(self.original).hexdigest()

    def prepare(self, **kwargs):
        return pdf_prepare.prepare_pdf(self.pdf, self.cache, **kwargs)

    def plan(self, *entries):
        return {"pdf_sha256": self.digest, "crops": list(entries) or [self.crop()]}

    def crop(self, **changes):
        entry = {"page": 1, "figure_number": "2", "bbox": [30, 100, 210, 180],
                 "out": "fig2.png", "dpi": 72, "pad": 0}
        entry.update(changes)
        return entry

    def cli(self, script, *args):
        environment = dict(os.environ, PYTHONUTF8="1")
        return subprocess.run([sys.executable, str(SCRIPTS / script), *map(str, args)],
                              capture_output=True, text=True, encoding="utf-8", env=environment)

    def test_cache_hit_does_not_parse_and_preserves_source(self):
        first = self.prepare()
        with mock.patch.object(pdf_prepare, "_extract", side_effect=AssertionError("parsed again")):
            second = self.prepare()
            hits = pdf_prepare.search_manifest(second, "baseline")
        self.assertFalse(first["cache"]["hit"])
        self.assertTrue(second["cache"]["hit"])
        self.assertEqual(first["pages"], second["pages"])
        self.assertEqual([hit["page"] for hit in hits], [1, 3])
        self.assertEqual(self.pdf.read_bytes(), self.original)
        self.assertTrue(pdf_prepare._valid_cache(second, self.digest))

    def test_cache_rebuilds_after_pdf_bytes_change(self):
        first = self.prepare()
        with fitz.open(stream=self.original, filetype="pdf") as document:
            document[0].insert_text((20, 370), "New version")
            changed = document.tobytes()
        self.pdf.write_bytes(changed)
        second = self.prepare()
        self.assertFalse(second["cache"]["hit"])
        self.assertNotEqual(first["pdf_sha256"], second["pdf_sha256"])
        self.assertNotEqual(first["cache"]["path"], second["cache"]["path"])
        self.assertIn("New version", second["pages"][0]["text"])

    def test_extractor_and_library_versions_invalidate_cache(self):
        first = self.prepare()
        with mock.patch.object(pdf_prepare, "EXTRACTOR_VERSION", "test-next"):
            changed = self.prepare()
        self.assertFalse(changed["cache"]["hit"])
        self.assertNotEqual(changed["cache"]["path"], first["cache"]["path"])
        with mock.patch.object(fitz, "VersionBind", "test-library"):
            changed = self.prepare()
        self.assertFalse(changed["cache"]["hit"])
        self.assertEqual(changed["pymupdf_version"], "test-library")

    def test_corrupt_and_stale_cache_rebuild(self):
        first = self.prepare()
        cache_file = Path(first["cache"]["path"])
        for corruption in ("broken JSON", '{"pages": []}', "\ufeffnot valid"):
            with self.subTest(corruption=corruption):
                cache_file.write_text(corruption, encoding="utf-8")
                self.assertFalse(self.prepare()["cache"]["hit"])
        payload = json.loads(cache_file.read_text(encoding="utf-8"))
        payload["pages"][0]["text"] = "silently damaged content"
        cache_file.write_text(json.dumps(payload), encoding="utf-8")
        rebuilt = self.prepare()
        self.assertFalse(rebuilt["cache"]["hit"])
        self.assertIn("Introduction", rebuilt["pages"][0]["text"])
        payload = json.loads(cache_file.read_text(encoding="utf-8"))
        payload["schema_version"] = -1
        cache_file.write_text(json.dumps(payload), encoding="utf-8")
        self.assertFalse(self.prepare()["cache"]["hit"])
        self.assertFalse(self.prepare(rebuild=True)["cache"]["hit"])

    def test_identical_bytes_share_cache_across_paths(self):
        self.prepare()
        copied = self.root / "renamed.pdf"
        copied.write_bytes(self.original)
        result = pdf_prepare.prepare_pdf(copied, self.cache)
        self.assertTrue(result["cache"]["hit"])
        self.assertEqual(result["source_path"], str(copied.resolve()))

    def test_page_ranges_search_and_full_caption(self):
        manifest = self.prepare()
        self.assertEqual(manifest["page_count"], 3)
        self.assertEqual(manifest["text_layer_status"], "partial")
        self.assertEqual(manifest["pages"][1]["text_layer_status"], "absent")
        caption = manifest["pages"][0]["captions"][0]
        self.assertEqual(" ".join(caption["text"].split()), self.caption)
        self.assertGreater(len(caption["text"]), 90)
        self.assertEqual(caption["figure_number"], "2")
        self.assertEqual(caption["page"], 1)
        self.assertEqual(caption["pdf_sha256"], self.digest)
        self.assertEqual(manifest["pages"][0]["sections"][0]["title"], "1 Introduction")
        self.assertEqual([p["page"] for p in pdf_prepare.select_pages(manifest, "3,1-2")], [1, 2, 3])
        hits = pdf_prepare.search_manifest(manifest, ["BASELINE", "absent-term"], "2-3")
        self.assertEqual([hit["page"] for hit in hits], [3])
        self.assertEqual(hits[0]["matched_terms"], ["BASELINE"])
        self.assertTrue(pdf_prepare.search_manifest(manifest, "ninety characters. The red"))
        for pages in ("0", "4", "3-1", "", "1,", "-1", "1.5", [True], [1.5], []):
            with self.subTest(pages=pages), self.assertRaises(ValueError):
                pdf_prepare.select_pages(manifest, pages)
        with self.assertRaises(ValueError):
            pdf_prepare.search_manifest(manifest, " ")

    def test_image_only_document_has_no_text_layer(self):
        no_text = self.root / "image-only.pdf"
        with fitz.open(stream=self.original, filetype="pdf") as original, fitz.open() as document:
            document.insert_pdf(original, from_page=1, to_page=1)
            document.save(no_text)
        manifest = pdf_prepare.prepare_pdf(no_text, self.cache)
        self.assertEqual(manifest["text_layer_status"], "absent")
        self.assertEqual(manifest["pages"][0]["text"], "")
        self.assertTrue(manifest["pages"][0]["images"])
        self.assertEqual(manifest["pages"][0]["captions"], [])

    def test_scan_json_reuses_cache_and_keeps_original_figure_number(self):
        self.prepare()
        stream = io.StringIO()
        with contextlib.redirect_stdout(stream), mock.patch.object(pdf_prepare, "_extract",
                                                                  side_effect=AssertionError("reparsed")):
            fig.scan(self.pdf, only_page=1, json_output=True, cache_dir=self.cache)
        result = json.loads(stream.getvalue())
        self.assertTrue(result["cache"]["hit"])
        self.assertEqual(len(result["pages"]), 1)
        self.assertEqual(result["pages"][0]["captions"][0]["figure_number"], "2")

    def test_batch_crops_original_pixels_and_rotated_coordinates(self):
        plan = self.plan(self.crop(), self.crop(page=3, figure_number="S4", out="nested/s4.png",
                                               bbox=[10, 10, 110, 50]))
        result = fig.batch_crop(self.pdf, plan, self.outputs)
        self.assertEqual([item["figure_number"] for item in result["crops"]], ["2", "S4"])
        first, second = result["crops"]
        self.assertEqual((first["width"], first["height"]), (180, 80))
        self.assertEqual((second["width"], second["height"]), (40, 100))
        pixmap = fitz.Pixmap(first["out"])
        self.assertEqual(pixmap.pixel(60, 40), (255, 0, 0))
        for item in result["crops"]:
            self.assertEqual(hashlib.sha256(Path(item["out"]).read_bytes()).hexdigest(), item["image_sha256"])
            self.assertEqual(item["pdf_sha256"], self.digest)
        self.assertEqual(self.pdf.read_bytes(), self.original)

    def test_batch_preflight_invalid_geometry_writes_nothing(self):
        cases = [dict(page=0), dict(page=4), dict(page=True), dict(dpi=0), dict(dpi=1201),
                 dict(dpi=72.5), dict(dpi=True), dict(pad=-1), dict(pad=float("nan")),
                 dict(bbox=[0, 0, 0, 1]), dict(bbox=[-1, 0, 10, 10]),
                 dict(bbox=[0, 0, 301, 20]), dict(bbox=[0, 0, 20, 401]),
                 dict(bbox=[0, 0, 20, float("inf")]), dict(bbox=[0, 0, 1])]
        for changes in cases:
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                fig.batch_crop(self.pdf, self.plan(self.crop(), self.crop(out="other.png", **changes)),
                               self.outputs)
            self.assertFalse(self.outputs.exists())

    def test_batch_rejects_wrong_pdf_duplicate_and_escaping_outputs(self):
        wrong = self.plan()
        wrong["pdf_sha256"] = "0" * 64
        with self.assertRaisesRegex(ValueError, "pdf_sha256"):
            fig.batch_crop(self.pdf, wrong, self.outputs)
        for filename in ("fig2.png", "nested/../fig2.png", "../escape.png", str(self.root / "absolute.png")):
            with self.subTest(filename=filename), self.assertRaises(ValueError):
                fig.batch_crop(self.pdf, self.plan(self.crop(), self.crop(out=filename)), self.outputs)
        self.assertFalse(self.outputs.exists())
        self.assertFalse((self.root / "escape.png").exists())

    def test_existing_outputs_need_explicit_overwrite(self):
        self.outputs.mkdir()
        target = self.outputs / "fig2.png"
        target.write_bytes(b"keep me")
        with self.assertRaisesRegex(ValueError, "already exists"):
            fig.batch_crop(self.pdf, self.plan(), self.outputs)
        self.assertEqual(target.read_bytes(), b"keep me")
        fig.batch_crop(self.pdf, self.plan(), self.outputs, overwrite=True)
        self.assertTrue(target.read_bytes().startswith(b"\x89PNG"))

    def test_render_failure_does_not_publish_earlier_crops(self):
        original_method = fitz.Page.get_pixmap
        calls = 0

        def fail_second(page, *args, **kwargs):
            nonlocal calls
            calls += 1
            if calls == 2:
                raise RuntimeError("simulated render failure")
            return original_method(page, *args, **kwargs)

        with mock.patch.object(fitz.Page, "get_pixmap", fail_second), self.assertRaises(RuntimeError):
            fig.batch_crop(self.pdf, self.plan(self.crop(), self.crop(out="second.png")), self.outputs)
        self.assertEqual(list(self.outputs.iterdir()), [])

    def test_cli_prepare_scan_batch_and_legacy_crop(self):
        prepared = self.cli("pdf_prepare.py", self.pdf, "--cache-dir", self.cache,
                            "--pages", "3", "--search", "ablation")
        self.assertEqual(prepared.returncode, 0, prepared.stderr)
        self.assertEqual(json.loads(prepared.stdout)["matches"][0]["page"], 3)
        scanned = self.cli("fig.py", "scan", self.pdf, "--page", "1", "--json", "--cache-dir", self.cache)
        self.assertEqual(scanned.returncode, 0, scanned.stderr)
        self.assertTrue(json.loads(scanned.stdout)["cache"]["hit"])
        cropped = self.cli("fig.py", "crop", self.pdf, "--page", "1", "--bbox", "30,100,210,180",
                           "--out", self.root / "legacy.png", "--dpi", "72", "--pad", "0")
        self.assertEqual(cropped.returncode, 0, cropped.stderr)
        plan_file = self.root / "crops.json"
        plan_file.write_text(json.dumps(self.plan()), encoding="utf-8")
        batched = self.cli("fig.py", "batch", self.pdf, "--manifest", plan_file, "--out-dir", self.outputs)
        self.assertEqual(batched.returncode, 0, batched.stderr)
        self.assertEqual(json.loads(batched.stdout)["crops"][0]["figure_number"], "2")
        invalid = self.cli("fig.py", "page", self.pdf, "--page", "0", "--out", self.root / "bad.png")
        self.assertEqual(invalid.returncode, 2)
        self.assertNotIn("Traceback", invalid.stderr)


if __name__ == "__main__":
    unittest.main()

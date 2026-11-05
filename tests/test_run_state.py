import json
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import run_state


class RunStateTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.source = self.root / "paper.pdf"
        self.source.write_bytes(b"test source bytes")
        self.note = self.root / "note.md"
        self.note.write_text("test note", encoding="utf-8")
        self.path = self.root / "run.json"
        run_state.init_state(self.path, "paper-key", self.source, self.note)

    def receipt(self, step):
        state = run_state.load_state(self.path)
        receipt = {"source_sha256": state["source_sha256"],
                   "note_sha256": state["note_sha256"]}
        if step == "pdf_prepared":
            receipt["page_count"] = 2
        elif step == "note_validated":
            receipt.update(valid=True, errors=[], citekey="paper-key")
        else:
            receipt.update(page_id="page-a", readback_checked=True)
            if step == "images_verified":
                receipt.update(images=[], no_images_reason="This note has no figures.")
            if step == "zotero_written":
                receipt["item_key"] = "ITEM1234"
        return receipt

    def finish(self):
        for step in run_state.STEPS:
            run_state.record_step(self.path, step, self.receipt(step))

    def test_complete_only_after_all_observations(self):
        self.assertFalse(run_state.status(run_state.load_state(self.path))["complete"])
        self.finish()
        result = run_state.status(run_state.load_state(self.path))
        self.assertTrue(result["complete"])
        self.assertEqual(result["pending"], [])
        # Same init keeps current progress.
        self.assertTrue(run_state.init_state(self.path, "paper-key", self.source, self.note)["complete"])

    def test_source_changed_is_stale_and_init_invalidates(self):
        self.finish()
        self.source.write_bytes(b"revised paper")
        result = run_state.status(run_state.load_state(self.path))
        self.assertEqual(result["done"], [])
        self.assertIn("source_changed", result["stale"])
        with self.assertRaisesRegex(ValueError, "init again"):
            run_state.record_step(self.path, "zotero_written", self.receipt("zotero_written"))
        run_state.init_state(self.path, "paper-key", self.source)
        state = run_state.load_state(self.path)
        self.assertEqual(state["steps"], {})
        self.assertEqual(state["invalidations"][-1]["reasons"], ["source_changed"])

    def test_note_changed_keeps_pdf_progress(self):
        self.finish()
        self.note.write_text("new note", encoding="utf-8")
        result = run_state.status(run_state.load_state(self.path))
        self.assertEqual(result["done"], ["pdf_prepared"])
        run_state.init_state(self.path, "paper-key", self.source, self.note)
        self.assertEqual(list(run_state.load_state(self.path)["steps"]), ["pdf_prepared"])

    def test_old_receipt_cannot_validate_changed_note(self):
        old_receipt = self.receipt("note_validated")
        self.note.write_text("changed", encoding="utf-8")
        run_state.init_state(self.path, "paper-key", self.source, self.note)
        run_state.record_step(self.path, "pdf_prepared", self.receipt("pdf_prepared"))
        with self.assertRaisesRegex(ValueError, "current note_sha256"):
            run_state.record_step(self.path, "note_validated", old_receipt)

    def test_failure_and_retry(self):
        run_state.record_step(self.path, "pdf_prepared", failure="PDF extraction failed")
        result = run_state.status(run_state.load_state(self.path))
        self.assertIn("pdf_prepared", result["pending"])
        self.finish()
        self.assertTrue(run_state.status(run_state.load_state(self.path))["complete"])

    def test_remote_id_without_readback_is_rejected(self):
        for step in ("pdf_prepared", "note_validated"):
            run_state.record_step(self.path, step, self.receipt(step))
        receipt = self.receipt("notion_written")
        receipt.pop("readback_checked")
        with self.assertRaisesRegex(ValueError, "readback_checked"):
            run_state.record_step(self.path, "notion_written", receipt)

    def test_remote_timings_are_preserved_and_validated(self):
        for step in ("pdf_prepared", "note_validated"):
            run_state.record_step(self.path, step, self.receipt(step))
        receipt = self.receipt("notion_written")
        receipt["timings_ms"] = {"citekey_query": 1200, "page_write": 2400,
                                  "readback": 1500}
        run_state.record_step(self.path, "notion_written", receipt)
        saved = run_state.load_state(self.path)["steps"]["notion_written"]["evidence"]
        self.assertEqual(saved["timings_ms"]["page_write"], 2400)
        receipt["timings_ms"] = {"page_write": -1}
        with self.assertRaisesRegex(ValueError, "timings_ms"):
            run_state.record_step(self.path, "notion_written", receipt)

    def test_publication_change_invalidates_remote_dependents(self):
        self.finish()
        receipt = self.receipt("notion_written")
        receipt["page_id"] = "page-b"
        run_state.record_step(self.path, "notion_written", receipt)
        result = run_state.status(run_state.load_state(self.path))
        self.assertEqual(result["pending"], ["images_verified", "zotero_written"])
        with self.assertRaisesRegex(ValueError, "page_id differs"):
            run_state.record_step(self.path, "zotero_written", self.receipt("zotero_written"))

    def test_missing_dependencies_and_unconfigured_steps(self):
        with self.assertRaisesRegex(ValueError, "note_validated"):
            run_state.record_step(self.path, "notion_written", self.receipt("notion_written"))
        run_state.init_state(self.path, "paper-key", self.source, steps=["pdf_prepared"])
        run_state.record_step(self.path, "pdf_prepared", self.receipt("pdf_prepared"))
        result = run_state.status(run_state.load_state(self.path))
        self.assertTrue(result["complete"])
        self.assertEqual(result["done"], ["pdf_prepared"])
        with self.assertRaisesRegex(ValueError, "not configured"):
            run_state.record_step(self.path, "notion_written", self.receipt("notion_written"))

    def test_image_changes_make_image_step_pending(self):
        self.finish()
        image = self.root / "figure.png"
        image.write_bytes(b"original figure")
        receipt = self.receipt("images_verified")
        receipt["images"] = [{"path": str(image), "sha256": run_state.file_hash(image),
                              "block_id": "image-block"}]
        run_state.record_step(self.path, "images_verified", receipt)
        image.write_bytes(b"different image")
        result = run_state.status(run_state.load_state(self.path))
        self.assertFalse(result["complete"])
        self.assertEqual(result["pending"], ["images_verified"])

    def test_invalid_state_or_receipt_does_not_overwrite_progress(self):
        before = self.path.read_bytes()
        with self.assertRaisesRegex(ValueError, "different citekey"):
            run_state.init_state(self.path, "another", self.source)
        self.assertEqual(self.path.read_bytes(), before)
        receipt = self.receipt("pdf_prepared")
        receipt["source_sha256"] = "0" * 64
        with self.assertRaisesRegex(ValueError, "does not match"):
            run_state.record_step(self.path, "pdf_prepared", receipt)
        self.assertEqual(self.path.read_bytes(), before)
        malformed = json.loads(before)
        malformed["steps"] = {"pdf_prepared": {"status": "done"}}
        self.path.write_text(json.dumps(malformed), encoding="utf-8")
        with self.assertRaises(ValueError):
            run_state.load_state(self.path)


if __name__ == "__main__":
    unittest.main()

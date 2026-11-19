import contextlib
import io
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import config
import run_state

def fixture():
    return {"publishing": {"backend": "feishu"}, "feishu": {
        "destination_url": "https://example.feishu.cn/base/test-base?table=test-table&view=test-view",
        "base_token": "test-base", "table_id": "test-table", "view_id": "test-view"}}


class FeishuRunTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        self.source, self.note, self.path = root / "paper.pdf", root / "note.md", root / "run.json"
        self.source.write_bytes(b"test pdf")
        self.note.write_text("test note", encoding="utf-8")
        self.cfg = fixture()
        run_state.init_state(self.path, "key", self.source, self.note, backend="feishu", cfg=self.cfg)

    def receipt(self, step):
        state = run_state.load_state(self.path)
        r = {"source_sha256": state["source_sha256"], "note_sha256": state["note_sha256"]}
        if step == "pdf_prepared":
            r["page_count"] = 1
        elif step == "note_validated":
            r.update(valid=True, errors=[], citekey="key")
        else:
            r.update(destination=config.feishu_destination(self.cfg), document_id="doc1", readback_document_id="doc1", readback_checked=True)
            if step == "feishu_written":
                r.update(record_id="rec1", readback_record_id="rec1", document_url="https://example.feishu.cn/docx/doc1", domain_preserved=True)
            elif step == "images_verified":
                r.update(images=[], no_images_reason="No figures in fixture")
            elif step == "pdf_uploaded":
                r.update(record_id="rec1", readback_record_id="rec1", attachment_token="file-token")
            elif step == "zotero_written":
                r["item_key"] = "ITEM1234"
        return r

    def prepare(self):
        for step in ("pdf_prepared", "note_validated"):
            run_state.record_step(self.path, step, self.receipt(step))

    def test_complete_requires_all_real_observations(self):
        self.prepare()
        self.assertFalse(run_state.status(run_state.load_state(self.path))["complete"])
        for step in ("feishu_written", "images_verified"):
            run_state.record_step(self.path, step, self.receipt(step))
        result = run_state.status(run_state.load_state(self.path))
        self.assertTrue(result["complete"])
        self.assertTrue(result["note_complete"])
        self.assertEqual(result["archival_pending"], ["pdf_uploaded"])
        state = run_state.load_state(self.path)
        self.assertNotIn("notion_written", state["required_steps"])
        self.assertEqual(state["backend"], "feishu")

    def test_pdf_upload_failure_is_archival_pending_not_note_failure(self):
        self.prepare()
        for step in ("feishu_written", "images_verified"):
            run_state.record_step(self.path, step, self.receipt(step))
        result = run_state.record_step(self.path, "pdf_uploaded", failure="upload unavailable")
        self.assertTrue(result["publication_complete"])
        self.assertTrue(result["complete"])
        self.assertEqual(result["archival_pending"], ["pdf_uploaded"])

    def test_verified_pdf_upload_and_mismatch_rejection(self):
        receipt = self.receipt("pdf_uploaded")
        result = run_state.record_step(self.path, "pdf_uploaded", receipt)
        self.assertTrue(result["archival_complete"])
        saved = run_state.load_state(self.path)["steps"]["pdf_uploaded"]["evidence"]
        self.assertEqual(saved["attachment_token"], "file-token")
        for field, value in (("destination", {}), ("source_sha256", "0" * 64),
                             ("readback_record_id", "other"), ("attachment_token", "")):
            bad = self.receipt("pdf_uploaded")
            bad[field] = value
            with self.assertRaises(ValueError):
                run_state.record_step(self.path, "pdf_uploaded", bad)

    def test_note_change_keeps_verified_pdf_archive(self):
        run_state.record_step(self.path, "pdf_uploaded", self.receipt("pdf_uploaded"))
        self.note.write_text("changed note", encoding="utf-8")
        run_state.init_state(self.path, "key", self.source, self.note, backend="feishu", cfg=self.cfg)
        self.assertIn("pdf_uploaded", run_state.load_state(self.path)["steps"])

    def test_wrong_target_document_record_or_domain_rejected(self):
        self.prepare()
        for field, value in (("destination", {}), ("readback_document_id", "other"), ("readback_record_id", "other"), ("domain_preserved", False), ("readback_checked", False), ("document_url", self.cfg["feishu"]["destination_url"])):
            r = self.receipt("feishu_written")
            r[field] = value
            with self.assertRaises(ValueError):
                run_state.record_step(self.path, "feishu_written", r)

    def test_cli_new_run_uses_configured_default_feishu(self):
        new_path = self.path.with_name("cli-run.json")
        stream = io.StringIO()
        with patch.object(config, "load", return_value=self.cfg), contextlib.redirect_stdout(stream):
            self.assertEqual(run_state.main(["init", "--file", str(new_path), "--citekey", "key",
                                             "--source", str(self.source), "--note", str(self.note)]), 0)
        result = json.loads(stream.getvalue())
        self.assertEqual(result["backend"], "feishu")
        self.assertIn("feishu_written", result["required_steps"])
        self.assertIn("pdf_uploaded", result["required_steps"])
        self.assertNotIn("zotero_written", result["required_steps"])
        self.assertFalse(result["complete"])

    def test_no_target_cannot_initialize_feishu(self):
        with self.assertRaisesRegex(ValueError, "BLOCKED"):
            run_state.init_state(self.path, "key", self.source, self.note, backend="feishu", cfg={})

    def test_image_or_backlink_receipt_must_match_document(self):
        self.prepare()
        run_state.record_step(self.path, "feishu_written", self.receipt("feishu_written"))
        r = self.receipt("images_verified")
        r.update(document_id="other", readback_document_id="other")
        with self.assertRaisesRegex(ValueError, "differs"):
            run_state.record_step(self.path, "images_verified", r)

    def test_target_change_invalidates_only_remote_steps(self):
        for step in run_state.FEISHU_STEPS:
            run_state.record_step(self.path, step, self.receipt(step))
        self.cfg["feishu"].update(destination_url="https://example.feishu.cn/base/base2?table=test-table&view=test-view", base_token="base2")
        result = run_state.init_state(self.path, "key", self.source, self.note, backend="feishu", cfg=self.cfg)
        self.assertEqual(result["done"], ["pdf_prepared", "note_validated"])
        self.assertFalse(result["complete"])

    def test_partial_steps_never_claim_published(self):
        result = run_state.init_state(self.path, "key", self.source, self.note, steps=["pdf_prepared"], backend="feishu", cfg=self.cfg)
        result = run_state.record_step(self.path, "pdf_prepared", self.receipt("pdf_prepared"))
        self.assertFalse(result["complete"])


    def test_pdf_archive_must_match_published_record(self):
        self.prepare()
        run_state.record_step(self.path, "feishu_written", self.receipt("feishu_written"))
        receipt = self.receipt("pdf_uploaded")
        receipt.update(record_id="other", readback_record_id="other")
        with self.assertRaisesRegex(ValueError, "differs"):
            run_state.record_step(self.path, "pdf_uploaded", receipt)

    def test_note_change_does_not_invalidate_pdf_archive_before_init(self):
        run_state.record_step(self.path, "pdf_uploaded", self.receipt("pdf_uploaded"))
        self.note.write_text("revised note", encoding="utf-8")
        result = run_state.status(run_state.load_state(self.path))
        self.assertTrue(result["archival_complete"])
        self.assertIn("pdf_uploaded", result["done"])
        self.assertFalse(result["publication_complete"])

    def test_published_record_change_invalidates_old_pdf_receipt(self):
        self.prepare()
        run_state.record_step(self.path, "feishu_written", self.receipt("feishu_written"))
        run_state.record_step(self.path, "pdf_uploaded", self.receipt("pdf_uploaded"))
        receipt = self.receipt("feishu_written")
        receipt.update(record_id="new-record", readback_record_id="new-record")
        result = run_state.record_step(self.path, "feishu_written", receipt)
        self.assertIn("pdf_uploaded", result["archival_pending"])
        self.assertNotIn("pdf_uploaded", run_state.load_state(self.path)["steps"])



class OptionalZoteroTests(unittest.TestCase):
    def test_backlink_failure_does_not_fail_published_note_or_expose_error_payload(self):
        import publish
        props = {"citekey": "key", "title": "Title"}
        with patch.object(publish, "write_zotero", side_effect=RuntimeError("PRIVATE_ERROR_PAYLOAD")):
            result = publish.optional_zotero(props, "https://example.feishu.cn/docx/doc1", {}, {})
        self.assertIn("backlink pending", result)
        self.assertNotIn("PRIVATE_ERROR_PAYLOAD", result)

    def test_successful_backlink_result_is_retained(self):
        import publish
        props = {"citekey": "key", "title": "Title", "alt_titles": ("Original",)}
        with patch.object(publish, "write_zotero", return_value="written: ITEM1234") as write:
            result = publish.optional_zotero(props, "url", {"data": ["PDB"]}, {})
        self.assertEqual(result, "written: ITEM1234")
        self.assertEqual(write.call_args.args[-1], ("Original",))

if __name__ == "__main__":
    unittest.main()

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
        from unittest.mock import patch
        env = patch.dict("os.environ", {"SOUNDING_FEISHU_STATE_DIR": str(Path(self.temporary.name) / "state")})
        env.start()
        self.addCleanup(env.stop)
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
            stack.enter_context(patch.object(
                publish.config, "feishu", return_value={"editor_open_ids": ["ou_" + "a" * 32]}))
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


class PublishPermissionFlowTests(unittest.TestCase):
    def setUp(self):
        from unittest.mock import MagicMock
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        from unittest.mock import patch
        env = patch.dict("os.environ", {"SOUNDING_FEISHU_STATE_DIR": str(Path(self.temporary.name) / "state")})
        env.start()
        self.addCleanup(env.stop)
        self.note = Path(self.temporary.name) / "notes" / "paper.md"
        self.note.parent.mkdir()
        self.note.write_text(NOTE, encoding="utf-8")
        self.workspace = {"app_token": "app", "folder_token": "folder", "tables": {"papers": "table"}}
        self.client = MagicMock()
        self.client.create_doc.return_value = "doc"
        self.client.doc_url.return_value = "/docx/doc"
        self.client.get.return_value = {"record": {"record_id": "record", "fields": {"笔记": {"link": "/docx/doc"}}}}
        self.client.create_record.return_value = {"record_id": "record"}
        self.client.doc_text.return_value = "x" * 600
        self.client.doc_children.return_value = [{} for _ in range(11)]
        self.editors = ["ou_" + "a" * 32, "ou_" + "b" * 32]

    def run_publish(self, editors, *, dry_run=False, index=None, rows=None, error=None, invalid=False,
                    cfg_overrides=None, expect_public=False):
        from contextlib import ExitStack, redirect_stderr, redirect_stdout
        from io import StringIO
        from unittest.mock import call, patch
        cfg = {"domain": "tenant.feishu.cn"}
        if editors is not None:
            cfg["editor_open_ids"] = editors
        cfg.update(cfg_overrides or {})
        argv = ["publish.py", str(self.note), "--data", "PDB"]
        if dry_run:
            argv.append("--dry-run")
        with ExitStack() as stack:
            stack.enter_context(redirect_stderr(StringIO()))
            stack.enter_context(redirect_stdout(StringIO()))
            stack.enter_context(patch.object(sys, "argv", argv))
            stack.enter_context(patch.object(publish.config, "feishu", return_value=cfg))
            stack.enter_context(patch.object(publish, "load_json", side_effect=[self.workspace, index or {}]))
            factory = stack.enter_context(patch.object(publish.fapi, "client_from_config", return_value=self.client))
            stack.enter_context(patch.object(publish, "find_papers", return_value=rows or []))
            stack.enter_context(patch.object(publish.fdoc, "orphan_figure_files", return_value=[]))
            stack.enter_context(patch.object(publish.fdoc, "note_figures", return_value=[]))
            body = stack.enter_context(patch.object(publish.fdoc, "replace_body"))
            save = stack.enter_context(patch.object(publish, "save_json"))
            events = []
            def write_fake_journal(*args, **kwargs):
                Path(kwargs["resume_path"]).write_text("{}", encoding="utf-8")
                events.append("body")
            body.side_effect = write_fake_journal
            self.client.ensure_doc_full_access.side_effect = (
                error if error else lambda *args: events.append("permissions"))
            if invalid:
                with self.assertRaisesRegex(ValueError, "editor_open_ids") as ctx:
                    publish.main()
                self.assertNotIn("ou_", str(ctx.exception))
                factory.assert_not_called()
                body.assert_not_called()
                save.assert_not_called()
                return
            if error:
                with self.assertRaises(publish.fapi.FeishuError):
                    publish.main()
            else:
                publish.main()
            if dry_run:
                factory.assert_not_called()
                body.assert_not_called()
                save.assert_not_called()
            elif error:
                body.assert_not_called()
                self.client.grant_public_access.assert_not_called()
                self.client.create_record.assert_not_called()
                self.client.update_record.assert_not_called()
                if index or rows:
                    save.assert_not_called()
                else:
                    save.assert_called_once()
                    self.assertEqual(save.call_args.args[1]["20861cd95b"],
                                     {"doc_id": "doc", "state": "pending"})
            else:
                self.client.ensure_doc_full_access.assert_called_once_with("doc", self.editors)
                self.assertEqual(events, ["permissions", "body"])
                if expect_public:
                    self.client.grant_public_access.assert_called_once_with("doc")
                    self.assertLess(self.client.mock_calls.index(call.ensure_doc_full_access("doc", self.editors)),
                                    self.client.mock_calls.index(call.grant_public_access("doc")))
                else:
                    self.client.grant_public_access.assert_not_called()
                self.assertEqual(save.call_count, 1 if index or rows else 2)
            return save

    def test_invalid_editor_config_preflight_before_any_remote_write(self):
        invalid = [None, [], "bad", {}, [""], ["ou_short"], ["ou_" + "z" * 32],
                   ["ou_" + "a" * 32 + " "], [17], [[]], ["ou_" + "a" * 32] * 2]
        for ids in invalid:
            with self.subTest(kind=type(ids).__name__):
                self.client.reset_mock()
                self.run_publish(ids, invalid=True)
                self.client.create_doc.assert_not_called()
                self.client.update_record.assert_not_called()
                self.client.create_record.assert_not_called()

    def test_dry_run_does_not_construct_remote_client_even_without_editors(self):
        self.run_publish(None, dry_run=True)

    def test_dry_run_with_migration_does_not_write_note_or_state(self):
        from contextlib import ExitStack, redirect_stderr, redirect_stdout
        from io import StringIO
        from unittest.mock import patch
        original = self.note.read_text(encoding="utf-8")
        with ExitStack() as stack:
            stack.enter_context(redirect_stdout(StringIO()))
            stack.enter_context(redirect_stderr(StringIO()))
            stack.enter_context(patch.object(sys, "argv", ["publish.py", str(self.note), "--data", "PDB",
                                                           "--mark-migrated", "notion", "--dry-run"]))
            stack.enter_context(patch.object(publish.config, "feishu", return_value={}))
            stack.enter_context(patch.object(publish, "load_json", side_effect=[self.workspace, {}]))
            factory = stack.enter_context(patch.object(publish.fapi, "client_from_config"))
            publish.main()
            factory.assert_not_called()
        self.assertEqual(self.note.read_text(encoding="utf-8"), original)
        self.assertFalse((Path(self.temporary.name) / "state").exists())
        self.assertFalse((self.note.parent / ".feishu-index.json.lock").exists())

    def test_create_grants_and_verifies_before_public_link_and_row(self):
        self.run_publish(self.editors, expect_public=True)
        self.client.create_doc.assert_called_once()
        self.client.create_record.assert_called_once()
        self.client.grant_public_access.assert_called_once_with("doc")

    def test_update_preserves_sharing_and_user_maintained_fields(self):
        rows = [{"record_id": "record", "fields": {"发表时间": "2026-07", "领域": ["manual"],
                                               "笔记": {"link": "https://tenant.feishu.cn/docx/doc",
                                                        "text": "笔记"}}}]
        index = {"20861cd95b": {"doc_id": "doc"}}
        self.run_publish(self.editors, index=index, rows=rows)
        self.client.create_doc.assert_not_called()
        fields = self.client.update_record.call_args.args[3]
        self.assertNotIn("发表时间", fields)
        self.assertNotIn("领域", fields)
        self.assertNotIn("读法", fields)
        self.assertNotIn("笔记", fields)

    def test_existing_link_is_reused_without_local_index(self):
        rows = [{"record_id": "record", "fields": {
            "笔记": {"link": "https://tenant.feishu.cn/docx/doc?from=copy", "text": "笔记"}}}]
        save = self.run_publish(self.editors, rows=rows)
        self.client.create_doc.assert_not_called()
        self.client.create_record.assert_not_called()
        self.client.grant_public_access.assert_not_called()
        self.client.doc_url.assert_not_called()
        self.client.update_record.assert_called_once()
        self.assertNotIn("笔记", self.client.update_record.call_args.args[3])
        self.assertEqual(save.call_args.args[1]["20861cd95b"]["url"], rows[0]["fields"]["笔记"]["link"])

    def test_completed_index_without_row_does_not_make_document_public(self):
        self.run_publish(self.editors, index={"20861cd95b": {"doc_id": "doc"}})
        self.client.create_doc.assert_not_called()
        self.client.create_record.assert_called_once()
        self.client.grant_public_access.assert_not_called()

    def test_pending_document_without_row_keeps_default_public_sharing(self):
        pending = {"20861cd95b": {"doc_id": "doc", "state": "pending"}}
        self.run_publish(self.editors, index=pending, expect_public=True)
        self.client.create_doc.assert_not_called()
        self.client.create_record.assert_called_once()
        self.client.grant_public_access.assert_called_once_with("doc")

    def test_pending_index_with_existing_row_does_not_make_document_public(self):
        pending = {"20861cd95b": {"doc_id": "doc", "state": "pending"}}
        rows = [{"record_id": "record", "fields": {"笔记": {"link": "/docx/doc"}}}]
        self.run_publish(self.editors, index=pending, rows=rows)
        self.client.create_doc.assert_not_called()
        self.client.grant_public_access.assert_not_called()
        self.assertNotIn("笔记", self.client.update_record.call_args.args[3])

    def test_relative_legacy_link_is_reused_without_index(self):
        rows = [{"record_id": "record", "fields": {"笔记": {"link": "/docx/doc"}}}]
        self.run_publish(self.editors, rows=rows)
        self.client.create_doc.assert_not_called()
        self.client.update_record.assert_called_once()
        self.assertNotIn("笔记", self.client.update_record.call_args.args[3])

    def assert_row_rejected_without_mutation(self, fields, index=None, record_id="PRIVATE_ROW"):
        from contextlib import ExitStack, redirect_stderr, redirect_stdout
        from io import StringIO
        from unittest.mock import patch
        output, errors = StringIO(), StringIO()
        rows = [{"record_id": record_id, "fields": fields}]
        with ExitStack() as stack:
            stack.enter_context(redirect_stderr(errors))
            stack.enter_context(redirect_stdout(output))
            stack.enter_context(patch.object(sys, "argv", ["publish.py", str(self.note), "--data", "PDB"]))
            stack.enter_context(patch.object(
                publish.config, "feishu", return_value={"editor_open_ids": self.editors,
                                                        "domain": "tenant.feishu.cn"}))
            stack.enter_context(patch.object(publish, "load_json", side_effect=[self.workspace, index or {}]))
            stack.enter_context(patch.object(publish.fapi, "client_from_config", return_value=self.client))
            stack.enter_context(patch.object(publish, "find_papers", return_value=rows))
            body = stack.enter_context(patch.object(publish.fdoc, "replace_body"))
            save = stack.enter_context(patch.object(publish, "save_json"))
            with self.assertRaises(ValueError) as caught:
                publish.main()
        for sensitive in ("PRIVATE_DOC", "PRIVATE_INDEX", "PRIVATE_ROW", "evil.invalid"):
            self.assertNotIn(sensitive, str(caught.exception))
            self.assertNotIn(sensitive, output.getvalue())
            self.assertNotIn(sensitive, errors.getvalue())
        self.client.create_doc.assert_not_called()
        self.client.ensure_doc_full_access.assert_not_called()
        self.client.grant_public_access.assert_not_called()
        self.client.create_record.assert_not_called()
        self.client.update_record.assert_not_called()
        body.assert_not_called()
        save.assert_not_called()

    def test_index_mismatch_stops_before_all_remote_mutations(self):
        fields = {"笔记": {"link": "https://tenant.feishu.cn/docx/PRIVATE_DOC"}}
        self.assert_row_rejected_without_mutation(fields, {"20861cd95b": {"doc_id": "PRIVATE_INDEX"}})

    def test_malformed_index_ids_fail_before_remote_mutations(self):
        fields = {"笔记": {"link": "/docx/PRIVATE_DOC"}}
        for value in (False, 0, [], {}, 123):
            with self.subTest(value_type=type(value).__name__):
                self.client.reset_mock()
                self.assert_row_rejected_without_mutation(fields, {"20861cd95b": {"doc_id": value}})

    def test_existing_row_without_record_id_does_not_create_another_row(self):
        fields = {"笔记": {"link": "/docx/PRIVATE_DOC"}}
        self.assert_row_rejected_without_mutation(fields, record_id=None)

    def test_missing_malformed_external_and_non_docx_links_fail_closed(self):
        links = [None, {}, {"link": None}, {"link": []}, {"link": ""},
                 {"link": "https://evil.invalid/docx/PRIVATE_DOC"},
                 {"link": "https://tenant.feishu.cn.evil.invalid/docx/PRIVATE_DOC"},
                 {"link": "https://tenant.feishu.cn@evil.invalid/docx/PRIVATE_DOC"},
                 {"link": "https://tenant.feishu.cn:443/docx/PRIVATE_DOC"},
                 {"link": "http://tenant.feishu.cn/docx/PRIVATE_DOC"},
                 {"link": "//tenant.feishu.cn/docx/PRIVATE_DOC"},
                 {"link": "https://tenant.feishu.cn/wiki/PRIVATE_DOC"},
                 {"link": "/docx/PRIVATE_DOC/extra"},
                 {"link": "/docx/PRIVATE_DOC%2Fother"},
                 {"link": "///docx/PRIVATE_DOC"},
                 {"link": "/docx/PRIVATE_DOC\n"},
                 {"link": " /docx/PRIVATE_DOC"},
                 {"link": "https://[bad/docx/PRIVATE_DOC"}]
        for note in links:
            for index in ({}, {"20861cd95b": {"doc_id": "PRIVATE_INDEX"}}):
                with self.subTest(note_type=type(note).__name__, position=links.index(note),
                                  indexed=bool(index)):
                    self.client.reset_mock()
                    self.assert_row_rejected_without_mutation({"笔记": note}, index)
        self.client.reset_mock()
        self.assert_row_rejected_without_mutation({})
        self.client.reset_mock()
        self.assert_row_rejected_without_mutation(["malformed"])

    def test_destination_host_can_establish_trust_when_domain_is_unset(self):
        cfg = {"destination_url": "https://tenant.feishu.cn/base/base?table=table&view=view",
               "base_token": "base", "table_id": "table", "view_id": "view"}
        fields = {"笔记": {"link": "https://tenant.feishu.cn/docx/doc?from=copy"}}
        self.assertEqual(publish.existing_note_doc_id(fields, cfg), "doc")
        save = self.run_publish(self.editors, rows=[{"record_id": "record", "fields": fields}],
                                cfg_overrides={"domain": "", **cfg})
        self.client.create_doc.assert_not_called()
        self.client.doc_url.assert_not_called()
        self.assertEqual(save.call_args.args[1]["20861cd95b"]["url"], fields["笔记"]["link"])
        cfg["table_id"] = "other"
        with self.assertRaises(ValueError):
            publish.existing_note_doc_id(fields, cfg)

    def test_absolute_link_requires_the_exact_trusted_tenant(self):
        fields = {"笔记": {"link": "https://tenant.feishu.cn/docx/doc"}}
        for cfg in ({}, {"domain": "other.feishu.cn"}, {"domain": "tenant.feishu.cn.evil.invalid"},
                    {"domain": "https://tenant.feishu.cn"}, {"domain": ".feishu.cn"}):
            with self.subTest(config_case=type(cfg.get("domain")).__name__):
                with self.assertRaises(ValueError):
                    publish.existing_note_doc_id(fields, cfg)
        evil = {"笔记": {"link": "https://evil.invalid/docx/doc"}}
        with self.assertRaises(ValueError):
            publish.existing_note_doc_id(evil, {"domain": "evil.invalid"})
        confusable = "tenant.feiſhu.cn"
        with self.assertRaises(ValueError):
            publish.existing_note_doc_id({"笔记": {"link": f"https://{confusable}/docx/doc"}},
                                         {"domain": confusable})

    def test_permission_failure_records_only_pending_and_cannot_report_success(self):
        failure = publish.fapi.FeishuError("document permissions", -1, "verification failed")
        self.run_publish(self.editors, error=failure)

    def test_existing_document_permission_failure_does_not_touch_body(self):
        failure = publish.fapi.FeishuError("document permissions", -1, "verification failed")
        self.run_publish(self.editors, index={"20861cd95b": {"doc_id": "doc"}}, error=failure)
        self.client.create_doc.assert_not_called()

    def test_linked_row_permission_failure_does_not_touch_body_or_row(self):
        failure = publish.fapi.FeishuError("document permissions", -1, "verification failed")
        rows = [{"record_id": "record", "fields": {"笔记": {"link": "/docx/doc"}}}]
        self.run_publish(self.editors, rows=rows, error=failure)
        self.client.create_doc.assert_not_called()

    def test_failed_authorization_reuses_pending_document_on_retry(self):
        import json
        from contextlib import ExitStack, redirect_stderr, redirect_stdout
        from io import StringIO
        from unittest.mock import patch
        workspace_path = self.note.parent / ".feishu-workspace.json"
        workspace_path.write_text(json.dumps(self.workspace), encoding="utf-8")
        index_path = self.note.parent / ".feishu-index.json"
        failure = publish.fapi.FeishuError("document permissions", -1, "verification failed")
        with ExitStack() as stack:
            stack.enter_context(redirect_stderr(StringIO()))
            stack.enter_context(redirect_stdout(StringIO()))
            stack.enter_context(patch.object(sys, "argv", ["publish.py", str(self.note), "--data", "PDB"]))
            stack.enter_context(patch.object(publish.config, "feishu", return_value={"editor_open_ids": self.editors}))
            stack.enter_context(patch.object(publish.fapi, "client_from_config", return_value=self.client))
            stack.enter_context(patch.object(publish, "find_papers", return_value=[]))
            stack.enter_context(patch.object(publish.fdoc, "orphan_figure_files", return_value=[]))
            stack.enter_context(patch.object(publish.fdoc, "note_figures", return_value=[]))
            body = stack.enter_context(patch.object(publish.fdoc, "replace_body"))
            body.side_effect = lambda *args, **kwargs: Path(kwargs["resume_path"]).write_text("{}", encoding="utf-8")
            self.client.ensure_doc_full_access.side_effect = failure
            with self.assertRaises(publish.fapi.FeishuError):
                publish.main()
            body.assert_not_called()
            self.client.grant_public_access.assert_not_called()
            pending = json.loads(index_path.read_text(encoding="utf-8"))
            self.assertEqual(pending["20861cd95b"], {"doc_id": "doc", "state": "pending"})
            self.client.ensure_doc_full_access.side_effect = None
            publish.main()
            self.client.create_doc.assert_called_once()
            self.client.grant_public_access.assert_called_once_with("doc")
            body.assert_called_once()
            completed = json.loads(index_path.read_text(encoding="utf-8"))["20861cd95b"]
            self.assertEqual(completed["doc_id"], "doc")
            self.assertEqual(completed["record_id"], "record")
            self.assertNotIn("state", completed)
            self.assertEqual(list(self.note.parent.glob(".feishu-index-*.tmp")), [])

    def test_pending_index_failure_stops_before_permission_or_body(self):
        from contextlib import ExitStack, redirect_stderr, redirect_stdout
        from io import StringIO
        from unittest.mock import patch
        with ExitStack() as stack:
            stack.enter_context(redirect_stderr(StringIO()))
            stack.enter_context(redirect_stdout(StringIO()))
            stack.enter_context(patch.object(sys, "argv", ["publish.py", str(self.note), "--data", "PDB"]))
            stack.enter_context(patch.object(publish.config, "feishu", return_value={"editor_open_ids": self.editors}))
            stack.enter_context(patch.object(publish, "load_json", side_effect=[self.workspace, {}]))
            stack.enter_context(patch.object(publish.fapi, "client_from_config", return_value=self.client))
            stack.enter_context(patch.object(publish, "find_papers", return_value=[]))
            stack.enter_context(patch.object(publish, "save_json", side_effect=OSError("private data")))
            body = stack.enter_context(patch.object(publish.fdoc, "replace_body"))
            with self.assertRaisesRegex(RuntimeError, "manual recovery") as ctx:
                publish.main()
            self.assertNotIn("private data", str(ctx.exception))
            self.assertTrue(ctx.exception.__suppress_context__)
            self.client.ensure_doc_full_access.assert_not_called()
            body.assert_not_called()
            self.client.create_record.assert_not_called()

    def test_completion_index_failure_never_reports_success(self):
        from contextlib import ExitStack, redirect_stderr, redirect_stdout
        from io import StringIO
        from unittest.mock import patch
        output = StringIO()
        with ExitStack() as stack:
            stack.enter_context(redirect_stderr(StringIO()))
            stack.enter_context(redirect_stdout(output))
            stack.enter_context(patch.object(sys, "argv", ["publish.py", str(self.note), "--data", "PDB"]))
            stack.enter_context(patch.object(publish.config, "feishu", return_value={"editor_open_ids": self.editors}))
            stack.enter_context(patch.object(publish, "load_json", side_effect=[self.workspace, {}]))
            stack.enter_context(patch.object(publish.fapi, "client_from_config", return_value=self.client))
            stack.enter_context(patch.object(publish, "find_papers", return_value=[]))
            stack.enter_context(patch.object(publish.fdoc, "orphan_figure_files", return_value=[]))
            stack.enter_context(patch.object(publish.fdoc, "note_figures", return_value=[]))
            stack.enter_context(patch.object(publish.fdoc, "replace_body"))
            stack.enter_context(patch.object(publish, "save_json", side_effect=[None, OSError("private data")]))
            with self.assertRaisesRegex(RuntimeError, "verify before retrying") as ctx:
                publish.main()
            self.assertNotIn("private data", str(ctx.exception))
            self.assertTrue(ctx.exception.__suppress_context__)
            self.assertEqual(output.getvalue(), "")

    def test_atomic_index_failure_preserves_previous_index(self):
        from unittest.mock import patch
        index_path = self.note.parent / ".feishu-index.json"
        index_path.write_text('{"old": "data"}', encoding="utf-8")
        with patch.object(publish.os, "replace", side_effect=OSError("failed")):
            with self.assertRaises(OSError):
                publish.save_json(str(index_path), {"new": "data"})
        self.assertEqual(index_path.read_text(encoding="utf-8"), '{"old": "data"}')
        self.assertEqual(list(self.note.parent.glob(".feishu-index-*.tmp")), [])



def crash_publisher_before_writer(note, state_root, remote_path, figure_root, stage="before"):
    import os
    from contextlib import ExitStack, redirect_stderr, redirect_stdout
    from io import StringIO
    from unittest.mock import patch
    from test_feishu_image_concurrency import LocalDocument, LocalClient, fixture
    remote = LocalDocument(remote_path)
    remote.crash_clear = stage in ("clear", "clear_empty")

    class Client(LocalClient):
        def search_records(self, app, table, conditions):
            return [{"record_id": "record", "fields": {"笔记": {"link": "/docx/doc"}}}]

        def ensure_doc_full_access(self, doc, editors):
            pass

    with ExitStack() as stack:
        stack.enter_context(redirect_stdout(StringIO()))
        stack.enter_context(redirect_stderr(StringIO()))
        stack.enter_context(patch.dict(os.environ, {"SOUNDING_FEISHU_STATE_DIR": state_root}))
        stack.enter_context(patch.object(sys, "argv", ["publish.py", note, "--data", "PDB"]))
        stack.enter_context(patch.object(publish.config, "feishu", return_value={
            "editor_open_ids": ["ou_" + "a" * 32], "image_concurrency": 1}))
        stack.enter_context(patch.object(publish.fapi, "client_from_config", return_value=Client(remote, primary=True)))
        stack.enter_context(patch.object(publish.fdoc, "orphan_figure_files", return_value=[]))
        figures = [] if stage == "clear_empty" else fixture(figure_root)
        stack.enter_context(patch.object(publish.fdoc, "note_figures", return_value=figures))
        if stage == "before":
            stack.enter_context(patch.object(publish.fdoc, "replace_body", side_effect=lambda *args, **kwargs: os._exit(73)))
        publish.main()


class PublishRealImageIntegrationTests(unittest.TestCase):
    def setUp(self):
        import json
        from unittest.mock import patch
        from test_feishu_image_concurrency import LocalDocument, fixture
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.notes = self.root / "clone" / "notes"
        self.notes.mkdir(parents=True)
        self.note = self.notes / "paper.md"
        tables = "\n\n".join("| A | B |\n| --- | --- |\n| a | b |" for _ in range(11))
        self.note.write_text(NOTE + "\n\n## Results\nFig. 1 explains one.\n\nFig. 2 explains two.\n\nFig. 3 explains three.\n\n" + tables, encoding="utf-8")
        self.workspace = {"app_token": "app", "folder_token": "folder", "tables": {"papers": "table"}}
        (self.notes / ".feishu-workspace.json").write_text(json.dumps(self.workspace), encoding="utf-8")
        self.state_root = self.root / "shared"
        env = patch.dict("os.environ", {"SOUNDING_FEISHU_STATE_DIR": str(self.state_root)})
        env.start(); self.addCleanup(env.stop)
        self.remote = LocalDocument(self.root / "remote.json")
        self.figures = fixture(self.root)
        self.records = {"record": {"record_id": "record", "fields": {
            "笔记": {"link": "/docx/doc"}, "citekey": "20861cd95b"}}}
        self.editor = ["ou_" + "a" * 32]
        self.factory_calls = 0

    def run_publish(self, figures=None, mutate_writer=None, use_manifest=False):
        from contextlib import ExitStack, redirect_stderr, redirect_stdout
        from io import StringIO
        from unittest.mock import patch
        from test_feishu_image_concurrency import LocalClient
        remote, records = self.remote, self.records

        class Client(LocalClient):
            def search_records(self, app, table, conditions):
                return [records["record"]]

            def ensure_doc_full_access(self, doc, editors):
                pass

            def update_record(self, app, table, record_id, fields):
                records[record_id]["fields"].update(fields)
                return records[record_id]

            def doc_url(self, doc):
                return "/docx/" + doc

            def doc_text(self, doc):
                return "x" * 600

        client = Client(remote, primary=True)
        calls = []
        def factory(cfg):
            calls.append(1)
            self.factory_calls += 1
            return client if len(calls) == 1 else remote.client()
        with ExitStack() as stack:
            stack.enter_context(redirect_stdout(StringIO()))
            stack.enter_context(redirect_stderr(StringIO()))
            stack.enter_context(patch.object(sys, "argv", ["publish.py", str(self.note), "--data", "PDB"]))
            stack.enter_context(patch.object(publish.config, "feishu", return_value={
                "editor_open_ids": self.editor, "image_concurrency": 1}))
            stack.enter_context(patch.object(publish.fapi, "client_from_config", side_effect=factory))
            stack.enter_context(patch.object(publish.fdoc, "orphan_figure_files", return_value=[]))
            if not use_manifest:
                stack.enter_context(patch.object(publish.fdoc, "note_figures", return_value=self.figures if figures is None else figures))
            if mutate_writer is not None:
                stack.enter_context(patch.object(publish.fdoc, "replace_body", side_effect=mutate_writer))
            return publish.main()

    def shared_state(self):
        import json
        registries = list(self.state_root.glob("*/registry.json"))
        self.assertEqual(len(registries), 1)
        registry = json.loads(registries[0].read_text(encoding="utf-8"))
        papers = list(registry["papers"].values())
        self.assertEqual(len(papers), 1)
        return papers[0]

    def journal(self):
        import json
        journals = list(self.state_root.glob("*/images/*.json"))
        self.assertEqual(len(journals), 1)
        return json.loads(journals[0].read_text(encoding="utf-8"))

    def test_invalid_figure_preflight_preserves_empty_and_completed_state(self):
        from unittest.mock import patch
        missing = self.root / "missing.png"
        first = self.figures[0]
        cases = [
            [(first[0], first[1], "bad label")],
            [first, (self.figures[1][0], "second", first[2])],
            [(str(missing), "missing", "fig1")],
        ]
        for invalid in cases:
            with self.subTest(invalid=invalid):
                factory_calls = self.factory_calls
                with self.assertRaises((ValueError, OSError)):
                    self.run_publish(figures=invalid)
                self.assertEqual(self.factory_calls, factory_calls)
                self.assertEqual(self.shared_state(), {})
                self.assertEqual(self.remote.clear_count, 0)
                self.assertEqual(list(self.state_root.glob("*/images/*.json")), [])
        self.run_publish()
        old_journal = self.journal()
        self.assertEqual(self.shared_state()["phase"], "complete")
        self.note.write_text(self.note.read_text(encoding="utf-8") + "\nupdated revision\n", encoding="utf-8")
        for invalid in cases:
            with self.subTest(completed=True, invalid=invalid):
                with self.assertRaises((ValueError, OSError)):
                    self.run_publish(figures=invalid)
                self.assertEqual(self.shared_state()["phase"], "complete")
                self.assertEqual(self.remote.clear_count, 1)
                self.assertEqual(self.journal(), old_journal)
        real_open = open
        target = str(self.figures[0][0])
        def denied(path, mode="r", *args, **kwargs):
            if str(path) == target and "b" in mode:
                raise PermissionError("simulated unreadable image")
            return real_open(path, mode, *args, **kwargs)
        with patch("builtins.open", side_effect=denied):
            with self.assertRaises((ValueError, OSError)):
                self.run_publish()
        self.assertEqual(self.shared_state()["phase"], "complete")
        self.assertEqual(self.remote.clear_count, 1)
        self.run_publish()
        self.assertEqual(self.shared_state()["phase"], "complete")
        self.assertEqual(self.remote.clear_count, 2)

    def test_missing_manifest_file_fails_without_poison_and_retry_succeeds(self):
        import json
        folder = self.notes / "figs" / "paper"
        folder.mkdir(parents=True)
        manifest = folder / "manifest.json"
        manifest.write_text(json.dumps({"figures": [{"label": "fig1", "page": 1}]}), encoding="utf-8")
        image = folder / "fig1.png"
        with self.assertRaises((OSError, ValueError)):
            self.run_publish(use_manifest=True)
        self.assertEqual(self.factory_calls, 0)
        self.assertEqual(self.shared_state(), {})
        self.assertEqual(self.remote.clear_count, 0)
        self.assertEqual(list(self.state_root.glob("*/images/*.json")), [])
        image.write_bytes(b"first image")
        self.run_publish(use_manifest=True)
        self.assertEqual(self.shared_state()["phase"], "complete")
        self.assertEqual(self.remote.clear_count, 1)
        image.unlink()
        self.note.write_text(self.note.read_text(encoding="utf-8") + "\nupdated revision\n", encoding="utf-8")
        calls = self.factory_calls
        with self.assertRaises((OSError, ValueError)):
            self.run_publish(use_manifest=True)
        self.assertEqual(self.factory_calls, calls)
        self.assertEqual(self.shared_state()["phase"], "complete")
        self.assertEqual(self.remote.clear_count, 1)
        image.write_bytes(b"restored image")
        self.run_publish(use_manifest=True)
        self.assertEqual(self.shared_state()["phase"], "complete")
        self.assertEqual(self.remote.clear_count, 2)

    def test_existing_document_initializes_empty_journal_then_adds_figures(self):
        self.run_publish(figures=[])
        self.assertEqual(self.journal()["identity"]["figures"], [])
        self.assertEqual(self.remote.clear_count, 1)
        self.run_publish(figures=[])
        self.assertEqual(self.remote.clear_count, 1)
        self.run_publish()
        self.assertEqual(len(self.journal()["identity"]["figures"]), 3)
        self.assertEqual(self.remote.clear_count, 2)

    def test_complete_revisions_repeat_text_figure_and_removal(self):
        self.run_publish()
        self.assertEqual(self.remote.clear_count, 1)
        self.run_publish()
        self.assertEqual(self.remote.clear_count, 1)
        self.note.write_text(self.note.read_text(encoding="utf-8") + "\nnew text\n", encoding="utf-8")
        self.run_publish()
        self.assertEqual(self.remote.clear_count, 2)
        Path(self.figures[0][0]).write_bytes(b"new image bytes")
        self.run_publish()
        self.assertEqual(self.remote.clear_count, 3)
        self.run_publish(figures=[])
        self.assertEqual(self.remote.clear_count, 4)
        self.assertEqual(self.journal()["identity"]["figures"], [])
        self.run_publish(figures=[])
        self.assertEqual(self.remote.clear_count, 4)
        self.run_publish()
        self.assertEqual(self.remote.clear_count, 5)

    def test_incomplete_revision_rejects_changed_and_removed_then_resumes(self):
        self.run_publish()
        self.note.write_text(self.note.read_text(encoding="utf-8") + "\nnew revision\n", encoding="utf-8")
        revision = self.note.read_text(encoding="utf-8")
        self.remote.fail["fig2.png"] = "fail"
        with self.assertRaises(publish.fapi.FeishuError):
            self.run_publish()
        self.assertEqual(self.remote.clear_count, 2)
        attempts = self.remote.attempts[:]
        with self.assertRaisesRegex(ValueError, "does not match"):
            self.run_publish(figures=[])
        self.note.write_text(revision + "changed again", encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "does not match"):
            self.run_publish()
        self.assertEqual(self.remote.clear_count, 2)
        self.assertEqual(self.remote.attempts, attempts)
        self.note.write_text(revision, encoding="utf-8")
        self.run_publish()
        self.assertEqual(self.remote.clear_count, 2)
        self.assertEqual(self.remote.attempts.count("fig1.png"), 2)
        self.assertEqual(self.remote.attempts.count("fig2.png"), 3)

    def test_crash_before_transition_cannot_reuse_completion_authority(self):
        self.run_publish()
        self.note.write_text(self.note.read_text(encoding="utf-8") + "\nnew revision\n", encoding="utf-8")
        with self.assertRaises(KeyboardInterrupt):
            self.run_publish(mutate_writer=KeyboardInterrupt())
        with self.assertRaisesRegex(ValueError, "does not match"):
            self.run_publish()
        self.assertEqual(self.remote.clear_count, 1)

    def test_real_process_death_after_intent_cannot_reuse_authority(self):
        import multiprocessing
        self.run_publish()
        self.note.write_text(self.note.read_text(encoding="utf-8") + "\nchanged\n", encoding="utf-8")
        process = multiprocessing.get_context("spawn").Process(target=crash_publisher_before_writer, args=(
            str(self.note), str(self.state_root), str(self.root / "remote.json"), str(self.root)))
        process.start(); process.join(20)
        if process.is_alive():
            process.terminate(); process.join(5)
            self.fail("crash fixture did not exit")
        self.assertEqual(process.exitcode, 73)
        with self.assertRaisesRegex(ValueError, "does not match"):
            self.run_publish()
        self.assertEqual(self.remote.clear_count, 1)

    def test_real_process_death_during_new_skeleton_fails_closed(self):
        import multiprocessing
        from test_feishu_image_concurrency import LocalDocument
        self.run_publish()
        self.note.write_text(self.note.read_text(encoding="utf-8") + "\nnew revision\n", encoding="utf-8")
        remote_path = self.root / "remote.json"
        process = multiprocessing.get_context("spawn").Process(target=crash_publisher_before_writer, args=(
            str(self.note), str(self.state_root), str(remote_path), str(self.root), "clear"))
        process.start(); process.join(20)
        if process.is_alive():
            process.terminate(); process.join(5)
            self.fail("crash fixture did not exit")
        self.assertEqual(process.exitcode, 72)
        self.assertEqual(self.journal()["phase"], "building")
        self.remote = LocalDocument(remote_path)
        self.assertEqual(self.remote.clear_count, 2)
        with self.assertRaisesRegex(ValueError, "incomplete|uncertain"):
            self.run_publish()
        self.assertEqual(self.remote.clear_count, 2)

    def test_crash_during_figure_removal_blocks_replay(self):
        import multiprocessing
        from test_feishu_image_concurrency import LocalDocument
        self.run_publish()
        remote_path = self.root / "remote.json"
        process = multiprocessing.get_context("spawn").Process(target=crash_publisher_before_writer, args=(
            str(self.note), str(self.state_root), str(remote_path), str(self.root), "clear_empty"))
        process.start(); process.join(20)
        if process.is_alive():
            process.terminate(); process.join(5)
            self.fail("crash fixture did not exit")
        self.assertEqual(process.exitcode, 72)
        self.assertEqual(self.journal()["phase"], "building")
        self.remote = LocalDocument(remote_path)
        with self.assertRaisesRegex(ValueError, "incomplete|uncertain"):
            self.run_publish(figures=[])
        self.assertEqual(self.remote.clear_count, 2)

    def test_changed_remote_document_is_not_overwritten(self):
        self.run_publish()
        block = next(b for b in self.remote.blocks.values() if b.get("text", {}).get("elements"))
        block["text"]["elements"] = []
        self.note.write_text(self.note.read_text(encoding="utf-8") + "\nnew revision\n", encoding="utf-8")
        attempts = self.remote.attempts[:]
        with self.assertRaisesRegex(ValueError, "changed|incomplete"):
            self.run_publish()
        self.assertEqual(self.remote.clear_count, 1)
        self.assertEqual(self.remote.attempts, attempts)

    def test_missing_initialized_checkpoint_fails_before_client(self):
        self.run_publish()
        journals = list(self.state_root.glob("*/images/*.json"))
        self.assertEqual(len(journals), 1)
        journals[0].unlink()
        with self.assertRaisesRegex(RuntimeError, "checkpoint is missing"):
            self.run_publish()
        self.assertEqual(self.remote.clear_count, 1)



if __name__ == "__main__":
    unittest.main(verbosity=2)

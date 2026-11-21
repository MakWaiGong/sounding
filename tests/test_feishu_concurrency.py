import json
import multiprocessing
import os
import sys
import tempfile
import unittest
from contextlib import ExitStack, redirect_stderr, redirect_stdout
from io import StringIO
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import feishu_concurrency as coordination
import publish

WORKSPACE = {"app_token": "app", "folder_token": "folder", "tables": {"papers": "table"}}
CONFIG = {"domain": "tenant.feishu.cn", "editor_open_ids": ["ou_" + "a" * 32]}
NOTE = """标题：Title
发表：Journal (2026)
类型：science
citekey：{key}
---
正文
## 定位素材
原文：Title · DOI {doi}
"""


class FakeClient:
    def __init__(self, state, guard, events, release, key, mode):
        self.state, self.guard, self.events, self.release = state, guard, events, release
        self.key, self.mode = key, mode

    def search_records(self, app, table, conditions, conjunction="and"):
        if self.mode == "find_created":
            return list(self.state["records"].values())
        return []  # Model a delayed remote search index.

    def create_doc(self, title, folder):
        with self.guard:
            self.state["doc_requests"] += 1
            if self.mode == "doc_rate_limit":
                raise publish.fapi.FeishuRateLimitError("request", 429, "rate limited")
            if self.mode == "doc_generic_429":
                raise publish.fapi.FeishuError("request", 429, "generic API error")
            self.state["doc_count"] += 1
            doc = "doc" + str(self.state["doc_count"])
        if self.mode == "doc_crash":
            os._exit(23)
        if self.mode in ("doc_unknown", "doc_5xx"):
            raise publish.fapi.FeishuUncertainError("private", 503, "unknown")
        return doc

    def ensure_doc_full_access(self, doc, editors):
        if self.mode == "permission_error":
            raise publish.fapi.FeishuError("private", -1, "failed")
        with self.guard:
            self.state["active"] += 1
            self.state["peak"] = max(self.state["peak"], self.state["active"])
        self.events.put(("entered", self.key))
        if not self.release.wait(20):
            raise RuntimeError("test release timed out")

    def grant_public_access(self, doc):
        pass

    def doc_url(self, doc):
        return "/docx/" + doc

    def create_record(self, app, table, fields):
        with self.guard:
            self.state["record_requests"] += 1
            if self.mode == "record_rate_limit":
                raise publish.fapi.FeishuRateLimitError("request", 99991400, "rate limited")
            if self.mode == "record_generic_429":
                raise publish.fapi.FeishuError("request", 429, "generic API error")
            self.state["record_count"] += 1
            record = "rec" + str(self.state["record_count"])
            if self.mode != "record_missing":
                self.state["records"][record] = {"record_id": record, "fields": dict(fields)}
        if self.mode in ("record_unknown", "record_5xx"):
            raise publish.fapi.FeishuUncertainError("private", 503, "unknown")
        if self.mode == "record_crash":
            os._exit(24)
        return {"record_id": record}

    def update_record(self, app, table, record, fields):
        with self.guard:
            row = dict(self.state["records"][record])
            row["fields"] = {**row["fields"], **fields}
            self.state["records"][record] = row

    def get(self, path):
        return {"record": self.state["records"].get(path.rsplit("/", 1)[-1])}

    def doc_text(self, doc):
        return "x" * 600

    def doc_children(self, doc):
        return [{} for _ in range(11)] + ([{"block_type": 27}] if self.mode == "image" else [])

    def close(self):
        pass


def publish_process(note, state_dir, state, guard, events, release, key, mode="normal", limit=3):
    client = FakeClient(state, guard, events, release, key, mode)
    def body(*args, **kwargs):
        Path(kwargs["resume_path"]).write_text("{}", encoding="utf-8")
        if mode == "image":
            resume = kwargs.get("resume_path")
            if not os.path.isdir(os.path.dirname(resume)):
                raise AssertionError("resume directory was not prepared")
            events.put(("resume", key, resume, kwargs.get("max_workers"),
                        callable(kwargs.get("client_factory"))))
        with guard:
            state["active"] -= 1
    cfg = {**CONFIG, "paper_concurrency": limit}
    with ExitStack() as stack:
        stack.enter_context(patch.dict(os.environ, {"SOUNDING_FEISHU_STATE_DIR": state_dir}))
        stack.enter_context(patch.object(sys, "argv", ["publish.py", note, "--data", "PDB"]))
        stack.enter_context(patch.object(publish.config, "feishu", return_value=cfg))
        stack.enter_context(patch.object(publish.fapi, "client_from_config", return_value=client))
        stack.enter_context(patch.object(publish.fdoc, "orphan_figure_files", return_value=[]))
        figure_file = Path(note).parent / "fake-figure.png"
        if mode == "image":
            figure_file.write_bytes(b"image")
        figures = [(str(figure_file), "Caption", "fig1")] if mode == "image" else []
        stack.enter_context(patch.object(publish.fdoc, "note_figures", return_value=figures))
        stack.enter_context(patch.object(publish.fdoc, "replace_body", side_effect=body))
        stack.enter_context(redirect_stdout(StringIO()))
        stack.enter_context(redirect_stderr(StringIO()))
        try:
            publish.main()
        except BaseException as exc:
            events.put(("error", key, type(exc).__name__, str(exc)))
        else:
            events.put(("done", key))


def lock_process(state_dir, signal, release):
    os.environ["SOUNDING_FEISHU_STATE_DIR"] = state_dir
    with coordination.Publication(WORKSPACE, CONFIG, "crash-key", "10.1234/crash", 3):
        signal.set()
        release.wait(20)


class ProcessTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.ctx = multiprocessing.get_context("spawn")
        cls.manager = cls.ctx.Manager()

    @classmethod
    def tearDownClass(cls):
        cls.manager.shutdown()

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.state_dir = str(self.root / "shared")
        self.state = self.manager.dict(doc_count=0, record_count=0, doc_requests=0, record_requests=0, active=0, peak=0,
                                       records=self.manager.dict())
        self.guard = self.ctx.Lock()
        self.events = self.ctx.Queue()
        self.release = self.ctx.Event()
        self.processes = []
        self.addCleanup(self.cleanup_processes)

    def cleanup_processes(self):
        self.release.set()
        for process in self.processes:
            if process.is_alive():
                process.terminate()
            process.join(5)
        self.events.close()

    def note(self, clone, key, doi):
        notes = self.root / clone / "notes"
        notes.mkdir(parents=True, exist_ok=True)
        (notes / ".feishu-workspace.json").write_text(json.dumps(WORKSPACE), encoding="utf-8")
        path = notes / (key + ".md")
        path.write_text(NOTE.format(key=key, doi=doi), encoding="utf-8")
        return str(path)

    def start(self, note, key, mode="normal", limit=3):
        process = self.ctx.Process(target=publish_process,
            args=(note, self.state_dir, self.state, self.guard, self.events, self.release, key, mode, limit))
        process.start()
        self.processes.append(process)
        return process

    def event(self, kind, key):
        event = self.events.get(timeout=15)
        self.assertEqual(event[:2], (kind, key), event)
        return event

    def finished(self, process, code=0):
        process.join(15)
        self.assertFalse(process.is_alive(), "publication process did not exit")
        self.assertEqual(process.exitcode, code)

    def test_distinct_papers_overlap_and_merge_same_index(self):
        a = self.note("clone", "a", "10.1234/a")
        b = self.note("clone", "b", "10.1234/b")
        index_path = self.root / "clone/notes/.feishu-index.json"
        index_path.write_text(json.dumps({"unrelated": {"manual": "preserve"}}), encoding="utf-8")
        pa = self.start(a, "a")
        self.event("entered", "a")
        pb = self.start(b, "b")
        self.event("entered", "b")
        self.assertEqual(self.state["peak"], 2)
        self.release.set()
        results = {self.events.get(timeout=15)[:2], self.events.get(timeout=15)[:2]}
        self.assertEqual(results, {("done", "a"), ("done", "b")})
        self.finished(pa); self.finished(pb)
        index = json.loads((self.root / "clone/notes/.feishu-index.json").read_text(encoding="utf-8"))
        self.assertEqual(set(index), {"a", "b", "unrelated"})
        self.assertEqual(index["unrelated"], {"manual": "preserve"})
        self.assertEqual((self.state["doc_count"], self.state["record_count"]), (2, 2))

    def test_doi_alias_busy_across_clones_and_reuses_nonindexed_row(self):
        a = self.note("clone-a", "a", "10.1234/DOI")
        b = self.note("clone-b", "b", "10.1234/doi")
        pa = self.start(a, "a")
        self.event("entered", "a")
        pb = self.start(b, "b")
        event = self.event("error", "b")
        self.assertEqual(event[2], "BusyError")
        self.assertEqual(self.state["doc_count"], 1)
        self.finished(pb)
        self.release.set()
        self.event("done", "a"); self.finished(pa)
        pc = self.start(b, "b")
        self.event("entered", "b"); self.event("done", "b"); self.finished(pc)
        self.assertEqual((self.state["doc_count"], self.state["record_count"]), (1, 1))
        index = json.loads((self.root / "clone-b/notes/.feishu-index.json").read_text(encoding="utf-8"))
        self.assertEqual(index["b"]["doc_id"], "doc1")
        self.assertEqual(self.state["records"]["rec1"]["fields"]["citekey"], "a")

    def test_capacity_and_mismatched_limits_fail_without_remote_work(self):
        paths = [self.note("clone", key, "10.1234/" + key) for key in ("a", "b", "c")]
        pa = self.start(paths[0], "a", limit=1)
        self.event("entered", "a")
        pb = self.start(paths[1], "b", limit=1)
        event = self.event("error", "b")
        self.assertIn("limit reached", event[3]); self.finished(pb)
        pc = self.start(paths[2], "c", limit=3)
        event = self.event("error", "c")
        self.assertIn("different", event[3]); self.finished(pc)
        self.assertEqual(self.state["doc_count"], 1)
        self.release.set(); self.event("done", "a"); self.finished(pa)

    def test_process_crash_releases_os_locks(self):
        signal, hold = self.ctx.Event(), self.ctx.Event()
        p = self.ctx.Process(target=lock_process, args=(self.state_dir, signal, hold))
        p.start(); self.processes.append(p)
        self.assertTrue(signal.wait(15))
        with patch.dict(os.environ, {"SOUNDING_FEISHU_STATE_DIR": self.state_dir}):
            with self.assertRaises(coordination.BusyError):
                with coordination.Publication(WORKSPACE, CONFIG, "crash-key", None, 3):
                    pass
            p.terminate(); p.join(15); self.assertFalse(p.is_alive())
            with coordination.Publication(WORKSPACE, CONFIG, "crash-key", None, 3):
                pass

    def test_unknown_document_and_crash_do_not_create_twice(self):
        for mode in ("doc_unknown", "doc_crash"):
            with self.subTest(mode=mode):
                key = mode
                path = self.note(mode, key, "10.1234/" + key)
                before = self.state["doc_count"]
                p = self.start(path, key, mode)
                if mode == "doc_unknown":
                    event = self.event("error", key); self.assertIn("outcome is unknown", event[3])
                    self.finished(p)
                else:
                    self.finished(p, 23)
                retry = self.start(path, key)
                event = self.event("error", key); self.finished(retry)
                self.assertIn("outcome is unknown", event[3])
                self.assertIn("repair", event[3])
                self.assertEqual(self.state["doc_count"], before + 1)

    def test_unknown_record_fails_closed_then_reconciles_real_row(self):
        path = self.note("clone", "a", "10.1234/a")
        self.release.set()
        p = self.start(path, "a", "record_unknown")
        self.event("entered", "a"); event = self.event("error", "a"); self.finished(p)
        self.assertIn("outcome is unknown", event[3])
        retry = self.start(path, "a")
        event = self.event("error", "a"); self.finished(retry)
        self.assertIn("outcome is unknown", event[3])
        self.assertEqual((self.state["doc_count"], self.state["record_count"]), (1, 1))
        reconcile = self.start(path, "a", "find_created")
        self.event("entered", "a"); self.event("done", "a"); self.finished(reconcile)
        self.assertEqual((self.state["doc_count"], self.state["record_count"]), (1, 1))

    def test_default_limit_three_and_same_citekey_busy(self):
        paths = [self.note("clone" + key, key, "10.1234/" + key) for key in ("a", "b", "c", "d")]
        active = []
        for path, key in zip(paths[:3], ("a", "b", "c")):
            p = self.start(path, key)
            self.event("entered", key)
            active.append(p)
        self.assertEqual(self.state["peak"], 3)
        blocked = self.start(paths[3], "d")
        event = self.event("error", "d"); self.finished(blocked)
        self.assertIn("limit reached", event[3])
        alias = self.note("other-clone", "a", "10.1234/different")
        duplicate = self.start(alias, "same-citekey")
        event = self.event("error", "same-citekey"); self.finished(duplicate)
        self.assertIn("paper busy", event[3])
        self.assertEqual(self.state["doc_count"], 3)
        self.release.set()
        events = {self.events.get(timeout=15)[:2] for _ in range(3)}
        self.assertEqual(events, {("done", "a"), ("done", "b"), ("done", "c")})
        for process in active:
            self.finished(process)

    def test_definitive_create_rate_limits_allow_safe_retry(self):
        self.release.set()
        for mode in ("doc_rate_limit", "record_rate_limit"):
            with self.subTest(mode=mode):
                key = mode
                path = self.note(mode, key, "10.1234/" + key)
                docs_before = self.state["doc_count"]
                records_before = self.state["record_count"]
                first = self.start(path, key, mode)
                if mode == "record_rate_limit":
                    self.event("entered", key)
                event = self.event("error", key); self.finished(first)
                self.assertEqual(event[2], "FeishuRateLimitError")
                self.assertIn("rate limited", event[3])
                self.assertEqual(self.state["record_count"], records_before)
                self.assertEqual(self.state["doc_count"], docs_before + (mode == "record_rate_limit"))
                retry = self.start(path, key)
                self.event("entered", key); self.event("done", key); self.finished(retry)
                self.assertEqual(self.state["doc_count"], docs_before + 1)
                self.assertEqual(self.state["record_count"], records_before + 1)

    def test_generic_numeric_429_is_not_a_proven_throttle(self):
        self.release.set()
        for mode in ("doc_generic_429", "record_generic_429"):
            with self.subTest(mode=mode):
                path = self.note(mode, mode, "10.1234/" + mode)
                first = self.start(path, mode, mode)
                if mode == "record_generic_429":
                    self.event("entered", mode)
                event = self.event("error", mode); self.finished(first)
                self.assertIn("outcome is unknown", event[3])
                requests = (self.state["doc_requests"], self.state["record_requests"])
                retry = self.start(path, mode)
                event = self.event("error", mode); self.finished(retry)
                self.assertIn("outcome is unknown", event[3])
                self.assertEqual((self.state["doc_requests"], self.state["record_requests"]), requests)

    def test_ambiguous_5xx_creates_still_block_retries(self):
        self.release.set()
        for mode in ("doc_5xx", "record_5xx"):
            with self.subTest(mode=mode):
                key = mode
                path = self.note(mode, key, "10.1234/" + key)
                first = self.start(path, key, mode)
                if mode == "record_5xx":
                    self.event("entered", key)
                event = self.event("error", key); self.finished(first)
                self.assertIn("outcome is unknown", event[3])
                requests = (self.state["doc_requests"], self.state["record_requests"])
                retry = self.start(path, key)
                event = self.event("error", key); self.finished(retry)
                self.assertIn("outcome is unknown", event[3])
                self.assertEqual((self.state["doc_requests"], self.state["record_requests"]), requests)

    def test_unreadable_created_row_is_not_reported_as_success(self):
        path = self.note("clone", "a", "10.1234/a")
        self.release.set()
        first = self.start(path, "a", "record_missing")
        self.event("entered", "a"); event = self.event("error", "a"); self.finished(first)
        self.assertIn("verify the recorded", event[3])
        index = json.loads((self.root / "clone/notes/.feishu-index.json").read_text(encoding="utf-8"))
        self.assertNotIn("record_id", index["a"])
        retry = self.start(path, "a")
        event = self.event("error", "a"); self.finished(retry)
        self.assertIn("verify the recorded", event[3])
        self.assertEqual((self.state["doc_count"], self.state["record_count"]), (1, 1))

    def test_record_crash_and_delayed_search_fail_closed(self):
        path = self.note("clone", "a", "10.1234/a")
        self.release.set()
        p = self.start(path, "a", "record_crash")
        self.event("entered", "a"); self.finished(p, 24)
        retry = self.start(path, "a")
        event = self.event("error", "a"); self.finished(retry)
        self.assertIn("outcome is unknown", event[3])
        self.assertEqual((self.state["doc_count"], self.state["record_count"]), (1, 1))

    def test_figures_resume_path_is_shared_by_alias_across_clones(self):
        a = self.note("clone-a", "a", "10.1234/shared")
        b = self.note("clone-b", "b", "10.1234/shared")
        self.release.set()
        pa = self.start(a, "a", "image")
        self.event("entered", "a"); first = self.event("resume", "a")
        self.event("done", "a"); self.finished(pa)
        pb = self.start(b, "b", "image")
        self.event("entered", "b"); second = self.event("resume", "b")
        self.event("done", "b"); self.finished(pb)
        self.assertTrue(os.path.isabs(first[2]))
        self.assertEqual(first[2:], second[2:])
        self.assertEqual(first[3:], (3, True))
        self.assertEqual((self.state["doc_count"], self.state["record_count"]), (1, 1))

    def test_pending_document_shared_across_clones(self):
        a = self.note("clone-a", "a", "10.1234/shared")
        b = self.note("clone-b", "b", "10.1234/shared")
        p = self.start(a, "a", "permission_error")
        self.event("error", "a"); self.finished(p)
        self.release.set()
        retry = self.start(b, "b")
        self.event("entered", "b"); self.event("done", "b"); self.finished(retry)
        self.assertEqual((self.state["doc_count"], self.state["record_count"]), (1, 1))


class CoordinationTests(unittest.TestCase):
    def test_limit_validation(self):
        self.assertEqual(coordination.worker_count({}, "paper_concurrency"), 3)
        for value in (0, -1, 9, 1.5, True, "3", None):
            with self.subTest(value=value), self.assertRaisesRegex(ValueError, "between 1 and 8"):
                coordination.worker_count({"paper_concurrency": value}, "paper_concurrency")
        self.assertEqual(coordination.worker_count({"image_concurrency": 8}, "image_concurrency"), 8)

    def test_corrupt_shared_and_local_entries_fail_before_client_creation(self):
        from types import SimpleNamespace
        cases = [
            ({"phase": "record_known", "doc_id": "doc"}, {}),
            ({"phase": "complete", "doc_id": "doc", "record_id": ""}, {}),
            ({"phase": "pending", "doc_id": "doc", "make_public": "false"}, {}),
            ({}, {"doc_id": "doc", "record_id": None}),
            ({}, {"doc_id": "invalid/path"}),
        ]
        for shared, entry in cases:
            with self.subTest(shared=shared, entry=entry):
                publication = SimpleNamespace(get_state=lambda: shared)
                with patch.object(publish.fapi, "client_from_config") as factory:
                    with self.assertRaises((RuntimeError, ValueError)):
                        publish.publish_locked(None, None, WORKSPACE, CONFIG, {}, {}, "unused",
                                               entry, publication, [], 3)
                    factory.assert_not_called()

    def test_effective_api_host_defines_destination(self):
        canonical = coordination.destination_dir(WORKSPACE, CONFIG)
        equivalent = coordination.destination_dir(WORKSPACE, {**CONFIG,
            "api_base": "https://OPEN.FEISHU.CN:443/ignored?ignored=1"})
        self.assertEqual(canonical, equivalent)
        other = coordination.destination_dir(WORKSPACE, {**CONFIG, "api_base": "https://other.feishu.cn"})
        self.assertNotEqual(canonical, other)

    def test_doi_normalization(self):
        a = coordination.alias_keys("A", "10.1234/ABC%2Fdef")
        b = coordination.alias_keys("B", "https://doi.org/10.1234/abc/DEF")
        self.assertEqual(len(set(a) & set(b)), 1)


if __name__ == "__main__":
    unittest.main()

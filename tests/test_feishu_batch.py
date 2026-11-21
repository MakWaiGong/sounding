import json
import os
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from contextlib import redirect_stderr, redirect_stdout
from io import StringIO
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

sys.dont_write_bytecode = True
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import publish_batch as batch

NOTE = """标题：Test paper
发表：Journal (2026)
类型：science
citekey：{key}
---
body
## 定位素材
原文：Title · DOI {doi}
"""

FAKE_CHILD = r'''import json
import os
import sys
import time
from pathlib import Path

path = Path(sys.argv[1])
key = next(line.split("：", 1)[1] for line in path.read_text(encoding="utf-8").splitlines() if line.startswith("citekey："))
logs = Path(os.environ["BATCH_TEST_LOG"])
(logs / (key + ".args")).write_text(json.dumps(sys.argv[1:]), encoding="utf-8")
(logs / (key + ".start")).write_text(str(time.monotonic()), encoding="utf-8")
time.sleep(0.45)
(logs / (key + ".end")).write_text(str(time.monotonic()), encoding="utf-8")
if key == "busy":
    print("paper busy: another publication is in progress", file=sys.stderr)
    sys.exit(1)
if key == "fail":
    print("credential SECRET_EXAMPLE in error output", file=sys.stderr)
    sys.exit(7)
if key == "readback":
    print("回读不通过：details SECRET_EXAMPLE", file=sys.stderr)
    sys.exit(1)
if key == "huge":
    print("SECRET_EXAMPLE" * 20000)
    sys.exit(0)
if key == "bad":
    print("not JSON SECRET_EXAMPLE")
    sys.exit(0)
if "--dry-run" in sys.argv:
    result = {"plan": {"citekey": key, "mode": "create"}, "missing": {}}
else:
    result = {"citekey": key, "action": "row_created", "doc": os.environ.get("BATCH_TEST_DOC", "doc123"),
              "url": os.environ.get("BATCH_TEST_URL", "https://tenant.feishu.cn/docx/doc123"),
              "zotero": "failed: SECRET_EXAMPLE" if key == "backlink" else "written: local-id"}
print(json.dumps(result))
'''


LOCK_CHILD = r'''import json
import os
import sys
import time
from pathlib import Path
sys.path.insert(0, os.environ["BATCH_SCRIPTS"])
import feishu_concurrency as concurrency

path = Path(sys.argv[1])
key = next(line.split("：", 1)[1] for line in path.read_text(encoding="utf-8").splitlines() if line.startswith("citekey："))
ws = {"app_token": "fake", "tables": {"papers": "fake"}}
try:
    with concurrency.Publication(ws, {}, key, None, 1):
        Path(os.environ["BATCH_STARTED"]).write_text("started", encoding="utf-8")
        deadline = time.monotonic() + 15
        while not Path(os.environ["BATCH_RELEASE"]).exists():
            if time.monotonic() > deadline:
                sys.exit("test release timeout")
            time.sleep(0.02)
        print(json.dumps({"citekey": key, "action": "row_created", "doc": "doc123",
                          "url": "https://tenant.feishu.cn/docx/doc123", "zotero": "not requested"}))
except concurrency.BusyError as exc:
    sys.exit(str(exc))
'''

class BatchTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.notes = self.root / "notes"
        self.notes.mkdir()
        self.log = self.root / "logs"
        self.log.mkdir()
        self.child = self.root / "fake_child.py"
        self.child.write_text(FAKE_CHILD, encoding="utf-8")
        self.env = patch.dict(os.environ, {"BATCH_TEST_LOG": str(self.log), "FEISHU_APP_SECRET": "SECRET_EXAMPLE"})
        self.env.start()
        self.addCleanup(self.env.stop)

    def note(self, key, doi=None, filename=None, text=None):
        path = self.notes / (filename or (key + ".md"))
        path.write_text(text or NOTE.format(key=key, doi=doi or "10.1234/" + key), encoding="utf-8")
        return str(path)

    def run_main(self, args, limit=2, child=None):
        out, err = StringIO(), StringIO()
        with patch.object(batch.config, "feishu", return_value={"paper_concurrency": limit, "domain": "tenant.feishu.cn"}), \
             patch.object(batch, "PUBLISHER", child or self.child), \
             redirect_stdout(out), redirect_stderr(err):
            code = batch.main(args)
        return code, json.loads(out.getvalue()), err.getvalue()

    def test_overlap_default_cap_and_results(self):
        paths = [self.note("paper" + str(i)) for i in range(5)]
        code, result, err = self.run_main(paths + ["--data", "PDB"])
        self.assertEqual((code, result["status"], result["workers"]), (0, "success", 2))
        self.assertEqual([r["note"] for r in result["results"]], [1, 2, 3, 4, 5])
        self.assertEqual(len({r["citekey_id"] for r in result["results"]}), 5)
        self.assertTrue(all(r["status"] == "success" for r in result["results"]))
        self.assertTrue(all(r["url"] == "https://tenant.feishu.cn/docx/doc123"
                            and r["doc"] == "doc123" for r in result["results"]))
        intervals = [(float((self.log / ("paper" + str(i) + ".start")).read_text()),
                      float((self.log / ("paper" + str(i) + ".end")).read_text())) for i in range(5)]
        peak = max(sum(start <= t < end for start, end in intervals) for t, _ in intervals)
        self.assertEqual(peak, 2)
        self.assertEqual(err, "")

    def test_explicit_one_worker_and_options_are_forwarded(self):
        paths = [self.note("one"), self.note("two")]
        code, result, _ = self.run_main(paths + ["--workers", "1", "--data", "A,B", "--allow-partial", "--zotero"])
        self.assertEqual(code, 0)
        self.assertEqual(result["workers"], 1)
        self.assertEqual(result["results"][0]["backlink"], "written")
        first_end = float((self.log / "one.end").read_text())
        second_start = float((self.log / "two.start").read_text())
        self.assertLessEqual(first_end, second_start)
        for name, path in zip(("one", "two"), paths):
            self.assertEqual(json.loads((self.log / (name + ".args")).read_text()),
                             [str(Path(path).absolute()), "--data=A,B", "--allow-partial", "--zotero"])

    def test_failure_busy_readback_and_no_automatic_retry(self):
        paths = [self.note(key) for key in ("fine", "busy", "fail", "readback")]
        code, result, err = self.run_main(paths + ["--data", "PDB"])
        self.assertEqual(code, 1)
        self.assertEqual(result["status"], "incomplete")
        self.assertEqual([r["status"] for r in result["results"]], ["success", "busy", "failed", "failed"])
        self.assertEqual(result["results"][1]["reason"], "paper_busy")
        self.assertEqual(result["results"][2]["exit_code"], 7)
        self.assertEqual(result["results"][3]["reason"], "readback_failed_verify_before_retry")
        self.assertEqual(len(list(self.log.glob("*.start"))), 4)
        self.assertNotIn("SECRET_EXAMPLE", json.dumps(result) + err)

    def test_untrusted_links_fail_without_echoing_child_secrets(self):
        path = self.note("link")
        note = batch.preflight([path], "PDB")[0]
        args = batch.arguments([path, "--data", "PDB"])
        cfg = {"domain": "tenant.feishu.cn"}
        process = SimpleNamespace(returncode=0)
        errors = SimpleNamespace(data=b"", failed=False)
        cases = [
            ("https://foreign.example/docx/doc123", "doc123"),
            ("https://other.feishu.cn/docx/doc123", "doc123"),
            ("https://tenant.feishu.cn/docx/other", "doc123"),
            ("https://tenant.feishu.cn/docx/doc123?token=SECRET_EXAMPLE", "doc123"),
            ("https://tenant.feishu.cn/docx/doc123?", "doc123"),
            ("https://tenant.feishu.cn/docx/doc123#SECRET_EXAMPLE", "doc123"),
            ("https://tenant.feishu.cn/docx/doc123#", "doc123"),
            ("https://user:SECRET_EXAMPLE@tenant.feishu.cn/docx/doc123", "doc123"),
            ("https://tenant.feishu.cn:443/docx/doc123", "doc123"),
            ("http://tenant.feishu.cn/docx/doc123", "doc123"),
            ("//tenant.feishu.cn/docx/doc123", "doc123"),
            ("https://tenant.feishu.cn/docx/doc123/extra", "doc123"),
            ("https://tenant.feishu.cn/docx/doc123", None),
            (None, "doc123"),
        ]
        for url, doc in cases:
            with self.subTest(url=url, doc=doc):
                child = {"citekey": "link", "action": "row_created", "url": url, "doc": doc}
                output = SimpleNamespace(data=json.dumps(child).encode(), truncated=False, failed=False)
                result = batch.result_for(note, args, process, output, errors, cfg)
                self.assertEqual(result["status"], "failed")
                self.assertEqual(result["reason"], "invalid_child_result_verify_before_retry")
                self.assertNotIn("SECRET_EXAMPLE", json.dumps(result))

    def test_untrusted_link_sets_overall_failure(self):
        path = self.note("untrusted")
        env = {"BATCH_TEST_URL": "https://user:SECRET_EXAMPLE@tenant.feishu.cn/docx/doc123"}
        with patch.dict(os.environ, env):
            code, result, err = self.run_main([path, "--data", "PDB"])
        self.assertEqual(code, 1)
        self.assertEqual(result["status"], "incomplete")
        self.assertEqual(result["results"][0]["reason"], "invalid_child_result_verify_before_retry")
        self.assertNotIn("SECRET_EXAMPLE", json.dumps(result) + err)

    def test_relative_url_resolves_only_to_configured_trusted_domain(self):
        child = {"url": "/docx/doc123", "doc": "doc123"}
        self.assertEqual(batch.verified_document(child, {"domain": "tenant.feishu.cn"}),
                         ("https://tenant.feishu.cn/docx/doc123", "doc123"))
        destination = {"destination_url": "https://tenant.feishu.cn/base/base123?table=table123&view=view123",
                       "base_token": "base123", "table_id": "table123", "view_id": "view123"}
        self.assertEqual(batch.verified_document(child, destination),
                         ("https://tenant.feishu.cn/docx/doc123", "doc123"))
        for cfg in ({}, {"domain": "foreign.example"},
                    {"destination_url": "https://foreign.example/base/base123?table=table123&view=view123"}):
            with self.subTest(cfg=cfg), self.assertRaises(ValueError):
                batch.verified_document(child, cfg)

    def test_zotero_pending_is_partial_but_not_failed(self):
        code, result, _ = self.run_main([self.note("backlink"), "--data", "PDB", "--zotero"])
        self.assertEqual(code, 0)
        self.assertEqual(result["status"], "partial")
        self.assertEqual(result["results"][0]["status"], "success")
        self.assertEqual(result["results"][0]["backlink"], "pending")
        self.assertEqual(result["results"][0]["url"], "https://tenant.feishu.cn/docx/doc123")
        self.assertEqual(result["results"][0]["doc"], "doc123")
        self.assertNotIn("SECRET_EXAMPLE", json.dumps(result))

    def test_allow_partial_is_reported_but_not_failed(self):
        path = self.note("missing", text=NOTE.format(key="missing", doi="10.1234/missing").replace("类型：science\n", ""))
        code, result, _ = self.run_main([path, "--allow-partial"])
        self.assertEqual(code, 0)
        self.assertEqual(result["status"], "partial")
        self.assertEqual(result["results"][0]["missing"], ["type", "data"])
        self.assertEqual(result["results"][0]["url"], "https://tenant.feishu.cn/docx/doc123")
        self.assertEqual(result["results"][0]["doc"], "doc123")

    def test_duplicate_citekey_rejected_before_child(self):
        paths = [self.note("MiXeD", filename="a.md"), self.note("mixed", filename="b.md", doi="10.9999/other")]
        with patch.object(batch.subprocess, "Popen") as popen:
            code, result, _ = self.run_main(paths)
        self.assertEqual(code, 2)
        self.assertIn("duplicate canonical citekey", result["reason"])
        popen.assert_not_called()

    def test_duplicate_percent_encoded_doi_rejected_before_child(self):
        paths = [self.note("one", "10.1234/Alpha%2Ftest"), self.note("two", "10.1234/alpha/test")]
        with patch.object(batch.subprocess, "Popen") as popen:
            code, result, _ = self.run_main(paths)
        self.assertEqual(code, 2)
        self.assertIn("duplicate canonical DOI", result["reason"])
        popen.assert_not_called()

    def test_invalid_path_or_note_rejected_before_child(self):
        good = self.note("good")
        bad = self.notes / "invalid.md"
        bad.write_bytes(b"\xff\xfe")
        for invalid in (str(self.notes / "absent"), str(self.notes), str(bad)):
            with self.subTest(invalid=invalid), patch.object(batch.subprocess, "Popen") as popen:
                code, result, _ = self.run_main([good, invalid])
                self.assertEqual(code, 2)
                self.assertIn("note 2", result["reason"])
                popen.assert_not_called()

    def test_bad_options_and_config_limit_never_start_children(self):
        path = self.note("valid")
        for args in ([path, "--workers", "0"], [path, "--workers", "9"],
                     [path, "--workers", "bad"], [path, "--unknown"]):
            with self.subTest(args=args), patch.object(batch.subprocess, "Popen") as popen, redirect_stderr(StringIO()):
                with self.assertRaises(SystemExit) as raised:
                    self.run_main(args)
                self.assertEqual(raised.exception.code, 2)
                popen.assert_not_called()
        for limit, args in ((2, [path, "--workers", "3"]), (0, [path]), (True, [path])):
            with self.subTest(limit=limit), patch.object(batch.subprocess, "Popen") as popen:
                code, result, _ = self.run_main(args, limit=limit)
                self.assertEqual(code, 2)
                self.assertEqual(result["status"], "failed")
                popen.assert_not_called()

    def test_argument_errors_do_not_reveal_unknown_values(self):
        err = StringIO()
        with patch.object(batch.subprocess, "Popen") as popen, redirect_stderr(err):
            with self.assertRaises(SystemExit):
                batch.main([self.note("args"), "--credential=SECRET_EXAMPLE"])
        self.assertNotIn("SECRET_EXAMPLE", err.getvalue())
        popen.assert_not_called()

    def test_child_arguments_are_not_shell_interpreted(self):
        path = self.note("safe", filename="路径 note & echo INJECTED.md")
        portable_path = Path(path).as_posix()
        data = "PDB & echo SECRET_EXAMPLE"
        with patch.object(batch.subprocess, "Popen", wraps=batch.subprocess.Popen) as popen:
            code, result, _ = self.run_main([portable_path, "--data", data])
        self.assertEqual(code, 0)
        command = popen.call_args.args[0]
        self.assertIs(popen.call_args.kwargs["shell"], False)
        self.assertEqual(command[3], str(Path(path).absolute()))
        self.assertEqual(command[4], "--data=" + data)
        self.assertEqual(json.loads((self.log / "safe.args").read_text())[1], "--data=" + data)
        self.assertNotIn("SECRET_EXAMPLE", json.dumps(result))

    def test_identifier_never_echoes_citekey_contents(self):
        path = self.note("SECRET_EXAMPLE", filename="paper.md")
        code, result, err = self.run_main([path, "--data", "PDB"])
        self.assertEqual(code, 0)
        self.assertEqual(len(result["results"][0]["citekey_id"]), 64)
        self.assertNotIn("SECRET_EXAMPLE", json.dumps(result) + err)

    def test_missing_fields_without_permission_never_report_success(self):
        path = self.note("missing")
        code, result, _ = self.run_main([path])
        self.assertEqual(code, 1)
        self.assertEqual(result["results"][0]["reason"], "missing_required_fields")

    def test_output_is_bounded_and_untrusted_child_data_is_not_relayed(self):
        paths = [self.note(key) for key in ("huge", "bad")]
        code, result, err = self.run_main(paths + ["--data", "PDB"])
        self.assertEqual(code, 1)
        self.assertLess(len(json.dumps(result)), 2000)
        self.assertNotIn("SECRET_EXAMPLE", json.dumps(result) + err)
        self.assertEqual(result["results"][0]["reason"], "child_output_unavailable_verify_before_retry")
        self.assertEqual(result["results"][1]["reason"], "invalid_child_result_verify_before_retry")

    def test_interrupt_reaps_started_children(self):
        paths = [self.note("first"), self.note("second"), self.note("third")]
        notes = batch.preflight(paths, "PDB")
        args = batch.arguments(paths + ["--data", "PDB"])
        started = []
        original = batch.start_child
        def fail_second(note, opts):
            if started:
                raise KeyboardInterrupt
            child = original(note, opts)
            started.append(child[0])
            return child
        with patch.object(batch, "PUBLISHER", self.child), patch.object(batch, "start_child", side_effect=fail_second):
            with self.assertRaises(batch.BatchStopped) as raised:
                batch.run_batch(notes, args, 2)
        self.assertEqual([result["status"] for result in raised.exception.results],
                         ["interrupted", "interrupted", "not_started"])
        self.assertEqual(len(started), 1)
        self.assertIsNotNone(started[0].poll())

    def test_cross_process_destination_cap_is_enforced_by_children(self):
        paths = [self.note("owner"), self.note("contender")]
        child = self.root / "locking_child.py"
        child.write_text(LOCK_CHILD, encoding="utf-8")
        cfg = self.root / "locking_config.json"
        cfg.write_text(json.dumps({"feishu": {"paper_concurrency": 1, "domain": "tenant.feishu.cn"}}), encoding="utf-8")
        started = self.root / "started"
        release = self.root / "release"
        env = os.environ.copy()
        env.update(SOUNDING_CONFIG=str(cfg), SOUNDING_FEISHU_STATE_DIR=str(self.root / "state"),
                   BATCH_SCRIPTS=str(ROOT / "scripts"), BATCH_STARTED=str(started), BATCH_RELEASE=str(release))
        wrapper = ("import sys; from pathlib import Path; sys.path.insert(0, sys.argv[1]); "
                   "import publish_batch as b; b.PUBLISHER=Path(sys.argv[2]); sys.exit(b.main(sys.argv[3:]))")
        def launch(path):
            return subprocess.Popen([sys.executable, "-B", "-c", wrapper, str(ROOT / "scripts"),
                                     str(child), path, "--workers", "1", "--data", "PDB"],
                                    stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env)
        owner = launch(paths[0])
        contender = None
        try:
            deadline = time.monotonic() + 15
            while not started.exists() and owner.poll() is None and time.monotonic() < deadline:
                time.sleep(0.02)
            self.assertTrue(started.exists(), "first child did not acquire its destination slot")
            contender = launch(paths[1])
            output, error = contender.communicate(timeout=15)
            self.assertEqual(contender.returncode, 1, error.decode("utf-8", "replace"))
            result = json.loads(output)
            self.assertEqual(result["results"][0]["status"], "busy")
            self.assertEqual(result["results"][0]["reason"], "destination_busy")
        finally:
            release.write_text("release", encoding="utf-8")
            if contender and contender.poll() is None:
                contender.kill()
                contender.wait()
            try:
                output, error = owner.communicate(timeout=15)
            except subprocess.TimeoutExpired:
                owner.kill()
                output, error = owner.communicate()
        self.assertEqual(owner.returncode, 0, error.decode("utf-8", "replace"))
        self.assertEqual(json.loads(output)["status"], "success")

    def test_signal_during_process_start_reaps_and_reports_child(self):
        path = self.note("signal")
        original = batch.start_child
        children = []
        def signal_during_start(note, args):
            child = original(note, args)
            children.append(child[0])
            signal.getsignal(signal.SIGTERM)(signal.SIGTERM, None)
            return child
        with patch.object(batch, "start_child", side_effect=signal_during_start):
            code, result, _ = self.run_main([path, "--data", "PDB"])
        self.assertEqual(code, 128 + signal.SIGTERM)
        self.assertEqual(result["status"], "interrupted")
        self.assertEqual(result["results"][0]["status"], "interrupted")
        self.assertIsNotNone(children[0].poll())

    def test_signal_after_last_child_keeps_completed_result(self):
        path = self.note("last")
        original = batch.result_for
        def signal_after_result(*args):
            result = original(*args)
            signal.getsignal(signal.SIGINT)(signal.SIGINT, None)
            return result
        with patch.object(batch, "result_for", side_effect=signal_after_result):
            code, result, _ = self.run_main([path, "--data", "PDB"])
        self.assertEqual(code, 128 + signal.SIGINT)
        self.assertEqual(result["status"], "interrupted")
        self.assertEqual(result["results"][0]["status"], "success")

    def test_pipe_reader_start_failure_reaps_the_child(self):
        path = self.note("reader")
        actual_start = batch.threading.Thread.start
        calls = []
        def fail_second_start(thread):
            calls.append(1)
            if len(calls) == 2:
                raise RuntimeError("synthetic reader start failure")
            return actual_start(thread)
        spawned = []
        actual_popen = batch.subprocess.Popen
        def capture_process(*args, **kwargs):
            process = actual_popen(*args, **kwargs)
            spawned.append(process)
            return process
        with patch.object(batch.threading.Thread, "start", fail_second_start), \
             patch.object(batch.subprocess, "Popen", side_effect=capture_process):
            code, result, _ = self.run_main([path, "--data", "PDB"])
        self.assertEqual(code, 1)
        self.assertEqual(result["status"], "incomplete")
        self.assertEqual(result["results"][0]["status"], "failed")
        self.assertEqual(len(spawned), 1)
        self.assertIsNotNone(spawned[0].poll())

    def test_actual_dry_run_does_not_write(self):
        paths = [self.note("first"), self.note("second")]
        workspace = self.notes / ".feishu-workspace.json"
        workspace.write_text(json.dumps({"app_token": "fake", "tables": {"papers": "fake"}}), encoding="utf-8")
        cfg = self.root / "config.json"
        cfg.write_text(json.dumps({"feishu": {"paper_concurrency": 2, "image_concurrency": 1}}), encoding="utf-8")
        before = {p.relative_to(self.root): p.read_bytes() for p in self.root.rglob("*") if p.is_file()}
        env = {k: v for k, v in os.environ.items() if not k.startswith(("FEISHU_", "SOUNDING_"))}
        env.update(SOUNDING_CONFIG=str(cfg), SOUNDING_FEISHU_STATE_DIR=str(self.root / "state"))
        command = [sys.executable, "-B", str(ROOT / "scripts" / "publish_batch.py"), *paths,
                   "--data", "PDB", "--dry-run", "--zotero"]
        run = subprocess.run(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env, timeout=30)
        self.assertEqual(run.returncode, 0, run.stderr.decode("utf-8", "replace") + run.stdout.decode("utf-8", "replace"))
        result = json.loads(run.stdout)
        self.assertEqual(result["status"], "success")
        self.assertTrue(result["dry_run"])
        after = {p.relative_to(self.root): p.read_bytes() for p in self.root.rglob("*") if p.is_file()}
        self.assertEqual(before, after)
        self.assertTrue(all(r["plan"] == "create" and "url" not in r and "doc" not in r
                            for r in result["results"]))
        self.assertFalse((self.notes / ".feishu-index.json").exists())
        self.assertFalse((self.root / "state").exists())


if __name__ == "__main__":
    unittest.main()

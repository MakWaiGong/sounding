import copy
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import feishu_api
import feishu_doc


class LocalDocument:
    def __init__(self, persistence=None):
        self.lock = threading.RLock()
        self.persistence = Path(persistence) if persistence else None
        self.roots = []
        self.blocks = {}
        self.counter = 0
        self.attempts = []
        self.clear_count = 0
        self.patch_count = 0
        self.active = 0
        self.max_active = 0
        self.closed = 0
        self.created = []
        self.fail = {}
        self.barrier = None
        self.patch_behavior = None
        self.crash_upload = False
        self.crash_clear = False
        if self.persistence and self.persistence.exists():
            saved = json.loads(self.persistence.read_text(encoding="utf-8"))
            for key in ("roots", "blocks", "counter", "attempts", "clear_count", "patch_count", "fail"):
                setattr(self, key, saved[key])

    def save(self):
        if self.persistence:
            data = {key: getattr(self, key) for key in
                    ("roots", "blocks", "counter", "attempts", "clear_count", "patch_count", "fail")}
            self.persistence.write_text(json.dumps(data), encoding="utf-8")

    def new_id(self):
        self.counter += 1
        return "block-%d" % self.counter

    def client(self, primary=False):
        client = LocalClient(self, primary)
        if not primary:
            self.created.append(client)
        return client

    def contents(self):
        return [self.blocks[block_id] for block_id in self.roots]


class LocalClient:
    def __init__(self, remote, primary=False):
        self.remote = remote
        self.primary = primary
        self.closed = False

    def close(self):
        self.closed = True
        with self.remote.lock:
            self.remote.closed += 1

    def post(self, path, payload, **kwargs):
        if path.endswith("/convert"):
            block = {"block_id": "converted", "block_type": 2,
                     "text": {"style": {}, "elements": [
                         {"text_run": {"content": payload["content"]}}]}}
            return {"blocks": [block], "first_level_block_ids": ["converted"]}
        raise AssertionError(path)

    def clear_body(self, doc_id):
        with self.remote.lock:
            self.remote.roots = []
            self.remote.blocks = {}
            self.remote.clear_count += 1
            self.remote.save()
            if self.remote.crash_clear:
                os._exit(72)

    def append_children(self, doc_id, children, index=-1):
        return self.append_block_children(doc_id, doc_id, children, index=index)

    def append_block_children(self, doc_id, parent, children, index=-1):
        result = []
        with self.remote.lock:
            for child in children:
                block = copy.deepcopy(child)
                block_id = self.remote.new_id()
                block.update(block_id=block_id, parent_id=parent)
                self.remote.blocks[block_id] = block
                if parent == doc_id:
                    self.remote.roots.append(block_id)
                else:
                    self.remote.blocks[parent].setdefault("children", []).append(block_id)
                if block["block_type"] == 19:
                    kid_id = self.remote.new_id()
                    block["children"] = [kid_id]
                    self.remote.blocks[kid_id] = {"block_id": kid_id, "parent_id": block_id,
                                                  "block_type": 2, "text": {"elements": []}}
                if block["block_type"] == 31:
                    prop = block["table"]["property"]
                    block["children"] = []
                    for _ in range(prop["row_size"] * prop["column_size"]):
                        cell_id = self.remote.new_id()
                        text_id = self.remote.new_id()
                        block["children"].append(cell_id)
                        self.remote.blocks[cell_id] = {"block_id": cell_id, "parent_id": block_id,
                                                       "block_type": 32, "children": [text_id]}
                        self.remote.blocks[text_id] = {"block_id": text_id, "parent_id": cell_id,
                                                       "block_type": 2, "text": {"elements": []}}
                result.append(copy.deepcopy(block))
            self.remote.save()
        return result

    def block_children(self, doc_id, block_id):
        with self.remote.lock:
            return [copy.deepcopy(self.remote.blocks[k]) for k in self.remote.blocks[block_id].get("children", [])]

    def batch_update_blocks(self, doc_id, updates):
        with self.remote.lock:
            for update in updates:
                self.remote.blocks[update["block_id"]]["text"]["elements"] = copy.deepcopy(
                    update["update_text_elements"]["elements"])
            self.remote.save()

    def doc_children(self, doc_id):
        with self.remote.lock:
            return copy.deepcopy(self.remote.contents())

    def all_blocks(self, doc_id):
        with self.remote.lock:
            return copy.deepcopy(list(self.remote.blocks.values()))

    def upload_media(self, blob, name, parent_type, parent_node, extra=None):
        assert not self.closed
        assert parent_type == "docx_image" and extra == {"drive_route_token": "doc"}
        with self.remote.lock:
            self.remote.active += 1
            self.remote.max_active = max(self.remote.max_active, self.remote.active)
            self.remote.attempts.append(name)
            self.remote.save()
            behavior = self.remote.fail.pop(name, None)
            self.remote.save()
        try:
            if self.remote.barrier and len(self.remote.attempts) <= self.remote.barrier.parties:
                self.remote.barrier.wait(timeout=10)
            if self.remote.crash_upload:
                os._exit(73)
            if behavior == "fail":
                raise feishu_api.FeishuError("upload", 400, "definite failure")
            if behavior == "uncertain":
                raise feishu_api.FeishuUncertainError("upload", -1, "outcome uncertain")
            if behavior == "no_token":
                return ""
            return "token:" + name
        finally:
            with self.remote.lock:
                self.remote.active -= 1

    def patch(self, path, payload):
        block_id = path.rsplit("/", 1)[1]
        with self.remote.lock:
            self.remote.patch_count += 1
            behavior, self.remote.patch_behavior = self.remote.patch_behavior, None
            if behavior != "before":
                self.remote.blocks[block_id]["image"]["token"] = payload["replace_image"]["token"]
            self.remote.save()
            if behavior == "exit_after":
                os._exit(74)
        if behavior in ("before", "after"):
            raise feishu_api.FeishuUncertainError("patch", -1, "outcome uncertain")
        return {}


def fixture(root):
    root = Path(root)
    figures = []
    for n in range(1, 4):
        path = root / ("fig%d.png" % n)
        if not path.exists():
            path.write_bytes(("image%d" % n).encode())
        figures.append((str(path), "Caption %d" % n, "fig%d" % n))
    return figures


NOTE = "1. Digest\n---\n## Results\nFirst paragraph discusses Fig. 1.\n\nSecond paragraph discusses Fig. 2.\n\nLast paragraph."


def run(remote, root, note=NOTE, doc_id="doc", workers=3, complete=False, figures=None):
    primary = remote.client(primary=True)
    figures = fixture(root) if figures is None else figures
    return feishu_doc.replace_body(primary, doc_id, note, figures,
                                   resume_path=str(Path(root) / "images.json"),
                                   client_factory=remote.client, max_workers=workers,
                                   previous_publication_complete=complete)


def child(root, behavior):
    remote = LocalDocument(Path(root) / "remote.json")
    if behavior == "fail":
        remote.fail["fig2.png"] = "fail"
    if behavior == "crash":
        remote.crash_upload = True
    if behavior == "crash_clear":
        remote.crash_clear = True
    if behavior == "crash_patch":
        remote.patch_behavior = "exit_after"
    try:
        run(remote, root, workers=1)
    except feishu_api.FeishuError:
        remote.save()
        return 2
    except ValueError:
        remote.save()
        return 3
    remote.save()
    return 0


class ConcurrentImageTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.remote = LocalDocument()

    def checkpoint(self):
        return json.loads((self.root / "images.json").read_text(encoding="utf-8"))

    def image_blocks(self):
        return [b for b in self.remote.contents() if b["block_type"] == 27]

    def test_parallel_order_independent_clients_and_legacy_equivalence(self):
        self.remote.barrier = threading.Barrier(2, timeout=10)
        self.assertEqual(run(self.remote, self.root, workers=2), "doc")
        self.assertGreater(self.remote.max_active, 1)
        self.assertEqual(len(self.remote.created), 2)
        self.assertTrue(all(client.closed for client in self.remote.created))
        self.assertEqual(self.remote.closed, 2)
        blocks = self.remote.contents()
        image_positions = [i for i, b in enumerate(blocks) if b["block_type"] == 27]
        self.assertEqual(len(image_positions), 3)
        for n, index in enumerate(image_positions, 1):
            self.assertEqual(blocks[index]["image"]["token"], "token:fig%d.png" % n)
            self.assertEqual(blocks[index + 1]["text"]["elements"][0]["text_run"]["content"], "Caption %d" % n)
        legacy = LocalDocument()
        primary = legacy.client(primary=True)
        self.assertEqual(feishu_doc.replace_body(primary, "doc", NOTE, fixture(self.root)), "doc")
        def projection(remote):
            return [(b["block_type"], b.get("text"), b.get("image", {}).get("token"))
                    for b in remote.contents()]
        self.assertEqual(projection(self.remote), projection(legacy))

    def test_definite_failure_resumes_without_reupload_or_rebuild(self):
        self.remote.fail["fig2.png"] = "fail"
        with self.assertRaises(feishu_api.FeishuError):
            run(self.remote, self.root)
        states = {x["label"]: x["status"] for x in self.checkpoint()["images"]}
        self.assertEqual(states, {"fig1": "confirmed", "fig2": "pending", "fig3": "confirmed"})
        ids = self.remote.roots[:]
        self.assertEqual(run(self.remote, self.root), "doc")
        self.assertEqual(self.remote.roots, ids)
        self.assertEqual(self.remote.clear_count, 1)
        self.assertEqual(self.remote.attempts.count("fig1.png"), 1)
        self.assertEqual(self.remote.attempts.count("fig2.png"), 2)
        self.assertEqual(self.remote.attempts.count("fig3.png"), 1)
        self.assertTrue(all(client.closed for client in self.remote.created))

    def test_real_process_restart_preserves_confirmed_uploads(self):
        def execute(behavior):
            env = dict(os.environ, PYTHONDONTWRITEBYTECODE="1")
            return subprocess.run([sys.executable, __file__, "--child", str(self.root), behavior],
                                  capture_output=True, text=True, env=env, timeout=60)
        first = execute("fail")
        self.assertEqual(first.returncode, 2, first.stderr)
        checkpoint = self.checkpoint()
        roots = checkpoint["snapshot"]["roots"]
        second = execute("resume")
        self.assertEqual(second.returncode, 0, second.stderr)
        remote = LocalDocument(self.root / "remote.json")
        self.assertEqual(remote.roots, roots)
        self.assertEqual(remote.clear_count, 1)
        self.assertEqual(remote.attempts.count("fig1.png"), 1)
        self.assertEqual(remote.attempts.count("fig2.png"), 2)
        self.assertEqual(remote.attempts.count("fig3.png"), 1)

    def test_process_crash_after_patch_reconciles_without_reupload(self):
        def execute(behavior):
            env = dict(os.environ, PYTHONDONTWRITEBYTECODE="1")
            return subprocess.run([sys.executable, __file__, "--child", str(self.root), behavior],
                                  capture_output=True, text=True, env=env, timeout=60)
        first = execute("crash_patch")
        self.assertEqual(first.returncode, 74, first.stderr)
        self.assertEqual(self.checkpoint()["images"][0]["status"], "patching")
        second = execute("resume")
        self.assertEqual(second.returncode, 0, second.stderr)
        remote = LocalDocument(self.root / "remote.json")
        self.assertEqual(remote.clear_count, 1)
        self.assertEqual(remote.attempts.count("fig1.png"), 1)
        self.assertEqual(remote.patch_count, 3)

    def test_process_crash_during_skeleton_never_clears_again(self):
        def execute(behavior):
            env = dict(os.environ, PYTHONDONTWRITEBYTECODE="1")
            return subprocess.run([sys.executable, __file__, "--child", str(self.root), behavior],
                                  capture_output=True, text=True, env=env, timeout=60)
        first = execute("crash_clear")
        self.assertEqual(first.returncode, 72, first.stderr)
        self.assertEqual(self.checkpoint()["phase"], "building")
        second = execute("resume")
        self.assertEqual(second.returncode, 3, second.stderr)
        remote = LocalDocument(self.root / "remote.json")
        self.assertEqual(remote.clear_count, 1)

    def test_resume_from_another_clone_with_same_figure_identity(self):
        run(self.remote, self.root)
        clone = self.root / "clone"
        clone.mkdir()
        attempts = self.remote.attempts[:]
        self.assertEqual(feishu_doc.replace_body(
            self.remote.client(primary=True), "doc", NOTE, fixture(clone),
            resume_path=str(self.root / "images.json"), client_factory=self.remote.client), "doc")
        self.assertEqual(self.remote.attempts, attempts)
        self.assertEqual(self.remote.clear_count, 1)
        changed = fixture(clone)
        changed[0] = (changed[0][0], "modified caption", changed[0][2])
        with self.assertRaisesRegex(ValueError, "does not match"):
            feishu_doc.replace_body(self.remote.client(primary=True), "doc", NOTE, changed,
                                    resume_path=str(self.root / "images.json"), client_factory=self.remote.client)
        self.assertEqual(self.remote.clear_count, 1)

    def test_process_crash_during_upload_is_not_replayed(self):
        def execute(behavior):
            env = dict(os.environ, PYTHONDONTWRITEBYTECODE="1")
            return subprocess.run([sys.executable, __file__, "--child", str(self.root), behavior],
                                  capture_output=True, text=True, env=env, timeout=60)
        first = execute("crash")
        self.assertEqual(first.returncode, 73, first.stderr)
        state = self.checkpoint()
        self.assertEqual([image["status"] for image in state["images"]],
                         ["uploading", "pending", "pending"])
        remote = LocalDocument(self.root / "remote.json")
        self.assertEqual(remote.attempts, ["fig1.png"])
        second = execute("resume")
        self.assertEqual(second.returncode, 3, second.stderr)
        remote = LocalDocument(self.root / "remote.json")
        self.assertEqual(remote.attempts, ["fig1.png"])
        self.assertEqual(remote.clear_count, 1)

    def test_completed_revisions_support_changed_inputs_and_empty_figures(self):
        run(self.remote, self.root)
        original_roots = self.remote.roots[:]
        original_attempts = self.remote.attempts[:]
        self.assertEqual(run(self.remote, self.root), "doc")
        self.assertEqual(self.remote.roots, original_roots)
        self.assertEqual(self.remote.attempts, original_attempts)
        changed_note = NOTE + "\n\nA revised conclusion."
        self.assertEqual(run(self.remote, self.root, note=changed_note, complete=True), "doc")
        self.assertEqual(self.remote.clear_count, 2)
        self.assertNotEqual(self.remote.roots, original_roots)
        (self.root / "fig1.png").write_bytes(b"a different image")
        changed_figures = fixture(self.root)
        changed_figures[1] = (changed_figures[1][0], "revised caption", changed_figures[1][2])
        self.assertEqual(run(self.remote, self.root, note=changed_note, complete=True,
                             figures=changed_figures), "doc")
        self.assertEqual(self.remote.clear_count, 3)
        self.assertEqual(run(self.remote, self.root, note=changed_note, complete=True, figures=[]), "doc")
        self.assertEqual(self.remote.clear_count, 4)
        self.assertEqual(self.image_blocks(), [])
        self.assertEqual(self.checkpoint()["images"], [])
        figureless_roots = self.remote.roots[:]
        attempts = self.remote.attempts[:]
        self.assertEqual(run(self.remote, self.root, note=changed_note, figures=[]), "doc")
        self.assertEqual(self.remote.clear_count, 4)
        self.assertEqual(self.remote.roots, figureless_roots)
        self.assertEqual(self.remote.attempts, attempts)

    def test_transition_requires_complete_old_journal_and_unchanged_document(self):
        self.remote.fail["fig1.png"] = "fail"
        with self.assertRaises(feishu_api.FeishuError):
            run(self.remote, self.root)
        original = self.checkpoint()
        with self.assertRaisesRegex(ValueError, "does not match"):
            run(self.remote, self.root, note=NOTE + " changed")
        with self.assertRaisesRegex(ValueError, "not fully confirmed"):
            run(self.remote, self.root, note=NOTE + " changed", complete=True)
        self.assertEqual(self.checkpoint(), original)
        self.assertEqual(self.remote.clear_count, 1)
        run(self.remote, self.root)
        self.assertEqual(self.remote.clear_count, 1)
        before = self.checkpoint()
        attempts = self.remote.attempts[:]
        with self.assertRaisesRegex(ValueError, "not fully confirmed"):
            run(self.remote, self.root, doc_id="other", complete=True)
        self.assertEqual(self.checkpoint(), before)
        self.assertEqual(self.remote.attempts, attempts)
        self.assertEqual(self.remote.clear_count, 1)

    def test_changed_revision_with_remote_drift_does_not_clear(self):
        run(self.remote, self.root)
        before = self.checkpoint()
        attempts = self.remote.attempts[:]
        self.image_blocks()[0]["image"]["token"] = "manually changed"
        with self.assertRaisesRegex(ValueError, "confirmed remote image"):
            run(self.remote, self.root, note=NOTE + " changed", complete=True)
        self.assertEqual(self.checkpoint(), before)
        self.assertEqual(self.remote.clear_count, 1)
        self.assertEqual(self.remote.attempts, attempts)

    def test_interrupted_changed_revision_resumes_without_prior_complete_flag(self):
        run(self.remote, self.root)
        changed_note = NOTE + "\nnew revision"
        self.remote.fail["fig2.png"] = "fail"
        with self.assertRaises(feishu_api.FeishuError):
            run(self.remote, self.root, note=changed_note, complete=True)
        state = self.checkpoint()
        self.assertEqual(state["phase"], "ready")
        self.assertEqual(self.remote.clear_count, 2)
        attempts = self.remote.attempts[:]
        confirmed = [image["label"] + ".png" for image in state["images"] if image["status"] == "confirmed"]
        self.assertTrue(confirmed)
        roots = self.remote.roots[:]
        self.assertEqual(run(self.remote, self.root, note=changed_note), "doc")
        self.assertEqual(self.remote.roots, roots)
        self.assertEqual(self.remote.clear_count, 2)
        for name in confirmed:
            self.assertEqual(self.remote.attempts.count(name), attempts.count(name))

    def test_interrupted_revision_skeleton_cannot_restart_or_rebuild(self):
        run(self.remote, self.root)
        changed_note = NOTE + " changed"
        primary = self.remote.client(primary=True)
        original = primary.clear_body
        def uncertain_clear(doc_id):
            original(doc_id)
            raise feishu_api.FeishuUncertainError("clear", -1, "uncertain")
        primary.clear_body = uncertain_clear
        with self.assertRaises(feishu_api.FeishuUncertainError):
            feishu_doc.replace_body(primary, "doc", changed_note, fixture(self.root),
                                    resume_path=str(self.root / "images.json"),
                                    client_factory=self.remote.client, previous_publication_complete=True)
        self.assertEqual(self.checkpoint()["phase"], "building")
        with self.assertRaisesRegex(ValueError, "incomplete"):
            run(self.remote, self.root, note=changed_note)
        self.assertEqual(self.remote.clear_count, 2)

    def test_local_preflight_rejects_bad_inputs_before_any_write(self):
        primary = self.remote.client(primary=True)
        primary.append_children("doc", [{"block_type": 2, "text": {"elements": [
            {"text_run": {"content": "existing user text"}}]}}])
        before_blocks = copy.deepcopy(self.remote.blocks)
        before_roots = self.remote.roots[:]
        good = fixture(self.root)
        bad_cases = [
            [(str(self.root / "missing.png"), "caption", "fig1")],
            [(good[0][0], "caption", "fig1junk")],
            [(good[0][0], "caption", "missing-number")],
            [good[0], (good[1][0], "caption", "figure_01")],
            [(good[0][0], None, "fig1")],
            [(good[0][0], "caption", None)],
        ]
        path = self.root / "images.json"
        for figures in bad_cases:
            with self.subTest(figures=figures):
                with self.assertRaises(ValueError):
                    feishu_doc.validate_figure_inputs("preflight", NOTE, figures)
                with self.assertRaises(ValueError):
                    feishu_doc.replace_body(primary, "doc", NOTE, figures,
                                            resume_path=str(path), client_factory=self.remote.client)
                with self.assertRaises(ValueError):
                    feishu_doc.replace_body(primary, "doc", NOTE, figures)
                self.assertEqual(self.remote.blocks, before_blocks)
                self.assertEqual(self.remote.roots, before_roots)
                self.assertEqual(self.remote.clear_count, 0)
                self.assertEqual(self.remote.attempts, [])
                self.assertFalse(path.exists())
                self.assertFalse((self.root / "images.json.lock").exists())
        self.assertIsNone(feishu_doc.validate_figure_inputs("preflight", NOTE, good))
        self.assertEqual(self.remote.blocks, before_blocks)
        self.assertFalse(path.exists())

    def test_unreadable_figure_preflight_preserves_remote_and_journal(self):
        from unittest.mock import patch
        figures = fixture(self.root)
        real_open = open
        def denied(path, mode="r", *args, **kwargs):
            if str(path) == figures[0][0] and mode == "rb":
                raise PermissionError("fixture unreadable")
            return real_open(path, mode, *args, **kwargs)
        primary = self.remote.client(primary=True)
        primary.append_children("doc", [{"block_type": 2, "text": {"elements": []}}])
        before = copy.deepcopy(self.remote.blocks)
        with patch("builtins.open", side_effect=denied):
            with self.assertRaisesRegex(ValueError, "missing or unreadable"):
                feishu_doc.validate_figure_inputs("preflight", NOTE, figures)
            with self.assertRaisesRegex(ValueError, "missing or unreadable"):
                feishu_doc.replace_body(primary, "doc", NOTE, figures,
                                        resume_path=str(self.root / "images.json"),
                                        client_factory=self.remote.client)
        self.assertEqual(self.remote.blocks, before)
        self.assertEqual(self.remote.clear_count, 0)
        self.assertFalse((self.root / "images.json").exists())

    def test_changed_note_document_or_image_rejected_before_writes(self):
        run(self.remote, self.root)
        writes = (self.remote.clear_count, self.remote.patch_count, self.remote.attempts[:])
        with self.assertRaisesRegex(ValueError, "does not match"):
            run(self.remote, self.root, note=NOTE + " changed")
        primary = self.remote.client(primary=True)
        with self.assertRaisesRegex(ValueError, "does not match"):
            feishu_doc.replace_body(primary, "other-doc", NOTE, fixture(self.root),
                                    resume_path=str(self.root / "images.json"), client_factory=self.remote.client)
        (self.root / "fig1.png").write_bytes(b"changed")
        with self.assertRaisesRegex(ValueError, "does not match"):
            run(self.remote, self.root)
        self.assertEqual((self.remote.clear_count, self.remote.patch_count, self.remote.attempts), writes)

    def test_malformed_checkpoint_fails_closed(self):
        path = self.root / "images.json"
        for invalid in ("{", "{}", '{"version":1,"version":1}'):
            with self.subTest(invalid=invalid):
                path.write_text(invalid, encoding="utf-8")
                with self.assertRaises(ValueError):
                    run(self.remote, self.root)
        self.assertEqual(self.remote.clear_count, 0)
        path.unlink()
        run(self.remote, self.root)
        state = self.checkpoint()
        state["images"][0]["status"] = "pending"
        path.write_text(json.dumps(state), encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "checksum"):
            run(self.remote, self.root)
        self.assertEqual(self.remote.clear_count, 1)

    def test_remote_drift_rejected_without_rebuild(self):
        run(self.remote, self.root)
        writes = (self.remote.clear_count, self.remote.patch_count, self.remote.attempts[:])
        original = copy.deepcopy(self.remote.blocks)
        roots = self.remote.roots[:]
        changes = ["text", "image", "missing", "order", "nested"]
        for change in changes:
            with self.subTest(change=change):
                self.remote.blocks = copy.deepcopy(original)
                self.remote.roots = roots[:]
                if change == "text":
                    next(b for b in self.remote.blocks.values() if b.get("text", {}).get("elements"))["text"]["elements"] = []
                elif change == "image":
                    self.image_blocks()[0]["image"]["token"] = "unexpected"
                elif change == "missing":
                    del self.remote.blocks[self.remote.roots[-1]]
                elif change == "order":
                    self.remote.roots.reverse()
                else:
                    nested = next(b for b in self.remote.blocks.values() if b.get("parent_id", "").startswith("block-"))
                    nested["parent_id"] = "changed"
                with self.assertRaises((ValueError, KeyError)):
                    run(self.remote, self.root)
                self.assertEqual((self.remote.clear_count, self.remote.patch_count, self.remote.attempts), writes)

    def test_uncertain_patch_readback_reconciles_or_reuses_uploaded_token(self):
        self.remote.patch_behavior = "after"
        run(self.remote, self.root)
        self.assertEqual(self.remote.attempts.count("fig1.png"), 1)
        self.assertEqual(self.remote.patch_count, 3)
        other = self.root / "other"
        other.mkdir()
        remote = LocalDocument()
        remote.patch_behavior = "before"
        with self.assertRaises(feishu_api.FeishuUncertainError):
            run(remote, other, workers=1)
        attempts_before = remote.attempts[:]
        self.assertEqual(run(remote, other, workers=1), "doc")
        self.assertEqual(attempts_before, ["fig1.png"])
        self.assertEqual(remote.attempts, ["fig1.png", "fig2.png", "fig3.png"])
        self.assertEqual(remote.clear_count, 1)

    def test_incomplete_callout_and_table_are_not_checkpointed_as_ready(self):
        for kind in ("callout", "table"):
            with self.subTest(kind=kind):
                folder = self.root / kind
                folder.mkdir()
                remote = LocalDocument()
                primary = remote.client(primary=True)
                note = NOTE
                if kind == "callout":
                    primary.block_children = lambda doc_id, block_id: []
                else:
                    note = "| A | B |\n| --- | --- |\n| C | D |"
                    original_all_blocks = primary.all_blocks
                    primary.all_blocks = lambda doc_id: [b for b in original_all_blocks(doc_id)
                                                         if b.get("block_type") != 32]
                with self.assertRaisesRegex(ValueError, "incomplete"):
                    feishu_doc.replace_body(primary, "doc", note, fixture(folder),
                                            resume_path=str(folder / "images.json"),
                                            client_factory=remote.client)
                state = json.loads((folder / "images.json").read_text(encoding="utf-8"))
                self.assertEqual(state["phase"], "building")
                self.assertEqual(remote.attempts, [])
                with self.assertRaisesRegex(ValueError, "incomplete"):
                    run(remote, folder, note=note)
                self.assertEqual(remote.clear_count, 1)

    def test_unapplied_skeleton_text_update_is_rejected(self):
        primary = self.remote.client(primary=True)
        primary.batch_update_blocks = lambda doc_id, updates: []
        with self.assertRaisesRegex(ValueError, "text update was not confirmed"):
            feishu_doc.replace_body(primary, "doc", NOTE, fixture(self.root),
                                    resume_path=str(self.root / "images.json"),
                                    client_factory=self.remote.client)
        self.assertEqual(self.checkpoint()["phase"], "building")
        self.assertEqual(self.remote.attempts, [])

    def test_complete_table_and_callout_are_verified_and_resumable(self):
        note = "1. Digest\n---\n## Results\n| A | B |\n| --- | --- |\n| C | D |\n\nFig. 1 explains the result."
        run(self.remote, self.root, note=note)
        tables = [b for b in self.remote.blocks.values() if b["block_type"] == 31]
        self.assertEqual(len(tables), 1)
        cells = [b for b in self.remote.blocks.values() if b["block_type"] == 32]
        self.assertEqual(len(cells), 4)
        contents = [self.remote.blocks[cell["children"][0]]["text"]["elements"][0]["text_run"]["content"]
                    for cell in cells]
        self.assertEqual(contents, ["A", "B", "C", "D"])
        attempts = self.remote.attempts[:]
        self.assertEqual(run(self.remote, self.root, note=note), "doc")
        self.assertEqual(self.remote.attempts, attempts)
        self.assertEqual(self.remote.clear_count, 1)

    def test_same_client_factory_is_rejected_and_closed(self):
        shared = self.remote.client()
        started = threading.Event()
        original = shared.upload_media
        def upload(*args, **kwargs):
            if not started.wait(5):
                raise RuntimeError("second worker did not start")
            return original(*args, **kwargs)
        shared.upload_media = upload
        calls = []
        def factory():
            calls.append(True)
            if len(calls) == 2:
                started.set()
            return shared
        primary = self.remote.client(primary=True)
        with self.assertRaisesRegex(ValueError, "independent client"):
            feishu_doc.replace_body(primary, "doc", NOTE, fixture(self.root),
                                    resume_path=str(self.root / "images.json"),
                                    client_factory=factory, max_workers=2)
        self.assertGreaterEqual(len(calls), 2)
        self.assertTrue(shared.closed)
        self.assertEqual(self.remote.closed, 1)

    def test_uncertain_skeleton_does_not_rebuild(self):
        primary = self.remote.client(primary=True)
        original = primary.clear_body
        def uncertain(doc_id):
            original(doc_id)
            raise feishu_api.FeishuUncertainError("clear", -1, "uncertain")
        primary.clear_body = uncertain
        with self.assertRaises(feishu_api.FeishuUncertainError):
            feishu_doc.replace_body(primary, "doc", NOTE, fixture(self.root),
                                    resume_path=str(self.root / "images.json"), client_factory=self.remote.client)
        with self.assertRaisesRegex(ValueError, "incomplete"):
            run(self.remote, self.root)
        self.assertEqual(self.remote.clear_count, 1)

    def test_uncertain_and_missing_upload_token_block_restart(self):
        for behavior in ("uncertain", "no_token"):
            with self.subTest(behavior=behavior):
                folder = self.root / behavior
                folder.mkdir()
                remote = LocalDocument()
                remote.fail["fig1.png"] = behavior
                with self.assertRaises((ValueError, feishu_api.FeishuUncertainError)):
                    run(remote, folder, workers=1)
                attempts = remote.attempts[:]
                with self.assertRaisesRegex(ValueError, "upload outcome is uncertain"):
                    run(remote, folder, workers=1)
                self.assertEqual(remote.attempts, attempts)
                self.assertEqual(remote.clear_count, 1)

    def test_worker_factory_reuse_and_invalid_options_fail(self):
        primary = self.remote.client(primary=True)
        with self.assertRaises(ValueError):
            feishu_doc.replace_body(primary, "doc", NOTE, fixture(self.root), resume_path=str(self.root / "bad"))
        with self.assertRaises(ValueError):
            feishu_doc.replace_body(primary, "doc", NOTE, fixture(self.root), client_factory=self.remote.client)
        with self.assertRaises(ValueError):
            feishu_doc.replace_body(primary, "doc", NOTE, fixture(self.root), resume_path="relative",
                                    client_factory=self.remote.client)
        with self.assertRaises(ValueError):
            feishu_doc.replace_body(primary, "doc", NOTE, fixture(self.root), resume_path=str(self.root / "bad"),
                                    client_factory=self.remote.client, max_workers=0)
        self.assertEqual(self.remote.clear_count, 0)


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "--child":
        sys.exit(child(sys.argv[2], sys.argv[3]))
    unittest.main()

"""Record resumable reading/publishing progress; never contact external services.

Receipts describe the agent's observed results. A recorded remote step is not an
independent network verification. All receipts are bound to the current source;
note-dependent receipts are also bound to the exact Markdown bytes.
"""
import argparse
import copy
import hashlib
import json
import os
from pathlib import Path
import re
import tempfile
from datetime import datetime, timezone
from urllib.parse import urlparse


import config


STEPS = ("pdf_prepared", "note_validated", "notion_written", "images_verified",
         "zotero_written")
DEPENDENCIES = {
    "pdf_prepared": (), "note_validated": ("pdf_prepared",),
    "notion_written": ("note_validated",),
    "images_verified": ("notion_written",), "zotero_written": ("notion_written",),
}
FEISHU_STEPS = ("pdf_prepared", "note_validated", "feishu_written", "images_verified", "pdf_uploaded")
ALL_STEPS = ("pdf_prepared", "note_validated", "notion_written", "feishu_written", "images_verified", "pdf_uploaded", "zotero_written")
PUBLICATION_STEPS = {
    "feishu": ("pdf_prepared", "note_validated", "feishu_written", "images_verified"),
    "notion": ("pdf_prepared", "note_validated", "notion_written", "images_verified"),
}
ARCHIVAL_STEPS = ("pdf_uploaded", "zotero_written")
DEPENDENCIES["feishu_written"] = ("note_validated",)
DEPENDENCIES["pdf_uploaded"] = ()


def dependencies(state, step):
    return tuple("feishu_written" if dep == "notion_written" and state.get("backend") == "feishu" else dep
                 for dep in DEPENDENCIES[step])


HASH_RE = re.compile(r"^[0-9a-f]{64}$")


def now():
    return datetime.now(timezone.utc).isoformat()


def file_hash(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def atomic_json(path, data):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as stream:
            json.dump(data, stream, ensure_ascii=False, indent=2)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def require_text(value, field):
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field} must be a non-empty string")
    return value


def check_hash(value, field):
    if not isinstance(value, str) or not HASH_RE.fullmatch(value):
        raise ValueError(f"{field} must be a lowercase SHA-256 digest")
    return value


def validate_timings(receipt, result):
    timings = receipt.get("timings_ms")
    if timings is None:
        return
    if (not isinstance(timings, dict) or
            any(not isinstance(key, str) or not key.strip() for key in timings) or
            any(type(value) is not int or value < 0 for value in timings.values())):
        raise ValueError("timings_ms must map non-empty names to non-negative integers")
    result["timings_ms"] = dict(timings)


def load_state(path):
    with Path(path).open(encoding="utf-8-sig") as stream:
        state = json.load(stream)
    if not isinstance(state, dict) or state.get("schema_version") != 1:
        raise ValueError("unsupported run state schema")
    require_text(state.get("citekey"), "citekey")
    check_hash(state.get("source_sha256"), "source_sha256")
    require_text(state.get("source_path"), "source_path")
    if state.get("note_path") is not None:
        require_text(state["note_path"], "note_path")
        check_hash(state.get("note_sha256"), "note_sha256")
    elif state.get("note_sha256") is not None:
        raise ValueError("note_sha256 requires note_path")
    required = state.get("required_steps")
    if (not isinstance(required, list) or not required or
            any(step not in ALL_STEPS for step in required) or len(set(required)) != len(required)):
        raise ValueError("required_steps must be a non-empty unique list of known steps")
    backend = state.get("backend", "notion")
    if backend not in ("feishu", "notion"):
        raise ValueError("state backend must be feishu or notion")
    if ("notion_written" if backend == "feishu" else "feishu_written") in required:
        raise ValueError("state publication step does not match backend")
    records = state.get("steps")
    if not isinstance(records, dict) or any(step not in required for step in records):
        raise ValueError("state contains unknown or unconfigured steps")
    for step, record in records.items():
        if not isinstance(record, dict) or record.get("status") not in ("done", "failed"):
            raise ValueError(f"invalid state for {step}")
        if record.get("source_sha256") != state["source_sha256"]:
            raise ValueError(f"{step} has a different source fingerprint")
        if step not in ("pdf_prepared", "pdf_uploaded") and record.get("note_sha256") != state.get("note_sha256"):
            raise ValueError(f"{step} has a different note fingerprint")
        if record["status"] == "done":
            validate_receipt(state, step, record.get("evidence"), check_files=False)
        else:
            require_text(record.get("error"), f"{step}.error")
    return state


def init_state(path, citekey, source, note=None, steps=None, backend=None, cfg=None):
    require_text(citekey, "citekey")
    source = Path(source).resolve(strict=True)
    source_hash = file_hash(source)
    old = load_state(path) if Path(path).exists() else None
    if old and old["citekey"] != citekey:
        raise ValueError("run file belongs to a different citekey; use another run file")
    backend = backend or (old.get("backend", "notion") if old else "notion")
    if backend not in ("feishu", "notion"):
        raise ValueError("backend must be feishu or notion")
    target = config.feishu_destination(cfg) if backend == "feishu" else None
    defaults = FEISHU_STEPS if backend == "feishu" else STEPS
    selected = list(steps or (old["required_steps"] if old and old.get("backend", "notion") == backend else defaults))
    wrong_step = "notion_written" if backend == "feishu" else "feishu_written"
    if wrong_step in selected:
        raise ValueError("publication step must match backend")
    if not selected or len(set(selected)) != len(selected) or any(x not in ALL_STEPS for x in selected):
        raise ValueError("steps must be a non-empty unique subset of: " + ", ".join(ALL_STEPS))
    note_path = str(Path(note).resolve(strict=True)) if note else (old.get("note_path") if old else None)
    note_hash = file_hash(note_path) if note_path else None
    state = copy.deepcopy(old) if old else {
        "schema_version": 1, "citekey": citekey, "created_at": now(),
        "steps": {}, "invalidations": [],
    }
    invalid = []
    reasons = []
    if old and old["source_sha256"] != source_hash:
        invalid = list(state["steps"])
        reasons.append("source_changed")
    elif old and old.get("note_sha256") != note_hash:
        invalid = [s for s in state["steps"] if s not in ("pdf_prepared", "pdf_uploaded")]
        reasons.append("note_changed")
    if old and (old.get("backend", "notion") != backend or old.get("destination") != target):
        invalid += [s for s in state["steps"] if s not in ("pdf_prepared", "note_validated")]
        reasons.append("publication_destination_changed")
    invalid += [s for s in state["steps"] if s not in selected]
    if old and selected != old["required_steps"]:
        reasons.append("required_steps_changed")
    if reasons:
        state.setdefault("invalidations", []).append({
            "at": now(), "reasons": reasons, "steps": sorted(set(invalid)),
            "previous_source_sha256": old["source_sha256"],
            "previous_note_sha256": old.get("note_sha256"),
        })
    for step in invalid:
        state["steps"].pop(step, None)
    state.update(source_path=str(source), source_sha256=source_hash,
                 note_path=note_path, note_sha256=note_hash,
                 required_steps=selected, backend=backend, destination=target, updated_at=now())
    atomic_json(path, state)
    return status(state)


def changed_files(state):
    changed = []
    for label, path_key, hash_key in (("source", "source_path", "source_sha256"),
                                      ("note", "note_path", "note_sha256")):
        path = state.get(path_key)
        if not path:
            continue
        try:
            if file_hash(path) != state[hash_key]:
                changed.append(label + "_changed")
        except OSError:
            changed.append(label + "_unavailable")
    return changed


def validate_receipt(state, step, receipt, check_files=True):
    if not isinstance(receipt, dict):
        raise ValueError("evidence must be a JSON object")
    source_hash = receipt.get("source_sha256", receipt.get("pdf_sha256"))
    if source_hash != state["source_sha256"]:
        raise ValueError("receipt source_sha256/pdf_sha256 does not match this run")
    if step == "pdf_uploaded":
        if state.get("backend") != "feishu":
            raise ValueError("pdf_uploaded is only valid for Feishu archival")
        target = state.get("destination")
        if not target or receipt.get("destination") != target:
            raise ValueError("PDF upload destination must match the configured Feishu target")
        record_id = require_text(receipt.get("record_id"), "record_id")
        if receipt.get("readback_record_id") != record_id or receipt.get("readback_checked") is not True:
            raise ValueError("PDF upload requires same-record readback")
        published = state["steps"].get("feishu_written", {}).get("evidence", {})
        if published and published.get("record_id") != record_id:
            raise ValueError("PDF upload record_id differs from the recorded publication")
        token = receipt.get("attachment_token") or receipt.get("file_token")
        token = require_text(token, "attachment_token/file_token")
        return {"source_sha256": source_hash, "destination": target, "record_id": record_id,
                "readback_record_id": record_id, "readback_checked": True,
                "attachment_token": token}
    if step == "pdf_prepared":
        count = receipt.get("page_count")
        if type(count) is not int or count < 1:
            raise ValueError("pdf_prepared requires positive page_count")
        return {"source_sha256": source_hash, "page_count": count}
    if not state.get("note_sha256") or receipt.get("note_sha256") != state["note_sha256"]:
        raise ValueError("receipt requires the current note_sha256; init with --note first")
    result = {"source_sha256": source_hash, "note_sha256": receipt["note_sha256"]}
    if step == "note_validated":
        if receipt.get("valid") is not True or receipt.get("errors") != []:
            raise ValueError("note_validated requires valid=true and errors=[]")
        if receipt.get("citekey") != state["citekey"]:
            raise ValueError("validation report citekey does not match this run")
        result.update(valid=True, errors=[], citekey=receipt["citekey"])
        validate_timings(receipt, result)
        return result
    if receipt.get("readback_checked") is not True:
        raise ValueError("remote receipts require readback_checked=true from an actual readback")
    feishu = state.get("backend") == "feishu"
    id_field = "document_id" if feishu else "page_id"
    remote_id = require_text(receipt.get(id_field), id_field)
    result.update({id_field: remote_id, "readback_checked": True})
    validate_timings(receipt, result)
    written_step = "feishu_written" if feishu else "notion_written"
    published = state["steps"].get(written_step, {}).get("evidence", {})
    if step != written_step and published and published.get(id_field) != remote_id:
        raise ValueError(f"receipt {id_field} differs from the recorded publication")
    if feishu:
        target = state.get("destination")
        if not target or receipt.get("destination") != target:
            raise ValueError("Feishu receipt destination must match the configured target")
        if receipt.get("readback_document_id") != remote_id:
            raise ValueError("Feishu requires same-document readback_document_id")
        result.update(destination=target, readback_document_id=remote_id)
        if step == "feishu_written":
            for field in ("record_id", "document_url"):
                result[field] = require_text(receipt.get(field), field)
            document_url = urlparse(result["document_url"])
            if (document_url.scheme != "https" or not document_url.hostname
                    or not document_url.hostname.endswith(".feishu.cn")
                    or not document_url.path.startswith(("/docx/", "/wiki/"))):
                raise ValueError("Feishu document_url must be the actual docx/wiki body, not the Bitable index")
            if receipt.get("readback_record_id") != result["record_id"]:
                raise ValueError("Feishu requires same-record readback_record_id")
            if receipt.get("domain_preserved") is not True:
                raise ValueError("Feishu requires domain_preserved=true; domain is USER-owned")
            result.update(readback_record_id=result["record_id"], domain_preserved=True)
    if step == "images_verified":
        images = receipt.get("images")
        if not isinstance(images, list):
            raise ValueError("images_verified requires an images list")
        if not images and not isinstance(receipt.get("no_images_reason"), str):
            raise ValueError("empty images requires no_images_reason")
        if not images:
            result["no_images_reason"] = require_text(receipt["no_images_reason"], "no_images_reason")
        result["images"] = []
        for entry in images:
            if not isinstance(entry, dict):
                raise ValueError("each image must be an object")
            image_path = require_text(entry.get("path"), "image.path")
            digest = check_hash(entry.get("sha256"), "image.sha256")
            block_id = require_text(entry.get("block_id"), "image.block_id")
            if check_files and file_hash(image_path) != digest:
                raise ValueError(f"image changed: {image_path}")
            result["images"].append({"path": str(Path(image_path).resolve()),
                                      "sha256": digest, "block_id": block_id})
    elif step == "zotero_written":
        result["item_key"] = require_text(receipt.get("item_key"), "item_key")
    return result


def status(state):
    stale = changed_files(state)
    invalid = set(state["required_steps"] if any(s.startswith("source_") for s in stale) else
                  [s for s in state["required_steps"] if s not in ("pdf_prepared", "pdf_uploaded")] if stale else [])
    image_record = state["steps"].get("images_verified", {})
    if image_record.get("status") == "done":
        for image in image_record["evidence"]["images"]:
            try:
                unchanged = file_hash(image["path"]) == image["sha256"]
            except OSError:
                unchanged = False
            if not unchanged:
                invalid.add("images_verified")
                stale.append("image_changed_or_unavailable: " + image["path"])
    done = []
    for step in ALL_STEPS:
        if step not in state["required_steps"]:
            continue
        required_deps = [x for x in dependencies(state, step) if x in state["required_steps"]]
        if (step not in invalid and state["steps"].get(step, {}).get("status") == "done"
                and all(x in done for x in required_deps)):
            done.append(step)
    pending = [step for step in state["required_steps"] if step not in done]
    required_publication = PUBLICATION_STEPS[state.get("backend", "notion")]
    publication_complete = (set(required_publication).issubset(state["required_steps"]) and
                            all(step in done for step in required_publication) and
                            not any(item.startswith(("source_", "note_", "image_")) for item in stale))
    archival = [step for step in ARCHIVAL_STEPS if step in state["required_steps"]]
    archival_pending = [step for step in archival if step not in done]
    return {"citekey": state["citekey"], "source_sha256": state["source_sha256"],
            "note_sha256": state.get("note_sha256"), "required_steps": state["required_steps"],
            "done": done, "pending": pending, "stale": stale,
            "note_complete": publication_complete, "publication_complete": publication_complete,
            "archival_pending": archival_pending, "archival_complete": bool(archival) and not archival_pending,
            "complete": publication_complete,
            "backend": state.get("backend", "notion"),
            "verification": "Recorded observations only; no remote services were contacted."}


def record_step(path, step, evidence=None, failure=None):
    state = load_state(path)
    if step not in state["required_steps"]:
        raise ValueError("step is not configured for this run")
    if changed_files(state):
        raise ValueError("source/note changed or unavailable; run init again before recording")
    if bool(evidence is not None) == bool(failure is not None):
        raise ValueError("supply exactly one of evidence or failure")
    record = {"at": now(), "source_sha256": state["source_sha256"],
              "note_sha256": state.get("note_sha256")}
    if failure is not None:
        record.update(status="failed", error=require_text(failure, "failure"))
    else:
        current = status(state)
        for dependency in dependencies(state, step):
            if dependency in state["required_steps"] and dependency not in current["done"]:
                raise ValueError(f"record {dependency} successfully before {step}")
        record.update(status="done", evidence=validate_receipt(state, step, evidence))
    # Re-running an upstream operation invalidates downstream acknowledgements,
    # even when the Markdown bytes stayed the same (e.g. a replaced Notion page).
    affected = {step}
    archived = state["steps"].get("pdf_uploaded", {}).get("evidence", {})
    if step == "feishu_written" and archived and archived.get("record_id") != record["evidence"]["record_id"]:
        affected.add("pdf_uploaded")
    for candidate in ALL_STEPS:
        if any(dep in affected for dep in dependencies(state, candidate)):
            affected.add(candidate)
    for candidate in affected - {step}:
        state["steps"].pop(candidate, None)
    state["steps"][step] = record
    state["updated_at"] = now()
    atomic_json(path, state)
    return status(state)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    init = sub.add_parser("init", help="Create or refresh the run's file fingerprints")
    init.add_argument("--file", required=True)
    init.add_argument("--citekey", required=True)
    init.add_argument("--source", required=True)
    init.add_argument("--note")
    init.add_argument("--backend", choices=("feishu", "notion"), help="Defaults to selected config backend for new runs")
    init.add_argument("--config")
    init.add_argument("--steps", help="Comma-separated subset of " + ",".join(ALL_STEPS))
    show = sub.add_parser("status", help="Show effective completion and pending steps")
    show.add_argument("--file", required=True)
    record = sub.add_parser("record", help="Record an observed result or a failed step")
    record.add_argument("--file", required=True)
    record.add_argument("--step", choices=ALL_STEPS, required=True)
    receipts = record.add_mutually_exclusive_group(required=True)
    receipts.add_argument("--evidence", help="JSON receipt path (paths inside are relative to this file)")
    receipts.add_argument("--failed", help="Failure reason; the step remains pending")
    args = parser.parse_args(argv)
    try:
        if args.command == "init":
            selected = args.steps.split(",") if args.steps is not None else None
            cfg = config.load(args.config)
            old = load_state(args.file) if Path(args.file).exists() else None
            backend = args.backend or (old.get("backend", "notion") if old else config.publishing_backend(cfg))
            result = init_state(args.file, args.citekey, args.source, args.note, selected, backend, cfg)
        elif args.command == "status":
            result = status(load_state(args.file))
        else:
            evidence = None
            if args.evidence:
                receipt_path = Path(args.evidence).resolve()
                evidence = json.loads(receipt_path.read_text(encoding="utf-8-sig"))
                if isinstance(evidence, dict) and isinstance(evidence.get("images"), list):
                    for image in evidence["images"]:
                        if isinstance(image, dict) and isinstance(image.get("path"), str):
                            image["path"] = str((receipt_path.parent / image["path"]).resolve())
            result = record_step(args.file, args.step, evidence, args.failed)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    except (OSError, ValueError, TypeError) as exc:
        parser.exit(2, f"run state error: {exc}\n")


if __name__ == "__main__":
    main()

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


STEPS = ("pdf_prepared", "note_validated", "notion_written", "images_verified",
         "zotero_written")
DEPENDENCIES = {
    "pdf_prepared": (), "note_validated": ("pdf_prepared",),
    "notion_written": ("note_validated",),
    "images_verified": ("notion_written",), "zotero_written": ("notion_written",),
}
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
            any(step not in STEPS for step in required) or len(set(required)) != len(required)):
        raise ValueError("required_steps must be a non-empty unique list of known steps")
    records = state.get("steps")
    if not isinstance(records, dict) or any(step not in required for step in records):
        raise ValueError("state contains unknown or unconfigured steps")
    for step, record in records.items():
        if not isinstance(record, dict) or record.get("status") not in ("done", "failed"):
            raise ValueError(f"invalid state for {step}")
        if record.get("source_sha256") != state["source_sha256"]:
            raise ValueError(f"{step} has a different source fingerprint")
        if step != "pdf_prepared" and record.get("note_sha256") != state.get("note_sha256"):
            raise ValueError(f"{step} has a different note fingerprint")
        if record["status"] == "done":
            validate_receipt(state, step, record.get("evidence"), check_files=False)
        else:
            require_text(record.get("error"), f"{step}.error")
    return state


def init_state(path, citekey, source, note=None, steps=None):
    require_text(citekey, "citekey")
    source = Path(source).resolve(strict=True)
    source_hash = file_hash(source)
    old = load_state(path) if Path(path).exists() else None
    if old and old["citekey"] != citekey:
        raise ValueError("run file belongs to a different citekey; use another run file")
    selected = list(steps or (old["required_steps"] if old else STEPS))
    if not selected or len(set(selected)) != len(selected) or any(x not in STEPS for x in selected):
        raise ValueError("steps must be a non-empty unique subset of: " + ", ".join(STEPS))
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
        invalid = [s for s in state["steps"] if s != "pdf_prepared"]
        reasons.append("note_changed")
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
                 required_steps=selected, updated_at=now())
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
        return result
    if receipt.get("readback_checked") is not True:
        raise ValueError("remote receipts require readback_checked=true from an actual readback")
    page_id = require_text(receipt.get("page_id"), "page_id")
    result.update(page_id=page_id, readback_checked=True)
    published = state["steps"].get("notion_written", {}).get("evidence", {})
    if step != "notion_written" and published and published.get("page_id") != page_id:
        raise ValueError("receipt page_id differs from the recorded Notion page")
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
                  [s for s in state["required_steps"] if s != "pdf_prepared"] if stale else [])
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
    for step in STEPS:
        if step not in state["required_steps"]:
            continue
        dependencies = [x for x in DEPENDENCIES[step] if x in state["required_steps"]]
        if (step not in invalid and state["steps"].get(step, {}).get("status") == "done"
                and all(x in done for x in dependencies)):
            done.append(step)
    pending = [step for step in state["required_steps"] if step not in done]
    return {"citekey": state["citekey"], "source_sha256": state["source_sha256"],
            "note_sha256": state.get("note_sha256"), "required_steps": state["required_steps"],
            "done": done, "pending": pending, "stale": stale, "complete": not pending and not stale,
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
        for dependency in DEPENDENCIES[step]:
            if dependency in state["required_steps"] and dependency not in current["done"]:
                raise ValueError(f"record {dependency} successfully before {step}")
        record.update(status="done", evidence=validate_receipt(state, step, evidence))
    # Re-running an upstream operation invalidates downstream acknowledgements,
    # even when the Markdown bytes stayed the same (e.g. a replaced Notion page).
    affected = {step}
    for candidate in STEPS:
        if any(dep in affected for dep in DEPENDENCIES[candidate]):
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
    init.add_argument("--steps", help="Comma-separated subset of " + ",".join(STEPS))
    show = sub.add_parser("status", help="Show effective completion and pending steps")
    show.add_argument("--file", required=True)
    record = sub.add_parser("record", help="Record an observed result or a failed step")
    record.add_argument("--file", required=True)
    record.add_argument("--step", choices=STEPS, required=True)
    receipts = record.add_mutually_exclusive_group(required=True)
    receipts.add_argument("--evidence", help="JSON receipt path (paths inside are relative to this file)")
    receipts.add_argument("--failed", help="Failure reason; the step remains pending")
    args = parser.parse_args(argv)
    try:
        if args.command == "init":
            selected = args.steps.split(",") if args.steps is not None else None
            result = init_state(args.file, args.citekey, args.source, args.note, selected)
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

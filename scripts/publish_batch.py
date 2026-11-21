"""Publish multiple notes through isolated, independently locked publishers.

    python scripts/publish_batch.py note1.md note2.md --data PDB --workers 2

Each child retains publish.py's locking and mandatory readback. Results are
bounded summaries, not raw child output. Notes are identified by input position
and the SHA-256 canonical citekey lock alias; successful publications include
a verified Feishu document URL and ID. A partial summary identifies
allowed missing metadata or a pending optional Zotero backlink; these do not
make a completed Feishu publication fail. A failed or interrupted publication
may have changed remote state; inspect it before retrying.
"""
import argparse
import json
import os
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path
from urllib.parse import urlsplit

sys.dont_write_bytecode = True

import config
import feishu_concurrency as concurrency
import publish

PUBLISHER = Path(__file__).resolve().with_name("publish.py")
OUTPUT_LIMIT = 65536
BUSY_MESSAGES = {
    "paper busy: another publication is in progress": "paper_busy",
    "destination busy: active publishers use a different feishu.paper_concurrency": "destination_busy",
    "destination busy: feishu.paper_concurrency limit reached": "destination_busy",
}


class PreflightError(ValueError):
    pass


class BatchInterrupted(Exception):
    def __init__(self, signum):
        self.signum = signum


class BatchStopped(Exception):
    def __init__(self, signum, results):
        self.signum, self.results = signum, results


class BatchFailed(Exception):
    def __init__(self, results):
        self.results = results


def parse_workers(value):
    try:
        number = int(value)
    except ValueError:
        raise argparse.ArgumentTypeError("must be an integer from 1 to 8") from None
    if not 1 <= number <= concurrency.MAX_WORKERS:
        raise argparse.ArgumentTypeError("must be an integer from 1 to 8")
    return number


class SafeArgumentParser(argparse.ArgumentParser):
    def error(self, message):
        if message.startswith("argument --workers:"):
            detail = "--workers must be an integer from 1 to 8"
        elif message.startswith("argument --data:"):
            detail = "--data requires one value"
        elif message.startswith("the following arguments are required:"):
            detail = "at least one NOTE is required"
        else:
            detail = "invalid or unrecognized arguments"
        self.print_usage(sys.stderr)
        self.exit(2, f"{self.prog}: error: {detail}\n")


def arguments(argv):
    parser = SafeArgumentParser(prog="publish_batch.py", description=__doc__)
    parser.add_argument("notes", nargs="+", metavar="NOTE")
    parser.add_argument("--workers", type=parse_workers)
    parser.add_argument("--data")
    parser.add_argument("--allow-partial", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--zotero", action="store_true")
    return parser.parse_args(argv)


def preflight(paths, data):
    notes = []
    seen_keys, seen_dois = {}, {}
    tags = {"data": [part.strip() for part in (data or "").split(",") if part.strip()]}
    for number, raw_path in enumerate(paths, 1):
        try:
            path = os.path.abspath(raw_path)
            if not os.path.isfile(path):
                raise OSError
            props = publish.parse_note(path)
            key_alias = concurrency.alias_keys(props["citekey"], None)[0]
            aliases = set(concurrency.alias_keys(props["citekey"], props["doi"]))
        except (OSError, UnicodeError, ValueError):
            raise PreflightError(f"note {number}: invalid, unreadable, or unparseable note") from None
        doi_aliases = aliases - {key_alias}
        if key_alias in seen_keys:
            raise PreflightError(f"notes {seen_keys[key_alias]} and {number}: duplicate canonical citekey")
        for alias in doi_aliases:
            if alias in seen_dois:
                raise PreflightError(f"notes {seen_dois[alias]} and {number}: duplicate canonical DOI")
        seen_keys[key_alias] = number
        for alias in doi_aliases:
            seen_dois[alias] = number
        notes.append({"number": number, "path": path, "citekey": props["citekey"],
                      "missing": list(publish.missing(props, tags))})
    return notes


def child_command(note, args):
    command = [sys.executable, "-B", str(PUBLISHER), note["path"]]
    if args.data is not None:
        command.append("--data=" + args.data)
    for attr, flag in (("allow_partial", "--allow-partial"),
                       ("dry_run", "--dry-run"), ("zotero", "--zotero")):
        if getattr(args, attr):
            command.append(flag)
    return command


class Capture:
    def __init__(self, pipe, tail=False):
        self.pipe, self.tail = pipe, tail
        self.data = bytearray()
        self.truncated = False
        self.failed = False
        self.thread = threading.Thread(target=self._read, daemon=True)

    def _read(self):
        try:
            while True:
                chunk = self.pipe.read(8192)
                if not chunk:
                    break
                if self.tail:
                    self.data.extend(chunk)
                    if len(self.data) > OUTPUT_LIMIT:
                        del self.data[:-OUTPUT_LIMIT]
                        self.truncated = True
                else:
                    remaining = OUTPUT_LIMIT - len(self.data)
                    self.data.extend(chunk[:remaining])
                    self.truncated |= len(chunk) > remaining
        except (OSError, ValueError):
            self.failed = True
        finally:
            self.pipe.close()

    def start(self):
        self.thread.start()

    def join(self):
        if self.thread.ident is None:
            self.pipe.close()
        else:
            self.thread.join()


def start_child(note, args):
    environment = os.environ.copy()
    environment["PYTHONIOENCODING"] = "utf-8"
    process = subprocess.Popen(child_command(note, args), stdin=subprocess.DEVNULL,
                               stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                               shell=False, env=environment)
    output = errors = None
    try:
        output = Capture(process.stdout)
        errors = Capture(process.stderr, tail=True)
        output.start()
        errors.start()
    except BaseException:
        process.kill()
        process.wait()
        if output is not None:
            output.join()
        else:
            process.stdout.close()
        if errors is not None:
            errors.join()
        else:
            process.stderr.close()
        raise
    return process, output, errors


def identifier(note):
    return concurrency.alias_keys(note["citekey"], None)[0]


def verified_document(document, fcfg):
    url, doc_id = document.get("url"), document.get("doc")
    if (not isinstance(url, str) or len(url) > 512 or "?" in url or "#" in url
            or not isinstance(doc_id, str) or len(doc_id) > 128):
        raise ValueError("invalid publication link")
    fields = {"笔记": {"link": url}}
    if publish.existing_note_doc_id(fields, fcfg) != doc_id:
        raise ValueError("publication link does not match document")
    if url.startswith("/"):
        domain = fcfg.get("domain")
        if not domain and fcfg.get("destination_url"):
            target = config.feishu_destination({"feishu": fcfg})
            domain = urlsplit(target["destination_url"]).hostname
        if not isinstance(domain, str) or not domain:
            raise ValueError("publication domain is not configured")
        url = "https://" + domain + url
        if publish.existing_note_doc_id({"笔记": {"link": url}}, fcfg) != doc_id:
            raise ValueError("untrusted publication domain")
    return url, doc_id


def result_for(note, args, process, output, errors, fcfg):
    result = {"note": note["number"], "citekey_id": identifier(note),
              "exit_code": process.returncode}
    if process.returncode:
        lines = errors.data.decode("utf-8", errors="replace").splitlines()
        busy = BUSY_MESSAGES.get(lines[-1].strip()) if lines else None
        if busy:
            result.update(status="busy", reason=busy)
        elif note["missing"] and not args.allow_partial:
            result.update(status="failed", reason="missing_required_fields", missing=note["missing"])
        elif lines and lines[-1].startswith("回读不通过："):
            result.update(status="failed", reason="readback_failed_verify_before_retry")
        else:
            result.update(status="failed", reason="child_failed_verify_before_retry")
        return result
    if output.truncated or output.failed or errors.failed:
        result.update(status="failed", reason="child_output_unavailable_verify_before_retry")
        return result
    try:
        document = json.loads(output.data.decode("utf-8"))
        if not isinstance(document, dict):
            raise ValueError
        if args.dry_run:
            plan = document.get("plan")
            if not isinstance(plan, dict) or plan.get("citekey") != note["citekey"]:
                raise ValueError
            if (not isinstance(document.get("missing"), dict)
                    or plan.get("mode") not in ("create", "update")):
                raise ValueError
        else:
            if (document.get("citekey") != note["citekey"]
                    or document.get("action") not in ("row_created", "row_updated")):
                raise ValueError
            url, doc_id = verified_document(document, fcfg)
    except (UnicodeError, ValueError):
        result.update(status="failed", reason="invalid_child_result_verify_before_retry")
        return result
    if note["missing"] and not args.allow_partial:
        result.update(status="failed", reason="missing_required_fields", missing=note["missing"])
        return result
    result["status"] = "success"
    if note["missing"]:
        result["metadata_partial"] = True
        result["missing"] = note["missing"]
    if args.dry_run:
        result["plan"] = document["plan"]["mode"]
    else:
        result.update(action=document["action"], url=url, doc=doc_id)
        if args.zotero:
            zotero = document.get("zotero")
            result["backlink"] = ("written" if isinstance(zotero, str)
                                  and zotero.startswith("written: ") else "pending")
    return result


def stop_children(active):
    for process, _, _ in active.values():
        if process.poll() is None:
            try:
                process.terminate()
            except OSError:
                pass
    for process, output, errors in active.values():
        try:
            process.wait(timeout=2)
        except subprocess.TimeoutExpired:
            try:
                process.kill()
            except OSError:
                pass
            process.wait()
        output.join()
        errors.join()


def run_batch(notes, args, workers, pending_signal=None, fcfg=None):
    results = [None] * len(notes)
    active = {}
    next_note = 0
    interrupted = None
    failed = False
    try:
        while next_note < len(notes) or active:
            if pending_signal and pending_signal():
                raise BatchInterrupted(pending_signal())
            while next_note < len(notes) and len(active) < workers:
                if pending_signal and pending_signal():
                    raise BatchInterrupted(pending_signal())
                note = notes[next_note]
                next_note += 1
                try:
                    active[note["number"]] = start_child(note, args)
                except OSError:
                    results[note["number"] - 1] = {
                        "note": note["number"], "citekey_id": identifier(note),
                        "status": "failed", "reason": "could_not_start_child"}
            for number, (process, output, errors) in list(active.items()):
                if process.poll() is None:
                    continue
                process.wait()
                output.join()
                errors.join()
                results[number - 1] = result_for(notes[number - 1], args, process, output, errors, fcfg)
                del active[number]
            if active:
                time.sleep(0.02)
        if pending_signal and pending_signal():
            raise BatchInterrupted(pending_signal())
    except (KeyboardInterrupt, BatchInterrupted) as exc:
        interrupted = exc.signum if isinstance(exc, BatchInterrupted) else signal.SIGINT
    except Exception:
        failed = True
    finally:
        old_int = signal.signal(signal.SIGINT, signal.SIG_IGN)
        old_term = signal.signal(signal.SIGTERM, signal.SIG_IGN)
        try:
            stop_children(active)
        finally:
            signal.signal(signal.SIGINT, old_int)
            signal.signal(signal.SIGTERM, old_term)
    if interrupted is not None or failed:
        for index, note in enumerate(notes):
            if results[index] is None:
                status = ("interrupted" if interrupted is not None else "failed")
                if index >= next_note:
                    status = "not_started"
                results[index] = {"note": note["number"], "citekey_id": identifier(note),
                                  "status": status}
                if status == "failed":
                    results[index]["reason"] = "batch_error_verify_before_retry"
        if interrupted is not None:
            raise BatchStopped(interrupted, results) from None
        raise BatchFailed(results) from None
    return results


def main(argv=None):
    args = arguments(argv)
    try:
        fcfg = config.feishu()
        maximum = concurrency.worker_count(fcfg, "paper_concurrency")
    except Exception:
        print(json.dumps({"status": "failed", "reason": "invalid_feishu_configuration"}))
        return 2
    workers = args.workers if args.workers is not None else maximum
    if workers > maximum:
        print(json.dumps({"status": "failed", "reason": "workers_exceed_paper_concurrency",
                          "maximum": maximum}))
        return 2
    try:
        notes = preflight(args.notes, args.data)
    except PreflightError as exc:
        print(json.dumps({"status": "failed", "reason": str(exc)}))
        return 2

    pending = [None]

    def interrupted(signum, _frame):
        if pending[0] is None:
            pending[0] = signum

    previous_int = signal.signal(signal.SIGINT, interrupted)
    previous_term = signal.signal(signal.SIGTERM, interrupted)
    try:
        results = run_batch(notes, args, workers, lambda: pending[0], fcfg)
    except BatchStopped as exc:
        print(json.dumps({"status": "interrupted", "reason": "verify_started_notes_before_retry",
                          "workers": workers, "dry_run": args.dry_run, "results": exc.results}))
        return 128 + exc.signum
    except BatchFailed as exc:
        print(json.dumps({"status": "incomplete", "reason": "batch_error_verify_started_notes_before_retry",
                          "workers": workers, "dry_run": args.dry_run, "results": exc.results}))
        return 1
    except Exception:
        print(json.dumps({"status": "failed", "reason": "batch_error_verify_started_notes_before_retry"}))
        return 1
    finally:
        signal.signal(signal.SIGINT, previous_int)
        signal.signal(signal.SIGTERM, previous_term)
    success = all(result["status"] == "success" for result in results)
    partial = any(result.get("metadata_partial") or result.get("backlink") == "pending"
                  for result in results)
    status = ("partial" if partial else "success") if success else "incomplete"
    print(json.dumps({"status": status, "workers": workers,
                      "dry_run": args.dry_run, "results": results}, ensure_ascii=True))
    return 0 if success else 1


if __name__ == "__main__":
    sys.exit(main())

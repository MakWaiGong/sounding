"""Publish one sounding note into the Feishu library, idempotent by citekey.

    python scripts/publish.py notes/<citekey>.md
    python scripts/publish.py notes/<citekey>.md --data PDB,AlphaFold-DB
    python scripts/publish.py notes/<citekey>.md --dry-run

feishu.paper_concurrency and feishu.image_concurrency accept integers 1–8
(default 3). All local clones must use the same user-scoped state directory;
SOUNDING_FEISHU_STATE_DIR overrides ~/.sounding/feishu. This coordination does
not protect other computers, older publishers, or manual remote changes.

What it does, in order:

1. reads the note's front matter (标题/发表/类型/citekey), the 速览第 10 行的去留
   (kept for the callout tint, not written to the table) and the paper's DOI from
   定位素材
2. finds the papers row by exact citekey -- 1 row updates, 0 rows creates,
   more than 1 stops (``reference-library.md`` §4)
3. writes the doc body and figures, replacing whatever was there
4. reads the document back and refuses to call a write finished without it
5. optionally writes ``Feishu: <url>`` and the ``d:`` data-family tags back to Zotero
6. with ``--mark-migrated``, records in 定位素材 that the note moved here from the
   other backend (``reference-library.md`` §6.4); the marker travels into the doc

Required properties missing from the note are a stop, not a silent gap: the
report names the concept and the field (``reference-library.md`` §3).

Only the 数据类型 family is written. 领域 (the old problem family) is filled
by hand in the Feishu table, and there is no method family. 去留 and 置信度 are
not table columns either: 去留 stays a note-side conclusion.
"""
import argparse
import hashlib
import json
import os
import re
import sys
import tempfile
import time
from urllib.parse import urlsplit

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import config  # noqa: E402
import feishu_api as fapi  # noqa: E402
import feishu_concurrency as concurrency  # noqa: E402
import feishu_doc as fdoc  # noqa: E402
import feishu_schema as fschema  # noqa: E402

WORKSPACE = "notes/.feishu-workspace.json"
INDEX = "notes/.feishu-index.json"

HEADER = re.compile(r"^(标题|发表时间|发表|期刊|类型|citekey|年份|本地 PDF|Zotero)\s*[：:]\s*(.+?)\s*$")
YEAR_AT_END = re.compile(r"[(]([0-9]{4})(?:-(0[1-9]|1[0-2]))?[)][ ]*$")
VERDICT = re.compile(r"去留[：:]\s*([^\s。]+)")
DOI = re.compile(r"10\.\d{4,9}/[^\s·，,;；]+")
MIGRATION = re.compile(r"^迁移[：:]\s*.+$", re.M)
SOURCE_HEADING = re.compile(r"^##\s*定位素材\s*$", re.M)
TYPES = ("algorithm", "science", "review", "other")


def root_of(note_path):
    return os.path.dirname(os.path.dirname(os.path.abspath(note_path)))


def load_json(path, default):
    if not os.path.exists(path):
        return default
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def save_json(path, data):
    directory = os.path.dirname(path)
    os.makedirs(directory, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=".feishu-index-", suffix=".tmp", dir=directory)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2, sort_keys=True)
            f.flush()
            os.fsync(f.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def update_index(path, key, entry):
    """Merge only this paper while holding the workspace index lock."""
    with concurrency.locked_index(path):
        if os.path.exists(path):
            with open(path, encoding="utf-8") as f:
                current = json.load(f)
        else:
            current = {}
        if not isinstance(current, dict):
            raise ValueError("invalid Feishu index")
        current[key] = entry
        save_json(path, current)


def normalize_publication_time(value):
    if value is None or value == "":
        return None
    value = str(value).strip()
    if not re.fullmatch(r"[1-9][0-9]{3}(?:-(?:0[1-9]|1[0-2]))?", value):
        raise ValueError("发表时间 must be YYYY-MM, or YYYY when the month is unknown")
    return value


def parse_note(path):
    """Front matter plus the verdict line; nothing is invented."""
    with open(path, encoding="utf-8") as f:
        text = f.read()
    lines = text.splitlines()
    head = {}
    for line in lines[:20]:
        m = HEADER.match(line)
        if m:
            head[m.group(1)] = m.group(2).strip("` ")
    venue_raw = head.get("期刊") or head.get("发表", "")
    m = YEAR_AT_END.search(venue_raw)
    year = int(m.group(1)) if m else None
    if year is None and head.get("年份", "").isdigit():
        year = int(head["年份"])
    inferred_time = (f"{year:04d}-{m.group(2)}" if m and m.group(2) else str(year)) if year is not None else None
    publication_time = normalize_publication_time(head.get("发表时间") or inferred_time)
    if publication_time:
        if year is not None and year != int(publication_time[:4]):
            raise ValueError("发表时间 conflicts with the publication year")
        year = int(publication_time[:4])
    # Only the trailing (year) is the year; a venue may carry its own, e.g.
    # "Cell (2003) (2026)" means the Cell paper, published in 2026.
    venue = (venue_raw[:m.start()] if m else venue_raw).strip() or None
    verdict = None
    for line in lines[:60]:
        m = VERDICT.search(line)
        if m:
            verdict = m.group(1)
            break
    # The four-line header is what the table row holds; repeating it at the top
    # of the document is noise, so the body starts after the first rule.
    parts = re.split(r"^\s*---\s*$", text, maxsplit=1, flags=re.M)
    body = parts[1].lstrip("\r\n") if len(parts) > 1 else text
    # 定位素材 carries the paper's real title; the header carries the short
    # Chinese name. Zotero stores the real one, so keep it for item lookup.
    source = re.search(r"^原文[：:]\s*(.+)$", text, re.M)
    alt_titles = ()
    if source:
        alt_titles = (source.group(1).split(" · ")[0].strip(),)
    # The DOI identifies the paper in the row; take it from 定位素材 first, then
    # from anywhere in the note (the template's 原文 line is the normal home).
    doi_match = DOI.search(source.group(1)) if source else None
    if doi_match is None:
        doi_match = DOI.search(text)
    doi = doi_match.group(0).rstrip(".") if doi_match else None
    return {
        "citekey": head.get("citekey") or os.path.splitext(os.path.basename(path))[0],
        "title": head.get("标题"),
        "alt_titles": alt_titles,
        "venue": venue,
        "year": year,
        "publication_time": publication_time,
        "type": head.get("类型"),
        "doi": doi,
        "verdict": verdict,
        "md": text,
        "body": body,
        "rev": hashlib.sha1(text.encode("utf-8")).hexdigest()[:12],
    }


def mark_migrated(text, source, target, today=None):
    """Put one migration line inside 定位素材; a rerun rewrites it, never stacks.

    The line lives in the note (not only in the index) because the note is the
    only thing both backends share, and it travels into the document body.
    """
    today = today or time.strftime("%Y-%m-%d")
    line = f"迁移：{today} 从 {source} 迁入；主后端 = {target}"
    if MIGRATION.search(text):
        return MIGRATION.sub(line, text, count=1)
    heading = SOURCE_HEADING.search(text)
    if heading is None:
        return text.rstrip("\n") + f"\n\n## 定位素材\n\n{line}\n"
    return text[:heading.end()] + f"\n{line}\n" + text[heading.end():]


def old_backend_from_zotero(props):
    """Which backend the existing link points at, judged by the Extra lines."""
    import zotero_write as zw

    cfg = config.load()
    base, user_id, api_key = config.zotero_api(cfg)
    if not user_id or not api_key:
        return None
    key, _ = zw.item_key_from_citekey(props["citekey"], cfg, props.get("title"),
                                      props.get("alt_titles") or ())
    if not key:
        return None
    status, _, item = zw.request("GET", f"{base}/users/{user_id}/items/{key}", api_key)
    if status != 200 or not isinstance(item, dict):
        return None
    extra = (item.get("data") or {}).get("extra") or ""
    if "Notion:" in extra:
        return "Notion"
    if "Feishu:" in extra:
        return "飞书"
    return None


def missing(props, tags):
    """Concept -> field, in the order the mapping table lists them."""
    want = {
        "title": props.get("title"), "venue": props.get("venue"),
        "year": props.get("year"), "type": props.get("type"),
        "citekey": props.get("citekey"),
        "data": tags.get("data"),
    }
    return {k: v for k, v in want.items() if not v}


def report_missing(gaps):
    names = {"title": "标题", "venue": "期刊", "year": "发表时间", "type": "类型",
             "citekey": "citekey", "data": "数据类型"}
    lines = ["缺字段："]
    for concept in gaps:
        lines.append(f"  {names[concept]}（概念 {concept}）")
    lines.append("建议：补齐笔记或先用 --data 传值后重跑")
    return "\n".join(lines)


def preserve_publication_time(fields, existing):
    fields = dict(fields)
    incoming = fields.get("发表时间")
    old = existing.get("发表时间")
    if isinstance(old, list):
        old = "".join(x.get("text", "") if isinstance(x, dict) else str(x) for x in old)
    if isinstance(incoming, str) and re.fullmatch(r"[1-9][0-9]{3}", incoming) and isinstance(old, str) and len(old) > 4:
        if not old.startswith(incoming):
            raise ValueError("Existing publication time conflicts with the incoming year")
        fields.pop("发表时间", None)
    return fields


def record_fields(props, tags, url):
    fields = {
        "标题": props["title"],
        "citekey": props["citekey"],
        "期刊": props["venue"],
        "发表时间": props.get("publication_time") or (str(props["year"]) if props.get("year") is not None else None),
        "类型": props["type"],
    }
    if props.get("doi"):
        fields["DOI"] = props["doi"]
    for concept, field in (("data", "数据类型"),):
        values = tags.get(concept)
        if values:
            fields[field] = values
    if url:
        fields["笔记"] = {"link": url, "text": "笔记"}
    for field, value in list(fields.items()):
        if value is None or value == []:
            fields.pop(field)
    return fields


def find_papers(fc, ws, citekey):
    """Exact citekey search: (field, operator, value list) triples."""
    return fc.search_records(ws["app_token"], ws["tables"]["papers"],
                             [("citekey", "is", [citekey])])


def existing_note_doc_id(fields, fcfg):
    """Resolve an existing row link without accepting another tenant or path."""
    note = fields.get("笔记") if isinstance(fields, dict) else None
    link = note.get("link") if isinstance(note, dict) else None
    error = "Existing row has no unambiguous trusted Feishu docx note link"
    if (not isinstance(link, str) or not link
            or any(c.isspace() or ord(c) < 32 or ord(c) == 127 or c == "\\" for c in link)):
        raise ValueError(error)
    try:
        parsed = urlsplit(link)
    except ValueError:
        raise ValueError(error) from None
    match = re.fullmatch(r"/docx/([A-Za-z0-9_-]+)", parsed.path)
    if not match:
        raise ValueError(error)
    if not parsed.scheme and not parsed.netloc and not link.startswith("/docx/"):
        raise ValueError(error)
    if parsed.scheme or parsed.netloc:
        domain = fcfg.get("domain")
        if not domain and fcfg.get("destination_url"):
            try:
                destination = config.feishu_destination({"feishu": fcfg})
                domain = urlsplit(destination["destination_url"]).hostname
            except ValueError:
                raise ValueError(error) from None
        if (not isinstance(domain, str) or len(domain) > 253
                or not re.fullmatch(r"(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+feishu\.cn",
                                    domain, re.IGNORECASE | re.ASCII)
                or parsed.scheme != "https" or parsed.netloc.lower() != domain.lower()):
            raise ValueError(error)
    return match.group(1)


def read_record(fc, ws, record_id):
    if not isinstance(record_id, str) or not re.fullmatch(r"[A-Za-z0-9_-]+", record_id):
        raise ValueError("Invalid record identifier in publication state")
    path = f"/open-apis/bitable/v1/apps/{ws['app_token']}/tables/{ws['tables']['papers']}/records/{record_id}"
    result = fc.get(path)
    row = result.get("record") if isinstance(result, dict) else None
    if not isinstance(row, dict) or row.get("record_id") != record_id:
        raise RuntimeError("Could not verify the recorded Feishu row; repair before retrying")
    return row


def write_zotero(citekey, url, tags, timings, title=None, alt_titles=()):
    """Extra gains ``Citation Key`` + ``Feishu: <url>``; data family keeps ``d:``."""
    import paper_identity as identity
    import zotero_write as zw

    cfg = config.load()
    base, user_id, api_key = config.zotero_api(cfg)
    if not user_id or not api_key:
        return "skipped: no zotero credentials"
    key, err = zw.item_key_from_citekey(citekey, cfg, title, alt_titles)
    if not key:
        return f"skipped: {err}"
    item_url = f"{base}/users/{user_id}/items/{key}"
    status, _, item = zw.request("GET", item_url, api_key)
    if status != 200 or not isinstance(item, dict) or "data" not in item:
        return f"failed: HTTP {status}"

    wanted = []
    for family, concept in (("d", "data"),):
        wanted.extend(f"{family}:{v}" for v in (tags.get(concept) or []))
    prefixes = config.tag_prefixes(cfg)

    def make_patch(current):
        data = current["data"]
        patch = {}
        if wanted:
            patch["tags"] = identity.merge_tags(data.get("tags"), wanted, prefixes)
        if url:
            patch["extra"] = identity.merge_extra(
                data.get("extra"), citekey=citekey, feishu=url)
        return patch

    started = time.time()
    zw.patch_with_retry(zw.request, item_url, api_key, make_patch, item)
    timings["zotero_write"] = round((time.time() - started) * 1000)
    return f"written: {key}"


def optional_zotero(props, url, tags, timings):
    try:
        return write_zotero(props["citekey"], url, tags, timings, props["title"],
                            props.get("alt_titles") or ())
    except Exception as exc:
        return f"failed ({type(exc).__name__}); note published, backlink pending"


def main():
    ap = argparse.ArgumentParser(description="Publish one note into the Feishu library.")
    ap.add_argument("note", help="path to notes/<citekey>.md")
    ap.add_argument("--data", help="数据类型, comma separated")
    ap.add_argument("--read", choices=["扫", "精读"], default=None,
                    help="deprecated: ignored; reading status is managed by the user")
    ap.add_argument("--allow-partial", action="store_true",
                    help="write even when a required property is missing")
    ap.add_argument("--zotero", action="store_true", help="write the link and tags back")
    ap.add_argument("--mark-migrated", nargs="?", const="auto", metavar="旧后端",
                    help="在定位素材写一行迁移标记（notion / 飞书）；省略值则从 Zotero 推断")
    ap.add_argument("--dry-run", action="store_true", help="print the plan, write nothing")
    args = ap.parse_args()

    note_path = args.note
    if not os.path.exists(note_path):
        sys.exit(f"no such note: {note_path}")
    root = root_of(note_path)
    props = parse_note(note_path)
    if args.mark_migrated is not None:
        source = args.mark_migrated
        if source == "auto":
            source = old_backend_from_zotero(props)
        if not source:
            sys.exit("--mark-migrated 需要旧后端名（如 --mark-migrated notion）；自动推断失败")
        with open(note_path, encoding="utf-8", newline="") as f:
            text = f.read()
        migrated = mark_migrated(text, source, "飞书")
        if not args.dry_run:
            with open(note_path, "w", encoding="utf-8", newline="") as f:
                f.write(migrated)
            props = parse_note(note_path)
        marker_label = "迁移标记（预览）" if args.dry_run else "迁移标记"
        print(f"{marker_label}：从 {source} 迁入，主后端 = 飞书", file=sys.stderr)
    split = lambda s: [p.strip() for p in (s or "").split(",") if p.strip()]
    tags = {"data": split(args.data), "read": args.read}

    gaps = missing(props, tags)
    if gaps and not args.allow_partial:
        sys.exit(report_missing(gaps))

    fcfg = config.feishu()
    workspace_path = os.path.join(root, WORKSPACE)
    ws = load_json(workspace_path, {})
    for key, field in (("app_token", "base_app_token"), ("folder_token", "folder_token")):
        if not ws.get(key) and fcfg.get(field):
            ws[key] = fcfg[field]
    if not ws.get("app_token") or not (ws.get("tables") or {}).get("papers"):
        sys.exit("飞书库未初始化：先跑 python scripts/feishu_setup.py --apply")

    index_path = os.path.join(root, INDEX)
    if args.dry_run:
        index = load_json(index_path, {})
        entry = index.get(props["citekey"], {})
    else:
        entry = {}

    plan = {
        "citekey": props["citekey"], "title": props["title"],
        "venue": props["venue"], "year": props["year"], "type": props["type"],
        "doi": props["doi"], "rev": props["rev"],
        "known_doc": entry.get("doc_id"), "mode": "update" if entry else "create",
    }
    if not props["doi"]:
        print("提示：笔记里没有 DOI，表格这一列会空着；在「## 定位素材」加一行 "
              "`原文：<标题> · <发表> <年> · DOI 10.xxxx/xxxx` 即可。", file=sys.stderr)
    paper_workers = concurrency.worker_count(fcfg, "paper_concurrency")
    image_workers = concurrency.worker_count(fcfg, "image_concurrency")
    if args.dry_run:
        print(json.dumps({"plan": plan, "fields": record_fields(props, tags, None),
                          "missing": gaps}, ensure_ascii=False, indent=2))
        return

    editor_ids = fapi.validate_editor_open_ids(fcfg.get("editor_open_ids"))
    with concurrency.Publication(ws, fcfg, props["citekey"], props["doi"], paper_workers) as publication:
        with concurrency.locked_index(index_path):
            index = load_json(index_path, {})
            if not isinstance(index, dict):
                raise ValueError("invalid Feishu index")
            entry = index.get(props["citekey"], {})
        publish_locked(args, note_path, ws, fcfg, props, tags, index_path, entry,
                       publication, editor_ids, image_workers)


def definitive_rate_limit(exc):
    return isinstance(exc, fapi.FeishuRateLimitError)


def publish_locked(args, note_path, ws, fcfg, props, tags, index_path, entry,
                   publication, editor_ids, image_workers):
    state = publication.get_state()
    if state.get("phase") == "document_creating":
        raise RuntimeError(publication.repair_message("document"))
    if not isinstance(entry, dict):
        raise ValueError("Invalid local index entry")
    if state.get("phase") not in (None, "document_creating", "pending", "record_creating", "record_known", "complete"):
        raise RuntimeError("Invalid shared recovery state; repair before publishing")
    shared_doc = state.get("doc_id")
    previous_complete_doc = shared_doc if state.get("phase") == "complete" else None
    if shared_doc is not None and (not isinstance(shared_doc, str) or not re.fullmatch(r"[A-Za-z0-9_-]+", shared_doc)):
        raise RuntimeError("Invalid shared document identifier; repair before publishing")
    if state.get("phase") in ("pending", "record_creating", "record_known", "complete") and not shared_doc:
        raise RuntimeError("Missing shared document identifier; repair before publishing")
    if "make_public" in state and not isinstance(state["make_public"], bool):
        raise RuntimeError("Invalid shared publication permissions; repair before publishing")
    if "body_journal" in state and state["body_journal"] is not True:
        raise RuntimeError("Invalid shared body journal marker; repair before publishing")
    if state.get("body_journal") and not os.path.isfile(publication.resume_path):
        raise RuntimeError("The shared body checkpoint is missing; inspect the document and restore the "
                           "checkpoint before retrying. Do not recreate the document blindly.")
    for source, values in (("shared recovery state", state), ("local index", entry)):
        if "record_id" in values and (not isinstance(values["record_id"], str)
                                      or not re.fullmatch(r"[A-Za-z0-9_-]+", values["record_id"])):
            raise ValueError(f"Invalid record identifier in {source}")
    if state.get("phase") in ("record_known", "complete") and not state.get("record_id"):
        raise RuntimeError("Missing shared record identifier; repair before publishing")
    local_doc = entry.get("doc_id")
    if local_doc is not None and (not isinstance(local_doc, str)
                                  or (local_doc and not re.fullmatch(r"[A-Za-z0-9_-]+", local_doc))):
        raise ValueError("Invalid document identifier in local index")
    if shared_doc and local_doc and shared_doc != local_doc:
        raise ValueError("Shared recovery state conflicts with the local index")
    orphans = fdoc.orphan_figure_files(note_path)
    if orphans:
        names = "、".join(os.path.basename(p) for p in orphans)
        sys.exit(
            f"缺 notes/figs/{props['citekey']}/manifest.json，目录里却有裁图：{names}。"
            "这些图不会上传，先跑 `python scripts/fig_upload.py crop <pdf> "
            f"--citekey {props['citekey']} --fig …` 生成 manifest 再发布"
        )
    figures = list(fdoc.note_figures(note_path))
    fdoc.validate_figure_inputs(shared_doc or local_doc or "preflight", props["md"], figures)
    fc = fapi.client_from_config(fcfg)
    timings = {}
    started = time.time()
    try:
        rows = find_papers(fc, ws, props["citekey"])
        shared_record = state.get("record_id") or entry.get("record_id")
        if not rows and shared_record:
            rows = [read_record(fc, ws, shared_record)]
        if state.get("phase") == "record_creating" and not rows:
            raise RuntimeError(publication.repair_message("record"))
        if rows and shared_record and rows[0].get("record_id") != shared_record:
            raise ValueError("Shared recovery state conflicts with the existing row")
        timings["citekey_query"] = round((time.time() - started) * 1000)
        if len(rows) > 1:
            sys.exit(f"库里有 {len(rows)} 行同 citekey；先合并再发布")

        fields = record_fields(props, tags, None)
        doc_id = shared_doc or entry.get("doc_id")
        make_public = not rows and (not doc_id or state.get("make_public", entry.get("state") == "pending"))
        if rows:
            existing_fields = rows[0].get("fields") or {}
            if not isinstance(existing_fields, dict):
                raise ValueError("Existing row has malformed fields")
            fields = preserve_publication_time(fields, existing_fields)
            row_doc_id = existing_note_doc_id(existing_fields, fcfg)
            if doc_id is not None and (not isinstance(doc_id, str)
                                       or (doc_id and doc_id != row_doc_id)):
                raise ValueError("Existing row note conflicts with the local index")
            if not rows[0].get("record_id"):
                raise ValueError("Existing row is missing its record identifier")
            doc_id = row_doc_id
            if existing_fields.get("citekey") and existing_fields["citekey"] != props["citekey"]:
                fields.pop("citekey", None)
            if state.get("phase") != "complete":
                state.update(phase="record_known", doc_id=doc_id, record_id=rows[0]["record_id"],
                             make_public=False)
                publication.set_state(state)

        if not doc_id:
            started = time.time()
            prior_state = dict(state)
            state = {"phase": "document_creating", "make_public": True}
            publication.set_state(state)
            try:
                doc_id = fc.create_doc(props["title"] or props["citekey"], ws.get("folder_token", ""))
            except Exception as exc:
                if definitive_rate_limit(exc):
                    publication.set_state(prior_state)
                    raise
                raise RuntimeError(publication.repair_message("document")) from None
            if not isinstance(doc_id, str) or not re.fullmatch(r"[A-Za-z0-9_-]+", doc_id):
                raise RuntimeError(publication.repair_message("document"))
            state = {"phase": "pending", "doc_id": doc_id, "make_public": True}
            publication.set_state(state)
            timings["doc_create"] = round((time.time() - started) * 1000)
            try:
                update_index(index_path, props["citekey"], {"doc_id": doc_id, "state": "pending"})
            except Exception:
                raise RuntimeError("could not record the new document for recovery; "
                                   "manual recovery may be required") from None
        elif not shared_doc:
            state = {"phase": "pending", "doc_id": doc_id, "make_public": make_public}
            if rows:
                state.update(phase="record_known", record_id=rows[0]["record_id"])
            publication.set_state(state)

        fc.ensure_doc_full_access(doc_id, editor_ids)

        started = time.time()
        # Pass the full note so replace_body can render the 速览 callout plus the
        # body; the table count check below still uses props["body"].
        resume_path = publication.resume_path
        os.makedirs(os.path.dirname(resume_path), mode=0o700, exist_ok=True)
        # Consume completion authority before the document writer can mutate it.
        state.update(phase="record_known" if state.get("record_id") else "pending",
                     doc_id=doc_id, body_journal=True)
        publication.set_state(state)
        fdoc.replace_body(fc, doc_id, props["md"], figures,
                          resume_path=resume_path,
                          client_factory=lambda: fapi.client_from_config(fcfg),
                          max_workers=image_workers,
                          previous_publication_complete=previous_complete_doc == doc_id)
        timings["body_write"] = round((time.time() - started) * 1000)

        existing_link = existing_fields["笔记"]["link"] if rows else None
        url = (existing_link if existing_link and not existing_link.startswith("/")
               else fc.doc_url(doc_id))
        if make_public:
            fc.grant_public_access(doc_id)

        if not rows:
            fields["笔记"] = {"link": url, "text": "笔记"}
        record_id = rows[0].get("record_id") if rows else None
        started = time.time()
        if record_id:
            fc.update_record(ws["app_token"], ws["tables"]["papers"], record_id, fields)
            action = "row_updated"
        else:
            prior_state = dict(state)
            state.update(phase="record_creating", doc_id=doc_id)
            publication.set_state(state)
            try:
                rec = fc.create_record(ws["app_token"], ws["tables"]["papers"], fields)
            except Exception as exc:
                if definitive_rate_limit(exc):
                    publication.set_state(prior_state)
                    raise
                raise RuntimeError(publication.repair_message("record")) from None
            record_id = rec.get("record_id") if isinstance(rec, dict) else None
            if not isinstance(record_id, str) or not record_id:
                raise RuntimeError(publication.repair_message("record"))
            state.update(phase="record_known", record_id=record_id)
            publication.set_state(state)
            created_row = read_record(fc, ws, record_id)
            if existing_note_doc_id(created_row.get("fields"), fcfg) != doc_id:
                raise ValueError("Created row does not link to the expected document")
            action = "row_created"
        timings["record_write"] = round((time.time() - started) * 1000)

        started = time.time()
        text = fc.doc_text(doc_id)
        blocks = fc.doc_children(doc_id)
        images = sum(1 for b in blocks if b.get("block_type") == 27)
        tables = sum(1 for b in blocks if b.get("block_type") == 31)
        timings["readback"] = round((time.time() - started) * 1000)
        expectations = {"blocks": len(blocks) > 10,
                        "text": len(text) > 500,
                        "images": images == len(figures),
                        "tables": tables >= props["body"].count("\n| ---")}
        if not all(expectations.values()):
            sys.exit(f"回读不通过：{expectations} blocks={len(blocks)} text={len(text)} "
                     f"images={images}/{len(figures)} tables={tables}")

        state.update(phase="complete", doc_id=doc_id, record_id=record_id, make_public=False)
        publication.set_state(state)
        entry = {
            "doc_id": doc_id, "url": url, "record_id": record_id,
            "rev": props["rev"], "updated_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        }
        try:
            update_index(index_path, props["citekey"], entry)
        except Exception:
            raise RuntimeError("could not record completed publication; verify before retrying") from None
        zotero = optional_zotero(props, url, tags, timings) if args.zotero else "not requested"
        print(json.dumps({
            "citekey": props["citekey"], "action": action, "doc": doc_id, "url": url,
            "record": record_id, "blocks": len(blocks), "images": images,
            "tables": tables, "chars": len(text), "zotero": zotero,
            "timings_ms": timings,
        }, ensure_ascii=False, indent=2))
    finally:
        fc.close()


if __name__ == "__main__":
    try:
        main()
    except concurrency.BusyError as e:
        sys.exit(str(e))
    except fapi.FeishuError as e:
        code = e.code if isinstance(e.code, int) else -1
        if definitive_rate_limit(e):
            sys.exit(f"feishu rate limit {code}: request rejected; retry later")
        sys.exit(f"feishu error {code}: request failed")

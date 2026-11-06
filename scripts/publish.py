"""Publish one sounding note into the Feishu library, idempotent by citekey.

    python scripts/publish.py notes/<citekey>.md
    python scripts/publish.py notes/<citekey>.md --data PDB,AlphaFold-DB
    python scripts/publish.py notes/<citekey>.md --dry-run

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
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import config  # noqa: E402
import feishu_api as fapi  # noqa: E402
import feishu_doc as fdoc  # noqa: E402
import feishu_schema as fschema  # noqa: E402

WORKSPACE = "notes/.feishu-workspace.json"
INDEX = "notes/.feishu-index.json"

HEADER = re.compile(r"^(标题|发表|类型|citekey|年份|本地 PDF|Zotero)\s*[：:]\s*(.+?)\s*$")
YEAR_AT_END = re.compile(r"\((\d{4})\)\s*$")
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
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2, sort_keys=True)


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
    venue_raw = head.get("发表", "")
    m = YEAR_AT_END.search(venue_raw)
    year = int(m.group(1)) if m else None
    if year is None and head.get("年份", "").isdigit():
        year = int(head["年份"])
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
    names = {"title": "标题", "venue": "发表", "year": "年份", "type": "类型",
             "citekey": "citekey", "data": "数据类型"}
    lines = ["缺字段："]
    for concept in gaps:
        lines.append(f"  {names[concept]}（概念 {concept}）")
    lines.append("建议：补齐笔记或先用 --data 传值后重跑")
    return "\n".join(lines)


def record_fields(props, tags, url):
    fields = {
        "标题": props["title"],
        "citekey": props["citekey"],
        "发表": props["venue"],
        "年份": props["year"],
        "类型": props["type"],
    }
    if props.get("doi"):
        fields["DOI"] = props["doi"]
    for concept, field in (("data", "数据类型"),):
        values = tags.get(concept)
        if values:
            fields[field] = values
    if tags.get("read"):
        fields["读法"] = tags["read"]
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


def main():
    ap = argparse.ArgumentParser(description="Publish one note into the Feishu library.")
    ap.add_argument("note", help="path to notes/<citekey>.md")
    ap.add_argument("--data", help="数据类型, comma separated")
    ap.add_argument("--read", choices=["扫", "精读"], default="精读")
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
        with open(note_path, "w", encoding="utf-8", newline="") as f:
            f.write(mark_migrated(text, source, "飞书"))
        props = parse_note(note_path)
        print(f"迁移标记：从 {source} 迁入，主后端 = 飞书", file=sys.stderr)
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
    index = load_json(index_path, {})
    entry = index.get(props["citekey"], {})
    timings = {}

    plan = {
        "citekey": props["citekey"], "title": props["title"],
        "venue": props["venue"], "year": props["year"], "type": props["type"],
        "doi": props["doi"], "rev": props["rev"],
        "known_doc": entry.get("doc_id"), "mode": "update" if entry else "create",
    }
    if not props["doi"]:
        print("提示：笔记里没有 DOI，表格这一列会空着；在「## 定位素材」加一行 "
              "`原文：<标题> · <发表> <年> · DOI 10.xxxx/xxxx` 即可。", file=sys.stderr)
    if args.dry_run:
        print(json.dumps({"plan": plan, "fields": record_fields(props, tags, None),
                          "missing": gaps}, ensure_ascii=False, indent=2))
        return

    fc = fapi.client_from_config(fcfg)
    started = time.time()
    try:
        rows = find_papers(fc, ws, props["citekey"])
        timings["citekey_query"] = round((time.time() - started) * 1000)
        if len(rows) > 1:
            ids = [r.get("record_id") for r in rows]
            sys.exit(f"库里有多行同 citekey {props['citekey']}：{ids}；先合并再发布")

        doc_id = entry.get("doc_id")
        if not doc_id:
            started = time.time()
            doc_id = fc.create_doc(props["title"] or props["citekey"], ws["folder_token"])
            timings["doc_create"] = round((time.time() - started) * 1000)

        started = time.time()
        orphans = fdoc.orphan_figure_files(note_path)
        if orphans:
            names = "、".join(os.path.basename(p) for p in orphans)
            sys.exit(
                f"缺 notes/figs/{props['citekey']}/manifest.json，目录里却有裁图：{names}。"
                "这些图不会上传，先跑 `python scripts/fig_upload.py crop <pdf> "
                f"--citekey {props['citekey']} --fig …` 生成 manifest 再发布"
            )
        figures = fdoc.note_figures(note_path)
        # Pass the full note so replace_body can render the 速览 callout plus the
        # body; the table count check below still uses props["body"].
        fdoc.replace_body(fc, doc_id, props["md"], figures)
        timings["body_write"] = round((time.time() - started) * 1000)

        url = fc.doc_url(doc_id)
        fc.grant_public_access(doc_id)

        fields = record_fields(props, tags, url)
        record_id = rows[0].get("record_id") if rows else None
        started = time.time()
        if record_id:
            fc.update_record(ws["app_token"], ws["tables"]["papers"], record_id, fields)
            action = "row_updated"
        else:
            rec = fc.create_record(ws["app_token"], ws["tables"]["papers"], fields)
            record_id = rec.get("record_id")
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

        zotero = "not requested"
        if args.zotero:
            zotero = write_zotero(props["citekey"], url, tags, timings, props["title"],
                                  props.get("alt_titles") or ())

        index[props["citekey"]] = {
            "doc_id": doc_id, "url": url, "record_id": record_id,
            "rev": props["rev"], "updated_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        }
        save_json(index_path, index)
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
    except fapi.FeishuError as e:
        sys.exit(f"feishu error {e.code}: {e.msg}")

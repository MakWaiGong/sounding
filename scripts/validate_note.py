"""Check Sounding note structure and local references, not scientific correctness.

Reads Markdown and an optional pdf_prepare.py source manifest. It never contacts
Notion, Zotero or image URLs. Exit 1 means structural errors; warnings require
human/model inspection and are not claims that source evidence is sufficient.
"""
import argparse
import hashlib
import json
from pathlib import Path
import re
from urllib.parse import unquote, urlparse

from run_state import atomic_json


PARTS = {
    "conference": (("科学故事", "痛点"), ("灵感",), ("具体算法",), ("基线对比", "极限对比")),
    "journal": (("痛点",), ("空白", "核心新发现"), ("论证链",)),
    "other": (("惯例",), ("覆盖面",), ("评价方法",), ("拿它做什么",)),
}
HEAD = re.compile(r"^\s{0,3}(#{1,6})\s+(.+?)\s*#*\s*$")
FIGURE = re.compile(r"\b(?:(Extended\s+Data|Supplementary)\s+)?(?:Fig(?:ure)?s?\.?)\s*(S?\d+)(?:[a-z])?", re.I)
TABLE = re.compile(r"\b(?:(Extended\s+Data|Supplementary)\s+)?Tables?\s*(S?\d+)(?:[a-z])?", re.I)


def visible_lines(text):
    """Exclude fenced examples/comments from structural and reference checks."""
    lines = re.sub(r"<!--.*?-->", "", text, flags=re.S).splitlines()
    result, fence = [], None
    for line in lines:
        marker = re.match(r"^\s*(`{3,}|~{3,})", line)
        if marker:
            symbol = marker.group(1)[0]
            if fence is None:
                fence = (symbol, len(marker.group(1)))
            elif symbol == fence[0] and len(marker.group(1)) >= fence[1]:
                fence = None
            result.append("")
        else:
            result.append(line if fence is None else "")
    return result


def content_lines(lines):
    return [line.strip() for line in lines if line.strip() and not
            re.fullmatch(r"\s*(?:[-*_]\s*){3,}", line)]


def label_value(line):
    match = re.match(r"^(标题|发表|类型|citekey)\s*[:：]\s*(.+)$", line.replace("**", ""), re.I)
    return (match.group(1).lower(), match.group(2).strip()) if match else (None, None)


def caption_key(kind, number, prefix=""):
    return f"{prefix.lower().strip()}|{kind}|{str(number).upper()}"


def reference_keys(text):
    keys = set()
    for kind, pattern in (("figure", FIGURE), ("table", TABLE)):
        for match in pattern.finditer(text):
            prefix = re.sub(r"\s+", " ", match.group(1) or "")
            keys.add(caption_key(kind, match.group(2), prefix))
    return keys


def image_targets(text):
    """Common inline/reference Markdown and Notion-style image destinations."""
    targets = []
    for match in re.finditer(r"!\[[^\]]*\]\(", text):
        offset = match.end()
        depth, end = 1, offset
        while end < len(text) and depth:
            if text[end] == "\\":
                end += 2
                continue
            if text[end] == "(":
                depth += 1
            elif text[end] == ")":
                depth -= 1
            end += 1
        if depth:
            continue
        body = text[offset:end - 1].strip()
        if body.startswith("<") and ">" in body:
            target = body[1:body.index(">")]
        else:
            target = re.split(r"\s+[\"']", body, maxsplit=1)[0].strip()
        targets.append(target.replace(r"\(", "(").replace(r"\)", ")"))
    definitions = {key.casefold(): dest for key, dest in re.findall(
        r"^\s*\[([^\]]+)\]:\s*(<[^>]+>|\S+)", text, re.M)}
    for match in re.finditer(r"!\[([^\]]+)\](?:\[([^\]]*)\])?(?!\()", text):
        key = (match.group(2) or match.group(1)).casefold()
        targets.append(definitions.get(key, "missing-reference:" + key).strip("<>"))
    targets += re.findall(r'<(?:image|img)\b[^>]*\b(?:source|src)="([^"]+)"', text, re.I)
    return targets


def validate_note(path, source_manifest=None, expected_citekey=None):
    path = Path(path).resolve(strict=True)
    raw = path.read_bytes()
    text = raw.decode("utf-8-sig")
    lines = visible_lines(text)
    visible = "\n".join(lines)
    errors, warnings = [], []

    def error(code, message):
        errors.append({"code": code, "message": message})

    def warn(code, message):
        warnings.append({"code": code, "message": message})

    metadata = {}
    nonempty = [(i, line.strip()) for i, line in enumerate(lines) if line.strip()]
    for index, expected in enumerate(("标题", "发表", "类型", "citekey")):
        key, value = label_value(nonempty[index][1]) if index < len(nonempty) else (None, None)
        if key != expected:
            error("metadata_header", f"第 {index + 1} 行元信息应为 {expected}：<内容>")
        elif key:
            metadata[key] = value
    if metadata.get("发表") and not re.search(r"\b(?:19|20)\d{2}\b", metadata["发表"]):
        error("venue_year", "正文发表信息需要带年份")
    paper_type = metadata.get("类型", "")
    if paper_type == "review":
        paper_type = "other"
    if paper_type not in PARTS:
        error("paper_type", "类型应为 conference、journal、other 或 review")
    headers = [(i, len(m.group(1)), m.group(2).replace("**", ""))
               for i, line in enumerate(lines) if (m := HEAD.match(line))]
    meta_end = nonempty[3][0] + 1 if len(nonempty) >= 4 else len(lines)
    digest_header = next((h for h in headers if "速览" in h[2] or
                          "10-line digest" in h[2].lower()), None)
    start = digest_header[0] + 1 if digest_header else meta_end
    end = next((h[0] for h in headers if h[0] >= start), len(lines))
    digest = [line for line in content_lines(lines[start:end]) if not
              re.match(r"^(?:本地 PDF|Zotero)\s*[:：]", line, re.I)]
    if len(digest) != 10:
        error("digest_line_count", f"速览需要 10 行，实际 {len(digest)} 行")
    numbered = [re.match(r"^(\d+)\.\s+", line) for line in digest]
    if any(numbered) and (not all(numbered) or
                          [int(m.group(1)) for m in numbered if m] != list(range(1, 11))):
        error("digest_numbering", "编号速览应连续使用 1 至 10")
    position = -1
    for choices in PARTS.get(paper_type, ()):
        candidate = next((h for h in headers if h[0] > position and h[0] >= end
                          and h[1] <= 2 and any(x in h[2] for x in choices)), None)
        if candidate is None:
            error("template_section", "缺少或顺序不符的主要章节：" + " / ".join(choices))
        else:
            position = candidate[0]
    for title in ("与已读的关系", "待查"):
        heading = next((h for h in headers if title in h[2]), None)
        if not heading:
            error("required_section", "缺少章节：" + title)
            continue
        stop = next((h[0] for h in headers if h[0] > heading[0] and h[1] <= heading[1]), len(lines))
        section = content_lines(lines[heading[0] + 1:stop])
        substance = [line for i, line in enumerate(section) if not (
            re.fullmatch(r"[|\s:\-]+", line) or
            (i + 1 < len(section) and re.fullmatch(r"[|\s:\-]+", section[i + 1])))]
        if not substance:
            error("empty_section", "章节没有内容：" + title)

    actual_citekey = metadata.get("citekey")
    if expected_citekey and actual_citekey != expected_citekey:
        error("citekey_mismatch", "笔记 citekey 与指定主键不同")

    local_images, remote_images = [], []
    for target in image_targets(visible):
        parsed = urlparse(target)
        if parsed.scheme in ("https", "http", "data"):
            remote_images.append(target)
            continue
        if target.startswith("missing-reference:"):
            error("image_reference_missing", target)
            continue
        if not target:
            error("image_target_empty", "图片链接没有目标")
            continue
        if parsed.scheme and not re.match(r"^[A-Za-z]:[\\/]", target):
            warn("image_scheme_unchecked", f"未检查图片协议：{parsed.scheme}")
            continue
        local = Path(unquote(target))
        if not local.is_absolute():
            local = path.parent / local
        local = local.resolve()
        local_images.append(str(local))
        if not local.is_file():
            error("image_missing", "图片文件不存在：" + str(local))
    if remote_images:
        warn("remote_images_unchecked", f"{len(remote_images)} 个远端/内嵌图片目标未做网络或显示验证")

    source_hash = None
    if source_manifest is not None:
        manifest = (json.loads(Path(source_manifest).read_text(encoding="utf-8-sig"))
                    if not isinstance(source_manifest, dict) else source_manifest)
        if not isinstance(manifest, dict):
            raise ValueError("source manifest must be a JSON object")
        source_hash = manifest.get("pdf_sha256")
        if not isinstance(source_hash, str) or not re.fullmatch(r"[0-9a-f]{64}", source_hash):
            raise ValueError("source manifest needs pdf_sha256")
        page_count = manifest.get("page_count")
        if type(page_count) is not int or page_count < 1:
            raise ValueError("source manifest needs positive page_count")
        if manifest.get("text_layer_status") == "absent":
            error("source_no_text_layer", "原 PDF 无文字层，不能把本次产物作为 sounding 分析完成")
        known = set()
        for page in manifest.get("pages", []):
            for cap in page.get("captions", []):
                known |= reference_keys(cap.get("label", "") or cap.get("text", ""))
                if cap.get("figure_number") is not None and cap.get("kind") in ("figure", "table"):
                    known.add(caption_key(cap["kind"], cap["figure_number"]))
        for key in sorted(reference_keys(visible) - known):
            warn("source_reference_unlocated", f"提取的图注中未定位 {key}；需核对原文/补充材料，候选图注提取可能不完整")
        page_refs = [int(n) for n in re.findall(r"\b(?:p\.|page)\s*(\d+)\b", visible, re.I)]
        page_refs += [int(n) for n in re.findall(r"第\s*(\d+)\s*页", visible)]
        for page in sorted(set(page_refs)):
            if not 1 <= page <= page_count:
                error("page_out_of_range", f"引用页码 {page} 不在 1..{page_count} 内")
    else:
        warn("source_not_checked", "未提供 source manifest，未检查 PDF 页码与图表编号")
    return {"schema_version": 2, "valid": not errors,
            "citekey": actual_citekey, "note_path": str(path),
            "note_sha256": hashlib.sha256(raw).hexdigest(), "source_sha256": source_hash,
            "errors": errors, "warnings": warnings,
            "images": {"local": local_images, "remote": remote_images},
            "scope": "Mechanical structure/reference checks only; semantic evidence support is not verified."}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("note")
    parser.add_argument("--source-manifest")
    parser.add_argument("--citekey")
    parser.add_argument("--output", help="Write the same JSON report atomically")
    args = parser.parse_args(argv)
    try:
        report = validate_note(args.note, args.source_manifest, args.citekey)
        if args.output:
            atomic_json(args.output, report)
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return 0 if report["valid"] else 1
    except (OSError, UnicodeError, ValueError, TypeError) as exc:
        parser.exit(2, f"validation error: {exc}\n")


if __name__ == "__main__":
    raise SystemExit(main())

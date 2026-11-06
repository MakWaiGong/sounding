"""Write a sounding note into a Feishu docx document.

Three primitives, each verified against the live API before being trusted:

- **Markdown -> blocks.** ``POST /docx/v1/documents/blocks/convert`` does the
  heavy lifting, so the note template stays one dialect. It is a pure converter
  (no document id); the blocks it returns are inserted with ``block_id`` and
  ``parent_id`` stripped, otherwise the insert call rejects them.
- **Tables.** The converter returns table blocks whose cells are separate blocks
  that cannot be created directly, so a pipe table is built explicitly: create
  the table block, then write one text block into each cell.
- **Images.** An image block is created empty, the bytes are uploaded against
  that block's id (``parent_type=docx_image``, ``extra.drive_route_token`` = the
  document), and the block is patched with ``replace_image``. Uploading alone
  does *not* attach the picture, and the block keeps an empty token if the patch
  is skipped -- a failure that only shows up in an export, not in the API reply.

A figure that cannot be attached raises instead of being dropped: a note without
its figures is not the note.

Rendering (``replace_body``) is richer than a flat append: the 速览 becomes a
light-blue callout whose 去留 line is coloured by verdict, figures are placed at
their first ``Fig. N`` mention in the body instead of a trailing appendix, and
section headings are tinted by level (H2 blue, H3 purple, H4 grey).
"""
from __future__ import annotations

import re
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# Inline HTML in the note template has no meaning to the converter.
_HTML = [(re.compile(p, re.I), r) for p, r in (
    (r"</?b>", "**"), (r"</?strong>", "**"), (r"</?i>", "*"),
    (r"</?em>", "*"), (r"<br\s*/?>", "\n"),
)]
_PIPE_ROW = re.compile(r"^\s*\|.*\|\s*$")
_PIPE_SEP = re.compile(r"^\s*\|[\s:|-]+\|\s*$")
_MATH = re.compile(r"\$\$(.+?)\$\$|\$(.+?)\$")
# A note may describe each figure on its own line: 图注：Fig. 1 —— 说明文字
_FIG_CAPTION = re.compile(r"^图注\s*[：:]\s*(?:Fig(?:ure)?\.?\s*)?(\d+)\s*[—–\-:：]+\s*(.+)$")

# -- Feishu docx styling enums (verified against the live API) ------------
_FONT_COLOR = {"pink": 1, "orange": 2, "yellow": 3, "green": 4,
               "blue": 5, "purple": 6, "grey": 7}
_CALLOUT_BG = {"light_red": 1, "light_orange": 2, "light_yellow": 3, "light_green": 4,
               "light_blue": 5, "light_purple": 6, "light_grey": 7,
               "dark_red": 8, "dark_orange": 9, "dark_yellow": 10, "dark_green": 11,
               "dark_blue": 12, "dark_purple": 13, "dark_grey": 14}
_CALLOUT_BORDER = {"red": 1, "orange": 2, "yellow": 3, "green": 4,
                   "blue": 5, "purple": 6, "grey": 7}

# block_type 3..11 -> heading1..heading9
_HEADING_KEY = {n: "heading%d" % (n - 2) for n in range(3, 12)}
_HEADING_COLOR = {3: _FONT_COLOR["blue"], 4: _FONT_COLOR["blue"],
                  5: _FONT_COLOR["purple"], 6: _FONT_COLOR["grey"]}
_VERDICT_COLOR = {"值得跟进": _FONT_COLOR["green"], "存档": _FONT_COLOR["grey"],
                  "仅作背景": _FONT_COLOR["orange"]}

_DIGEST = re.compile(r"^(\d{1,2})\.\s+(.+)$")
_VERDICT = re.compile(r"去留[：:]\s*([^\s。]+)")
_FIG_LABEL = re.compile(r"^fig(?:ure)?_?(\d+)", re.I)
_FIG_REF = re.compile(r"Fig(?:ure)?\.?\s*(\d+)", re.I)
_LIST_ITEM = re.compile(r"^\s*(?:[-*+]|\d+[.)])\s")


def clean_markdown(md):
    for pattern, repl in _HTML:
        md = pattern.sub(repl, md)
    return md


def split_markdown(md):
    """Break markdown into ordered chunks: ('md', text) and ('table', rows)."""
    chunks, buf, table = [], [], []

    def flush_md():
        if buf and any(line.strip() for line in buf):
            chunks.append(("md", "\n".join(buf)))
        buf.clear()

    def flush_table():
        if table:
            chunks.append(("table", list(table)))
        table.clear()

    for line in md.splitlines():
        if _PIPE_ROW.match(line):
            if buf:
                flush_md()
            table.append(line)
            continue
        if table:
            if _PIPE_SEP.match(line):
                table.append(line)
                continue
            flush_table()
        buf.append(line)
    flush_table()
    flush_md()
    return chunks


def parse_table(lines):
    """Pipe table -> list of rows (cells stripped of markdown emphasis).

    Cells are split on ``|`` that is *not* preceded by a backslash, so a LaTeX
    norm ``\\|W_1\\|`` inside a cell survives as one cell instead of exploding
    the row into extra columns.
    """
    rows = []
    for line in lines:
        if _PIPE_SEP.match(line):
            continue
        cells = [c.strip() for c in re.split(r"(?<!\\)\|", line.strip().strip("|"))]
        cells = [re.sub(r"\*\*(.+?)\*\*", r"\1", c).replace("`", "") for c in cells]
        rows.append(cells)
    width = max(len(r) for r in rows) if rows else 0
    return [r + [""] * (width - len(r)) for r in rows], width


def inline_elements(text, color=None, bold=False, italic=False):
    """Split text on $...$ into text_run + equation elements.

    The markdown converter already turns inline math into ``equation`` elements,
    but text we build by hand (table cells, callout lines, captions) would
    otherwise land ``$...$`` as literal text. KaTeX is the only dialect Feishu
    renders (per the Block/TextElement docs).
    """
    if not text:
        return []

    def run(content):
        return _text_element(content, color, bold, italic)

    out, pos = [], 0
    for m in _MATH.finditer(text):
        if m.start() > pos:
            out.append(run(text[pos:m.start()]))
        body = m.group(1) if m.group(1) is not None else m.group(2)
        out.append({"equation": {"content": body}})
        pos = m.end()
    if pos < len(text):
        out.append(run(text[pos:]))
    return out or [run(text)]


def md_to_blocks(client, md):
    """Convert markdown to insertable blocks (ids and parents removed).

    The convert endpoint returns ``blocks`` as an unordered map — the document
    order lives in ``first_level_block_ids``. Reading the array as-is silently
    scrambles paragraphs, so reorder by that list first.
    """
    if not md.strip():
        return []
    data = client.post("/open-apis/docx/v1/documents/blocks/convert",
                       {"content_type": "markdown", "content": clean_markdown(md)})
    blocks = data.get("blocks") or []
    order = data.get("first_level_block_ids") or []
    if order:
        by_id = {block.get("block_id"): block for block in blocks}
        ordered = [by_id[bid] for bid in order if bid in by_id]
        seen = set(order)
        ordered.extend(block for block in blocks if block.get("block_id") not in seen)
        blocks = ordered
    out = []
    for block in blocks:
        block.pop("block_id", None)
        block.pop("parent_id", None)
        block.pop("children", None)
        out.append(block)
    return out


def append_table(client, doc_id, rows, width):
    """Create a table and fill every cell. Returns the table block id.

    Feishu refuses to create a table with more than 9 rows (``1770001``), so a
    bigger table is created at 9 rows and grown with ``insert_table_row``.
    A cell has no text block until one is appended, so an empty cell stays quiet
    rather than raising.
    """
    _MAX_ROW = 9
    create_row = min(len(rows), _MAX_ROW)
    payload = {"block_type": 31, "table": {"property": {
        "row_size": create_row, "column_size": width, "header_row": True}}}
    created = client.append_children(doc_id, [payload])[0]
    table_id = created["block_id"]
    if len(rows) > create_row:
        # Each insert_table_row call adds exactly one row (the API ignores
        # row_size), so insert the surplus one call at a time.
        for _ in range(len(rows) - create_row):
            client.batch_update_blocks(doc_id, [{
                "block_id": table_id,
                "insert_table_row": {"row_index": create_row, "row_size": 1},
            }])
    # Cells come back flat in row-major order: 20 cells for a 5x4 table. Each
    # already holds one empty text block, so the text is patched into place
    # instead of appended (appending leaves a blank line above the value).
    # One tree read plus one batch update replaces two calls per cell.
    blocks = client.all_blocks(doc_id)
    by_parent = {}
    for block in blocks:
        by_parent.setdefault(block.get("parent_id"), []).append(block)
    cells = by_parent.get(table_id, [])
    updates = []
    for r, row in enumerate(rows):
        for col, text in enumerate(row):
            pos = r * width + col
            if not text or pos >= len(cells):
                continue
            inner = by_parent.get(cells[pos]["block_id"], [])
            if inner:
                updates.append({"block_id": inner[0]["block_id"],
                                "update_text_elements": {
                                    "elements": inline_elements(text)}})
    client.batch_update_blocks(doc_id, updates)
    return table_id


def add_figure(client, doc_id, path, caption=""):
    """Attach one image file to the document, with an optional caption below."""
    block = client.append_children(doc_id, [{"block_type": 27, "image": {}}])[0]
    block_id = block["block_id"]
    with open(path, "rb") as f:
        blob = f.read()
    token = client.upload_media(blob, os.path.basename(path), "docx_image", block_id,
                               extra={"drive_route_token": doc_id})
    client.patch(f"/open-apis/docx/v1/documents/{doc_id}/blocks/{block_id}",
                 {"replace_image": {"token": token}})
    if caption:
        # A caption is explanatory, so it sits grey and italic right under the
        # figure instead of being another body paragraph.
        client.append_children(
            doc_id, [_text_block(caption, _FONT_COLOR["grey"], italic=True)])
    return block_id


# -- styling helpers -----------------------------------------------------

def _text_element(content, color=None, bold=False, italic=False):
    """A Feishu ``TextElement``: ``{"text_run": {...}}`` (the wrapper is required)."""
    run = {"content": content}
    style = {}
    if color:
        style["text_color"] = color
    if bold:
        style["bold"] = True
    if italic:
        style["italic"] = True
    if style:
        run["text_element_style"] = style
    return {"text_run": run}


def _text_block(content, color=None, bold=False, italic=False):
    return {"block_type": 2,
            "text": {"style": {}, "elements": inline_elements(content, color, bold, italic)}}


def _recolor_headings(blocks):
    """Tint headings by level in the blocks the markdown converter returns."""
    for block in blocks:
        color = _HEADING_COLOR.get(block.get("block_type"))
        if color is None:
            continue
        node = block.get(_HEADING_KEY[block["block_type"]]) or {}
        for element in node.get("elements") or []:
            run = element.get("text_run")
            if run:
                run.setdefault("text_element_style", {})["text_color"] = color
    return blocks


def _split_note(md):
    """Full note -> (digest_lines, body). Title/meta are in the doc name and table
    row, so the body starts at the first ``---`` and the numbered 速览 lines become
    the digest. A body-only markdown (no ``---``) yields an empty digest."""
    lines = md.splitlines()
    body_start = None
    for i, line in enumerate(lines):
        if line.strip() == "---":
            body_start = i + 1
            break
    head = lines[: body_start - 1] if body_start else lines
    digest = []
    for line in head:
        m = _DIGEST.match(line.strip())
        if m:
            digest.append(m.group(2))
    body = "\n".join(lines[body_start:]).lstrip("\r\n") if body_start else md
    return digest, body


def _append_digest_callout(client, doc_id, digest):
    """The 速览 as a light-blue callout with a pushpin; the 去留 line is tinted."""
    if not digest:
        return None
    items = []
    for line in digest:
        m = _VERDICT.search(line)
        items.append((line, _VERDICT_COLOR.get(m.group(1)) if m else None))
    created = client.append_children(doc_id, [{
        "block_type": 19,
        "callout": {"background_color": _CALLOUT_BG["light_blue"],
                    "border_color": _CALLOUT_BORDER["blue"],
                    "emoji_id": "pushpin"},
    }])[0]
    callout_id = created["block_id"]
    # The API pre-creates one empty text child inside a callout; reuse it for the
    # first digest line so the callout does not start with a blank line.
    kids = client.block_children(doc_id, callout_id)
    empty = next((b for b in kids if b.get("block_type") == 2), None)
    if empty is not None and items:
        content, color = items.pop(0)
        client.batch_update_blocks(doc_id, [{
            "block_id": empty["block_id"],
            "update_text_elements": {"elements": inline_elements(content, color)},
        }])
    if items:
        client.append_block_children(
            doc_id, callout_id,
            [_text_block(content, color) for content, color in items])
    return callout_id


def _fig_mentions(lines, num, allowed=None):
    """Every ``(line_index, char_pos, char_end)`` of ``Fig. N`` in narrative prose,
    skipping ``Extended Data`` / ``Supplementary`` references."""
    hits = []
    for i, line in enumerate(lines):
        if allowed is not None and i not in allowed:
            continue
        for m in _FIG_REF.finditer(line):
            if m.group(1) != num:
                continue
            before = line[: m.start()].lower()
            if "extended data" in before[-20:] or "supplementary" in before[-20:]:
                continue
            hits.append((i, m.start(), m.end()))
    return hits


def _first_fig_mention(lines, num, allowed=None):
    """First narrative ``(line_index, char_pos)`` for ``Fig. N`` or ``None``."""
    hits = _fig_mentions(lines, num, allowed)
    return (hits[0][0], hits[0][1]) if hits else None


# Sections that describe the note or list facts; a figure must not be anchored to
# the mentions inside them (PDF pointer lists, evidence tables, caption source).
_NON_NARRATIVE_SECTIONS = {
    "PDF 与来源", "速览", "符号表", "指标", "与已读的关系", "待查",
    "定位素材", "实验清单", "参考文献",
}


def _narrative_indices(lines):
    """Indices of lines that belong to narrative prose.

    Excludes fenced code (the 论证地图 diagram), table rows, and the sections in
    ``_NON_NARRATIVE_SECTIONS``: a ``Fig. N`` there is a citation, not the
    paragraph a reader expects the image to sit next to.
    """
    allowed = set()
    section = ""
    in_code = False
    for i, line in enumerate(lines):
        stripped = line.strip()
        if stripped.startswith("```"):
            in_code = not in_code
            continue
        if in_code:
            continue
        if line.startswith("## "):
            section = line[3:].strip()
            continue
        if section in _NON_NARRATIVE_SECTIONS:
            continue
        if stripped.startswith("|"):
            continue
        allowed.add(i)
    return allowed


def _block_end(lines, index, allowed=None):
    """Last line of the paragraph or list item that starts at ``index``.

    A figure lands after the prose that explains it instead of splitting a
    sentence or jumping to the end of a numbered list.
    """
    last = index
    for j in range(index + 1, len(lines)):
        stripped = lines[j].strip()
        if not stripped or lines[j].startswith("#"):
            break
        if allowed is not None and j not in allowed:
            break
        if _LIST_ITEM.match(lines[j]):
            break
        last = j
    return last


def _append_body(client, doc_id, body, figures):
    """Write the body, dropping each figure right after the paragraph that
    narrates it.

    ``figures`` is an iterable of ``(path, caption, label)``. An anchor is only
    a mention inside narrative prose (``_narrative_indices``); figures whose
    mention sits in a table, a diagram or a metadata section are anchored at the
    paragraph that actually discusses them, and any figure never mentioned in
    prose falls back to the end. Figures sharing a paragraph keep mention order.
    """
    lines = body.splitlines()
    # 图注 lines feed the caption under each figure; they are not body prose.
    lines = [line for line in lines if not _FIG_CAPTION.match(line.strip())]
    allowed = _narrative_indices(lines)
    by_num = {}
    for path, caption, label in figures:
        m = _FIG_LABEL.match(label)
        if m:
            by_num.setdefault(m.group(1), []).append((path, caption))

    insertions = []  # (line_index, char_pos, path, caption)
    placed = set()
    used_lines = set()
    for num in by_num:
        path, caption = by_num[num][0]
        anchor = None
        for i, pos, _end in _fig_mentions(lines, num, allowed):
            if i not in used_lines:
                anchor = (i, pos)
                break
        if anchor is None:
            continue
        line_idx, char_pos = anchor
        used_lines.add(line_idx)
        insertions.append((_block_end(lines, line_idx, allowed), char_pos, path, caption))
        placed.add(num)
    insertions.sort(key=lambda item: (item[0], item[1]))

    by_line = {}
    for line_idx, _char_pos, path, caption in insertions:
        by_line.setdefault(line_idx, []).append((path, caption))

    buf = []
    for i, line in enumerate(lines):
        buf.append(line)
        if i in by_line:
            append_md(client, doc_id, "\n".join(buf))
            buf = []
            for path, caption in by_line[i]:
                add_figure(client, doc_id, path, caption)
    append_md(client, doc_id, "\n".join(buf))

    for num in by_num:
        if num not in placed:
            for path, caption in by_num[num]:
                add_figure(client, doc_id, path, caption)


def append_md(client, doc_id, md):
    """Append markdown (tables included, headings tinted) to the end of a document."""
    for kind, payload in split_markdown(md):
        if kind == "table":
            rows, width = parse_table(payload)
            if rows:
                append_table(client, doc_id, rows, width)
        else:
            blocks = md_to_blocks(client, payload)
            if blocks:
                client.append_children(doc_id, _recolor_headings(blocks))
    return client


def replace_body(client, doc_id, md, figures=()):
    """Rebuild the whole document: 速览 callout, then the body with inline figures."""
    client.clear_body(doc_id)
    digest, body = _split_note(md)
    _append_digest_callout(client, doc_id, digest)
    _append_body(client, doc_id, body, figures)
    return doc_id


def orphan_figure_files(note_path, figures_root=None):
    """Cropped PNGs sitting in notes/figs/<citekey>/ without a manifest.json.

    ``note_figures`` returns ``[]`` when the manifest is absent, so publishing
    would silently ship a figure-less note. The publish step calls this first to
    turn that silence into a stop.
    """
    citekey = os.path.splitext(os.path.basename(note_path))[0]
    root = figures_root or os.path.join(os.path.dirname(os.path.abspath(note_path)), "figs")
    folder = os.path.join(root, citekey)
    if not os.path.isdir(folder) or os.path.exists(os.path.join(folder, "manifest.json")):
        return []
    return sorted(
        os.path.join(folder, name)
        for name in os.listdir(folder)
        if name.lower().endswith(".png")
    )


def note_figures(note_path, figures_root=None):
    """(path, caption, label) triples from notes/figs/<citekey>/manifest.json.

    The caption comes from a ``图注：Fig. N —— …`` line in the note when the
    note has one (see ``reference-science.md``), so the document explains what
    the figure shows instead of just naming it.
    """
    import json

    citekey = os.path.splitext(os.path.basename(note_path))[0]
    root = figures_root or os.path.join(os.path.dirname(os.path.abspath(note_path)), "figs")
    manifest = os.path.join(root, citekey, "manifest.json")
    if not os.path.exists(manifest):
        return []
    with open(manifest, encoding="utf-8") as f:
        data = json.load(f)
    captions = {}
    if os.path.exists(note_path):
        with open(note_path, encoding="utf-8") as f:
            for line in f:
                m = _FIG_CAPTION.match(line.strip())
                if m:
                    captions[m.group(1)] = m.group(2).strip()
    out = []
    for fig in data.get("figures") or []:
        label = fig.get("label", "")
        # The manifest's local_path can go stale (e.g. the figures moved beside
        # the notes); fall back to <figs>/<citekey>/<label>.png when it does.
        path = fig.get("local_path") or ""
        if not path or not os.path.exists(path):
            path = os.path.join(root, citekey, label + ".png")
        if not os.path.exists(path):
            continue
        num = (_FIG_LABEL.match(label).group(1) if _FIG_LABEL.match(label) else "")
        page = fig.get("page", "?")
        display = f"Fig. {num}" if num else label
        text = captions.get(num)
        if text:
            caption = f"{display}　{text}（原 PDF 第 {page} 页）"
        else:
            caption = f"{display}（原 PDF 第 {page} 页）"
        out.append((path, caption, label))
    return out


if __name__ == "__main__":
    import argparse

    from scripts import config as conf
    from scripts import feishu_api as fapi

    ap = argparse.ArgumentParser(description="Write one note into a Feishu doc.")
    ap.add_argument("note", help="path to notes/<citekey>.md")
    ap.add_argument("--keep", action="store_true", help="do not delete the document at the end")
    args = ap.parse_args()

    with open(args.note, encoding="utf-8") as f:
        md = f.read()
    client = fapi.client_from_config(conf.feishu())
    folder = conf.feishu().get("folder_token", "")
    doc = client.create_doc(os.path.basename(args.note), folder)
    try:
        figs = note_figures(args.note)
        replace_body(client, doc, md, figs)
        text = client.doc_text(doc)
        kids = client.doc_children(doc)
        images = [b for b in kids if b.get("block_type") == 27]
        print(f"doc        {doc}")
        print(f"blocks     {len(kids)}  images {len(images)}  figures {len(figs)}")
        print(f"text chars {len(text)}")
        print(f"url        {client.doc_url(doc)}")
        if not args.keep:
            client.delete(f"/open-apis/drive/v1/files/{doc}", params={"type": "docx"})
            print("removed")
    finally:
        client.close()

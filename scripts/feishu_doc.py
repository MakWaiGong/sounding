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

import copy
import hashlib
import json
import os
import re
import sys
import tempfile
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from contextlib import contextmanager

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

if __package__:
    from . import feishu_api as _fapi
else:
    import feishu_api as _fapi

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
    Columns share a 1020px desktop target, with a 50px minimum per column;
    this is not responsive sizing.
    An empty cell stays quiet rather than raising.
    """
    if not isinstance(width, int) or isinstance(width, bool) or width < 1:
        raise ValueError("table width must be a positive integer")
    if not rows or any(len(row) > width for row in rows):
        raise ValueError("table must have rows with no more cells than columns")
    base, remainder = divmod(max(1020, 50 * width), width)
    column_width = [base + (col < remainder) for col in range(width)]
    _MAX_ROW = 9
    create_row = min(len(rows), _MAX_ROW)
    payload = {"block_type": 31, "table": {"property": {
        "row_size": create_row, "column_size": width, "header_row": True,
        "column_width": column_width}}}
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
    if getattr(client, "_strict_skeleton", False) is True:
        if len(cells) != len(rows) * width:
            raise ValueError("incomplete table skeleton")
        for cell in cells:
            inner = by_parent.get(cell.get("block_id"), [])
            if len(inner) != 1 or inner[0].get("block_type") != 2:
                raise ValueError("incomplete table cell skeleton")
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
    if getattr(client, "_strict_skeleton", False) is True and empty is None:
        raise ValueError("incomplete callout skeleton")
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


def _append_body(client, doc_id, body, figures, figure_writer=None):
    """Write the body, dropping each figure right after the paragraph that
    narrates it.

    ``figures`` is an iterable of ``(path, caption, label)``. An anchor is only
    a mention inside narrative prose (``_narrative_indices``); figures whose
    mention sits in a table, a diagram or a metadata section are anchored at the
    paragraph that actually discusses them, and any figure never mentioned in
    prose falls back to the end. Figures sharing a paragraph keep mention order.
    """
    if figure_writer is None:
        figure_writer = lambda c, d, p, caption, label: add_figure(c, d, p, caption)
    lines = body.splitlines()
    # 图注 lines feed the caption under each figure; they are not body prose.
    lines = [line for line in lines if not _FIG_CAPTION.match(line.strip())]
    allowed = _narrative_indices(lines)
    by_num = {}
    for path, caption, label in figures:
        m = _FIG_LABEL.match(label)
        if m:
            by_num.setdefault(m.group(1), []).append((path, caption, label))

    insertions = []  # (line_index, char_pos, path, caption, label)
    placed = set()
    used_lines = set()
    for num in by_num:
        path, caption, label = by_num[num][0]
        anchor = None
        for i, pos, _end in _fig_mentions(lines, num, allowed):
            if i not in used_lines:
                anchor = (i, pos)
                break
        if anchor is None:
            continue
        line_idx, char_pos = anchor
        used_lines.add(line_idx)
        insertions.append((_block_end(lines, line_idx, allowed), char_pos, path, caption, label))
        placed.add(num)
    insertions.sort(key=lambda item: (item[0], item[1]))

    by_line = {}
    for line_idx, _char_pos, path, caption, label in insertions:
        by_line.setdefault(line_idx, []).append((path, caption, label))

    buf = []
    for i, line in enumerate(lines):
        buf.append(line)
        if i in by_line:
            append_md(client, doc_id, "\n".join(buf))
            buf = []
            for path, caption, label in by_line[i]:
                figure_writer(client, doc_id, path, caption, label)
    append_md(client, doc_id, "\n".join(buf))

    for num in by_num:
        if num not in placed:
            for path, caption, label in by_num[num]:
                figure_writer(client, doc_id, path, caption, label)


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


_JOURNAL_VERSION = 1
_MAX_IMAGE_WORKERS = 32


@contextmanager
def _journal_lock(path):
    # The lock file is deliberately retained: unlinking it can split waiters
    # across two inodes and permit overlapping publishes.
    with open(path + ".lock", "a+b") as lock:
        lock.seek(0, os.SEEK_END)
        if lock.tell() == 0:
            lock.write(b"\0")
            lock.flush()
        lock.seek(0)
        if os.name == "nt":
            import msvcrt
            try:
                msvcrt.locking(lock.fileno(), msvcrt.LK_NBLCK, 1)
            except OSError as exc:
                raise RuntimeError("image checkpoint is already in use") from exc
        else:
            import fcntl
            try:
                fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError as exc:
                raise RuntimeError("image checkpoint is already in use") from exc
        try:
            yield
        finally:
            lock.seek(0)
            if os.name == "nt":
                msvcrt.locking(lock.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(lock.fileno(), fcntl.LOCK_UN)


def _journal_digest(state):
    encoded = json.dumps(state, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _save_journal(path, state):
    directory = os.path.dirname(path)
    fd, temporary = tempfile.mkstemp(prefix=".feishu-images-", suffix=".tmp", dir=directory)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            payload = dict(state, checksum=_journal_digest(state))
            json.dump(payload, stream, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        if os.name != "nt":
            directory_fd = os.open(directory, os.O_RDONLY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON key")
        result[key] = value
    return result


def _figure_number(label, seen):
    match = _FIG_LABEL.fullmatch(label) if isinstance(label, str) else None
    if not match:
        raise ValueError("figure label must be a numbered fig/figure label, e.g. fig1")
    number = match.group(1)
    key = number.lstrip("0") or "0"
    if key in seen:
        raise ValueError("duplicate figure number")
    seen.add(key)
    return number


def _figure_inputs(doc_id, md, figures):
    if not isinstance(doc_id, str) or not doc_id or not isinstance(md, str):
        raise ValueError("document id and note are required")
    descriptions, contents, nums = [], {}, set()
    for entry in figures:
        try:
            path, caption, label = entry
            path = os.fspath(path)
        except (TypeError, ValueError) as exc:
            raise ValueError("figure must be a (path, caption, label) triple") from exc
        if not isinstance(path, str) or not path or not isinstance(caption, str):
            raise ValueError("figure path and caption must be strings")
        _figure_number(label, nums)
        try:
            with open(path, "rb") as stream:
                blob = stream.read()
        except OSError:
            raise ValueError(f"figure {label!r} is missing or unreadable") from None
        name = os.path.basename(path)
        descriptions.append({"name": name, "caption": caption, "label": label,
                             "sha256": hashlib.sha256(blob).hexdigest()})
        contents[label] = (blob, name, path, caption)
    identity = {"doc_id": doc_id, "note_sha256": hashlib.sha256(md.encode("utf-8")).hexdigest(),
                "figures": descriptions}
    return identity, contents


def validate_figure_inputs(doc_id, md, figures):
    """Validate note and figure inputs locally, without mutating files or Feishu."""
    _figure_inputs(doc_id, md, figures)


def _block_snapshot(client, doc_id):
    roots = client.doc_children(doc_id)
    blocks = client.all_blocks(doc_id)
    if not isinstance(roots, list) or not isinstance(blocks, list):
        raise ValueError("invalid remote block readback")
    root_ids = []
    for block in roots:
        if not isinstance(block, dict) or not isinstance(block.get("block_id"), str) or not block["block_id"]:
            raise ValueError("invalid remote root block")
        root_ids.append(block["block_id"])
    normal, tokens = {}, {}
    for block in blocks:
        if not isinstance(block, dict):
            raise ValueError("invalid remote block")
        block = copy.deepcopy(block)
        block_id = block.get("block_id")
        if not isinstance(block_id, str) or not block_id or block_id in normal:
            raise ValueError("invalid or duplicate remote block id")
        # These bookkeeping fields can change without an edit to the content.
        for key in ("revision_id", "create_time", "update_time"):
            block.pop(key, None)
        if block.get("block_type") == 27:
            image = block.get("image")
            if not isinstance(image, dict):
                raise ValueError("invalid remote image block")
            token = image.pop("token", "")
            if token is None:
                token = ""
            if not isinstance(token, str):
                raise ValueError("invalid remote image token")
            tokens[block_id] = token
        normal[block_id] = block
    for parent in normal.values():
        children = parent.get("children", [])
        if not isinstance(children, list) or any(not isinstance(child, str) or child not in normal
                                                for child in children):
            raise ValueError("incomplete remote block tree")
    if len(set(root_ids)) != len(root_ids) or any(block_id not in normal for block_id in root_ids):
        raise ValueError("inconsistent remote block tree")
    return {"roots": root_ids, "blocks": [normal[key] for key in sorted(normal)]}, tokens


def _load_journal(path, identity=None):
    try:
        with open(path, encoding="utf-8") as stream:
            state = json.load(stream, object_pairs_hook=_unique_object)
    except (OSError, ValueError, UnicodeError) as exc:
        raise ValueError("invalid image checkpoint") from exc
    if not isinstance(state, dict) or set(state) != {"version", "identity", "phase", "snapshot", "images", "checksum"}:
        raise ValueError("invalid image checkpoint")
    checksum = state.pop("checksum")
    if not isinstance(checksum, str) or checksum != _journal_digest(state):
        raise ValueError("image checkpoint checksum mismatch")
    if type(state["version"]) is not int or state["version"] != _JOURNAL_VERSION:
        raise ValueError("unsupported image checkpoint version")
    stored_identity = state["identity"]
    if (not isinstance(stored_identity, dict)
            or set(stored_identity) != {"doc_id", "note_sha256", "figures"}
            or not isinstance(stored_identity["doc_id"], str) or not stored_identity["doc_id"]
            or not isinstance(stored_identity["note_sha256"], str)
            or not re.fullmatch(r"[0-9a-f]{64}", stored_identity["note_sha256"])
            or not isinstance(stored_identity["figures"], list)):
        raise ValueError("invalid image checkpoint identity")
    labels_in_identity = []
    for figure in stored_identity["figures"]:
        if (not isinstance(figure, dict) or set(figure) != {"name", "caption", "label", "sha256"}
                or not all(isinstance(figure[key], str) for key in figure)
                or not _FIG_LABEL.fullmatch(figure["label"])
                or not re.fullmatch(r"[0-9a-f]{64}", figure["sha256"])):
            raise ValueError("invalid image checkpoint identity")
        labels_in_identity.append(figure["label"])
    if len(set(labels_in_identity)) != len(labels_in_identity):
        raise ValueError("invalid image checkpoint identity")
    if identity is not None and stored_identity != identity:
        raise ValueError("image checkpoint does not match document, note or figures")
    if state["phase"] == "building":
        raise ValueError("image skeleton is incomplete or its outcome is uncertain; preserve the "
                         "checkpoint and inspect the document before manual recovery; "
                         "do not delete the checkpoint or rebuild the document")
    if state["phase"] != "ready" or not isinstance(state["snapshot"], dict) or not isinstance(state["images"], list):
        raise ValueError("invalid image checkpoint")
    expected_labels = set(labels_in_identity)
    labels, ids = set(), set()
    for image in state["images"]:
        if not isinstance(image, dict) or set(image) != {"label", "block_id", "status", "token"}:
            raise ValueError("invalid image checkpoint")
        label, block_id, status, token = (image[k] for k in ("label", "block_id", "status", "token"))
        if (not isinstance(label, str) or label not in expected_labels or label in labels
                or not isinstance(block_id, str) or not block_id or block_id in ids
                or status not in ("pending", "uploading", "uploaded", "patching", "confirmed")
                or not isinstance(token, str) or (status in ("pending", "uploading")) != (token == "")):
            raise ValueError("invalid image checkpoint")
        labels.add(label)
        ids.add(block_id)
    if labels != expected_labels:
        raise ValueError("invalid image checkpoint")
    return state


def _verify_snapshot(client, doc_id, state):
    snapshot, tokens = _block_snapshot(client, doc_id)
    if set(tokens) != {image["block_id"] for image in state["images"]}:
        raise ValueError("remote image blocks changed or are missing")
    expected = copy.deepcopy(state["snapshot"])
    if not isinstance(expected, dict) or not isinstance(expected.get("blocks"), list):
        raise ValueError("invalid image checkpoint snapshot")
    changed = {image["block_id"] for image in state["images"]
               if image["status"] == "patching" and tokens[image["block_id"]] == image["token"]}
    observed = {block["block_id"]: block for block in snapshot["blocks"]}
    for block in expected["blocks"]:
        if isinstance(block, dict) and block.get("block_id") in changed:
            block["image"] = copy.deepcopy(observed[block["block_id"]]["image"])
    if snapshot != expected:
        raise ValueError("remote document skeleton changed or is incomplete")
    reconciled = False
    for image in state["images"]:
        actual = tokens[image["block_id"]]
        status, expected = image["status"], image["token"]
        if status == "confirmed":
            if actual != expected:
                raise ValueError("confirmed remote image changed or is missing")
        elif status == "patching" and actual == expected:
            image["status"] = "confirmed"
            reconciled = True
        elif actual:
            raise ValueError("unexpected remote image token")
    if reconciled:
        state["snapshot"] = snapshot
    return reconciled


def _contains_expected(actual, expected):
    if isinstance(expected, dict):
        return isinstance(actual, dict) and all(
            (key in actual and _contains_expected(actual[key], value)) or (key not in actual and value in ({}, []))
            for key, value in expected.items())
    if isinstance(expected, list):
        return (isinstance(actual, list) and len(actual) == len(expected)
                and all(_contains_expected(a, e) for a, e in zip(actual, expected)))
    return actual == expected


class _SkeletonClient:
    _strict_skeleton = True

    def __init__(self, client):
        self.client = client
        self.root_ids = []
        self.expected_blocks = {}
        self.expected_updates = {}

    def __getattr__(self, name):
        return getattr(self.client, name)

    def append_children(self, doc_id, children, index=-1):
        result = self._check(children, self.client.append_children(doc_id, children, index=index), doc_id)
        self.root_ids.extend(block["block_id"] for block in result)
        return result

    def append_block_children(self, doc_id, block_id, children, index=-1):
        result = self.client.append_block_children(doc_id, block_id, children, index=index)
        return self._check(children, result, block_id)

    def batch_update_blocks(self, doc_id, requests):
        result = self.client.batch_update_blocks(doc_id, requests)
        for update in requests:
            block_id = update["block_id"]
            if "update_text_elements" in update:
                self.expected_updates[block_id] = copy.deepcopy(update["update_text_elements"]["elements"])
            if "insert_table_row" in update and block_id in self.expected_blocks:
                self.expected_blocks[block_id]["table"]["property"]["row_size"] += 1
        return result

    def _check(self, children, result, parent_id):
        if (not isinstance(result, list) or len(result) != len(children)
                or any(not isinstance(block, dict) or not isinstance(block.get("block_id"), str)
                       or not block["block_id"] for block in result)):
            raise ValueError("incomplete skeleton creation response")
        for child, block in zip(children, result):
            expected = copy.deepcopy(child)
            expected["parent_id"] = parent_id
            self.expected_blocks[block["block_id"]] = expected
        return result

    def verify(self, snapshot):
        blocks = {block["block_id"]: block for block in snapshot["blocks"]}
        for block_id, expected in self.expected_blocks.items():
            if block_id not in blocks or not _contains_expected(blocks[block_id], expected):
                raise ValueError("remote skeleton content did not match the requested blocks")
        for block_id, elements in self.expected_updates.items():
            actual = blocks.get(block_id, {}).get("text", {}).get("elements")
            if not _contains_expected(actual, elements):
                raise ValueError("remote skeleton text update was not confirmed")


def _build_skeleton(client, doc_id, md, identity, contents, journal_path):
    state = {"version": _JOURNAL_VERSION, "identity": identity, "phase": "building",
             "snapshot": None, "images": []}
    _save_journal(journal_path, state)
    client.clear_body(doc_id)
    skeleton = _SkeletonClient(client)

    def placeholder(writer, document, path, caption, label):
        created = writer.append_children(document, [{"block_type": 27, "image": {}}])[0]
        state["images"].append({"label": label, "block_id": created["block_id"],
                                "status": "pending", "token": ""})
        if caption:
            writer.append_children(document, [_text_block(caption, _FONT_COLOR["grey"], italic=True)])

    digest, body = _split_note(md)
    _append_digest_callout(skeleton, doc_id, digest)
    figures = [(contents[item["label"]][2], item["caption"], item["label"])
               for item in identity["figures"]]
    _append_body(skeleton, doc_id, body, figures, placeholder)
    state["snapshot"], tokens = _block_snapshot(client, doc_id)
    skeleton.verify(state["snapshot"])
    if (len(state["images"]) != len(identity["figures"])
            or state["snapshot"]["roots"] != skeleton.root_ids
            or set(tokens) != {image["block_id"] for image in state["images"]}
            or any(tokens.values())):
        raise ValueError("incomplete or incompatible remote image skeleton")
    state["phase"] = "ready"
    _save_journal(journal_path, state)
    return state


def _upload_images(client, doc_id, contents, state, journal_path, client_factory, max_workers):
    clients, client_guard = [], threading.Lock()
    local = threading.local()

    def upload(image):
        worker = getattr(local, "client", None)
        if worker is None:
            worker = client_factory()
            with client_guard:
                if worker is client or any(worker is other for other in clients):
                    raise ValueError("client_factory must return a new independent client")
                clients.append(worker)
            local.client = worker
        blob, name, _path, _caption = contents[image["label"]]
        token = worker.upload_media(blob, name, "docx_image", image["block_id"],
                                    extra={"drive_route_token": doc_id})
        if not isinstance(token, str) or not token:
            raise _fapi.FeishuUncertainError("image upload", -1,
                                              "upload returned no token; preserve the checkpoint "
                                              "and inspect the image block before manual recovery")
        return token

    def patch(image):
        if _verify_snapshot(client, doc_id, state):
            _save_journal(journal_path, state)
        if image["status"] == "confirmed":
            return
        image["status"] = "patching"
        _save_journal(journal_path, state)
        try:
            client.patch(f"/open-apis/docx/v1/documents/{doc_id}/blocks/{image['block_id']}",
                         {"replace_image": {"token": image["token"]}})
        except Exception:
            if _verify_snapshot(client, doc_id, state):
                _save_journal(journal_path, state)
            if image["status"] != "confirmed":
                raise
        else:
            if _verify_snapshot(client, doc_id, state):
                _save_journal(journal_path, state)
            if image["status"] != "confirmed":
                raise ValueError("image attachment was not confirmed by remote readback")

    errors = []
    for image in state["images"]:
        if image["status"] in ("uploaded", "patching"):
            try:
                patch(image)
            except Exception as exc:
                errors.append(exc)
                break
    if errors:
        raise errors[0]

    pending = [image for image in state["images"] if image["status"] == "pending"]
    if pending:
        try:
            with ThreadPoolExecutor(max_workers=min(max_workers, len(pending))) as executor:
                futures, waiting = {}, iter(pending)
                stop_patching = False

                def start_next():
                    image = next(waiting, None)
                    if image is not None:
                        image["status"] = "uploading"
                        _save_journal(journal_path, state)
                        futures[executor.submit(upload, image)] = image

                for _ in range(min(max_workers, len(pending))):
                    start_next()
                while futures:
                    future = next(as_completed(futures))
                    image = futures.pop(future)
                    try:
                        token = future.result()
                    except Exception as exc:
                        if isinstance(exc, _fapi.FeishuError) and not isinstance(exc, _fapi.FeishuUncertainError):
                            image["status"] = "pending"
                            _save_journal(journal_path, state)
                        errors.append(exc)
                    else:
                        image["token"] = token
                        image["status"] = "uploaded"
                        _save_journal(journal_path, state)
                        if not stop_patching:
                            try:
                                patch(image)
                            except Exception as exc:
                                errors.append(exc)
                                stop_patching = True
                    if not stop_patching and not errors:
                        start_next()
        finally:
            for worker in clients:
                try:
                    worker.close()
                except Exception as exc:
                    errors.append(exc)
    if errors:
        raise errors[0]
    if _verify_snapshot(client, doc_id, state):
        _save_journal(journal_path, state)
    if any(image["status"] != "confirmed" for image in state["images"]):
        raise ValueError("not all image attachments were verified")


def replace_body(client, doc_id, md, figures=(), *, resume_path=None, client_factory=None,
                 max_workers=3, previous_publication_complete=False):
    """Rebuild a note, optionally checkpointing a verified concurrent image upload."""
    if type(previous_publication_complete) is not bool:
        raise ValueError("previous_publication_complete must be a boolean")
    if resume_path is None and client_factory is None:
        if max_workers != 3 or previous_publication_complete:
            raise ValueError("new publishing options require resume_path and client_factory")
        figures = list(figures)
        _figure_inputs(doc_id, md, figures)
        client.clear_body(doc_id)
        digest, body = _split_note(md)
        _append_digest_callout(client, doc_id, digest)
        _append_body(client, doc_id, body, figures)
        return doc_id
    if resume_path is None or not callable(client_factory):
        raise ValueError("resume_path and client_factory are both required")
    if type(max_workers) is not int or not 1 <= max_workers <= _MAX_IMAGE_WORKERS:
        raise ValueError("max_workers must be an integer from 1 to 32")
    path = os.fspath(resume_path)
    if not isinstance(path, str) or not os.path.isabs(path):
        raise ValueError("resume_path must be absolute")
    identity, contents = _figure_inputs(doc_id, md, figures)
    with _journal_lock(path):
        if os.path.exists(path):
            state = _load_journal(path)
            if state["identity"] != identity:
                if not previous_publication_complete:
                    raise ValueError("image checkpoint does not match document, note or figures")
                if (state["identity"]["doc_id"] != doc_id
                        or any(image["status"] != "confirmed" for image in state["images"])):
                    raise ValueError("previous document or publication is not fully confirmed")
                _verify_snapshot(client, doc_id, state)
                state = _build_skeleton(client, doc_id, md, identity, contents, path)
            else:
                if _verify_snapshot(client, doc_id, state):
                    _save_journal(path, state)
                if any(image["status"] == "uploading" for image in state["images"]):
                    raise ValueError("an image upload outcome is uncertain; preserve the checkpoint, "
                                     "inspect the image blocks in Feishu and recover manually; "
                                     "do not delete the checkpoint or rebuild the document")
        else:
            state = _build_skeleton(client, doc_id, md, identity, contents, path)
        _upload_images(client, doc_id, contents, state, path, client_factory, max_workers)
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
    citekey = os.path.splitext(os.path.basename(note_path))[0]
    root = figures_root or os.path.join(os.path.dirname(os.path.abspath(note_path)), "figs")
    manifest = os.path.join(root, citekey, "manifest.json")
    try:
        with open(manifest, encoding="utf-8") as f:
            data = json.load(f, object_pairs_hook=_unique_object)
    except FileNotFoundError:
        return []
    except OSError:
        raise ValueError("figure manifest is unreadable") from None
    if (not isinstance(data, dict) or not isinstance(data.get("figures"), list)
            or ("citekey" in data and data["citekey"] != citekey)):
        raise ValueError("invalid figure manifest")
    captions = {}
    try:
        with open(note_path, encoding="utf-8") as f:
            for line in f:
                m = _FIG_CAPTION.match(line.strip())
                if m:
                    captions[m.group(1)] = m.group(2).strip()
    except OSError:
        raise ValueError("note is missing or unreadable") from None
    out, nums = [], set()
    for fig in data["figures"]:
        if not isinstance(fig, dict):
            raise ValueError("invalid figure manifest entry")
        label = fig.get("label")
        num = _figure_number(label, nums)
        # A stale path can be relocated when a clone keeps the same crop name.
        path = fig.get("local_path")
        if path is not None and not isinstance(path, str):
            raise ValueError("invalid figure manifest path")
        if path:
            try:
                os.stat(path)
            except FileNotFoundError:
                path = ""
            except OSError:
                raise ValueError(f"figure {label!r} is missing or unreadable") from None
        if not path:
            path = os.path.join(root, citekey, label + ".png")
        page = fig.get("page", "?")
        display = f"Fig. {num}"
        text = captions.get(num)
        if text:
            caption = f"{display}　{text}（原 PDF 第 {page} 页）"
        else:
            caption = f"{display}（原 PDF 第 {page} 页）"
        out.append((path, caption, label))
    validate_figure_inputs("preflight", "", out)
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

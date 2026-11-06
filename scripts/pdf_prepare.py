"""Extract a PDF once, then inspect its text, layout and figure candidates locally.

Examples::

    python scripts/pdf_prepare.py paper.pdf
    python scripts/pdf_prepare.py paper.pdf --pages 1,3-5 --search ablation

Coordinates are unrotated PDF points relative to the top-left of the page's
visible crop box. Captions and headings are candidates, not verified structure;
their source text is never truncated. No OCR or changes to the source PDF occur.
"""

import argparse
import hashlib
import json
import math
import os
from pathlib import Path
import re
import sys
import tempfile

try:  # PyMuPDF >= 1.24 exposes the same API as `pymupdf`
    import pymupdf as fitz
except ImportError:  # older releases only ship the `fitz` alias
    import fitz


SCHEMA_VERSION = 1
EXTRACTOR_VERSION = "1.0.0"
DEFAULT_CACHE_DIR = Path(__file__).resolve().parent.parent / "notes" / ".pdf-cache"
CAPTION_RE = re.compile(
    r"^\s*(Figure|Fig\.?|Scheme|Table|图|表)\s*"
    r"([A-Za-z]?\d+(?:\.\d+)*(?:[a-z])?|[IVXLCDM]+)"
    r"(?=\s|[.:：()\-–—]|$)", re.IGNORECASE,
)
SECTION_RE = re.compile(
    r"^(?:\d+(?:\.\d+)*\.?\s+\S|[A-Z]\.\s+\S|"
    r"abstract\b|introduction\b|related work\b|background\b|"
    r"methods?\b|materials and methods\b|results\b|discussion\b|"
    r"experiments?\b|conclusions?\b|references\b|appendix\b|"
    r"supplementary\b|acknowledg(?:e)?ments?\b)", re.IGNORECASE,
)


def _digest(data):
    return hashlib.sha256(data).hexdigest()


def pdf_sha256(path):
    """Hash file bytes without opening or parsing the PDF."""
    hasher = hashlib.sha256()
    with open(path, "rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            hasher.update(chunk)
    return hasher.hexdigest()


def _integrity(manifest):
    # These fields describe this invocation, not the cached extraction.
    body = {k: v for k, v in manifest.items()
            if k not in ("manifest_sha256", "source_path", "cache")}
    return _digest(json.dumps(body, ensure_ascii=False, sort_keys=True,
                              separators=(",", ":"), allow_nan=False).encode("utf-8"))


def _bbox(values):
    result = [float(value) for value in values]
    if len(result) != 4 or not all(math.isfinite(value) for value in result):
        raise ValueError("PDF contains a non-finite or invalid bounding box")
    return result


def _union(boxes):
    return [min(b[0] for b in boxes), min(b[1] for b in boxes),
            max(b[2] for b in boxes), max(b[3] for b in boxes)]


def _section_candidate(text):
    return len(text) <= 160 and bool(SECTION_RE.match(text.strip()))


def _text_blocks(page):
    flags = fitz.TEXTFLAGS_DICT & ~fitz.TEXT_PRESERVE_IMAGES
    raw = page.get_text("dict", flags=flags)
    blocks = []
    for index, block in enumerate(raw["blocks"]):
        if block.get("type") != 0:
            continue
        lines = []
        for line in block.get("lines", []):
            spans = line.get("spans", [])
            text = "".join(span["text"] for span in spans)
            lines.append({"text": text, "bbox": _bbox(line["bbox"]),
                          "font_size": max((float(s["size"]) for s in spans), default=0)})
        blocks.append({"block_id": index, "bbox": _bbox(block["bbox"]),
                       "text": "\n".join(line["text"] for line in lines), "lines": lines})
    return blocks


def _captions(blocks, page_number, digest):
    """Retain complete source lines, with conservative nearby-block continuation.

    The boundary is heuristic: source block IDs and boxes remain available for
    visual checking, including cases where a caption continues on another page.
    """
    result = []
    for block_index, block in enumerate(blocks):
        for line_index, line in enumerate(block["lines"]):
            match = CAPTION_RE.match(line["text"])
            if not match:
                continue
            selected = [line]
            block_ids = [block["block_id"]]
            for following in block["lines"][line_index + 1:]:
                if CAPTION_RE.match(following["text"]) or _section_candidate(following["text"]):
                    break
                selected.append(following)
            else:
                # Join only adjacent blocks with matching size, alignment and gap.
                # Never cross another caption or a likely section heading.
                for following_block in blocks[block_index + 1:]:
                    lines = following_block["lines"]
                    if not lines:
                        break
                    first = lines[0]
                    gap = first["bbox"][1] - selected[-1]["bbox"][3]
                    font_size = selected[0]["font_size"]
                    if (CAPTION_RE.match(first["text"]) or _section_candidate(first["text"])
                            or not -1 <= gap <= max(4, font_size)
                            or abs(first["font_size"] - font_size) > 0.5
                            or abs(first["bbox"][0] - selected[0]["bbox"][0]) > max(4, font_size)):
                        break
                    block_ids.append(following_block["block_id"])
                    for following in lines:
                        if CAPTION_RE.match(following["text"]) or _section_candidate(following["text"]):
                            break
                        selected.append(following)
                    else:
                        continue
                    break
            kind_label = match.group(1).lower().rstrip(".")
            kind = "table" if kind_label in ("table", "表") else (
                "scheme" if kind_label == "scheme" else "figure")
            result.append({
                "caption_id": f"p{page_number}-b{block['block_id']}-l{line_index}",
                "page": page_number, "pdf_sha256": digest, "kind": kind,
                "number": match.group(2),
                "figure_number": match.group(2) if kind != "table" else None,
                "label": match.group(0).strip(),
                "text": "\n".join(part["text"] for part in selected),
                "bbox": _union([part["bbox"] for part in selected]),
                "block_ids": block_ids, "boundary": "heuristic", "candidate": True,
            })
    return result


def _extract(pdf_bytes, digest):
    pages = []
    with fitz.open(stream=pdf_bytes, filetype="pdf") as document:
        if document.needs_pass:
            raise ValueError("PDF is password-protected; supply an unlocked readable copy")
        if not document.page_count:
            raise ValueError("PDF has no pages")
        for number, page in enumerate(document, start=1):
            blocks = _text_blocks(page)
            text = "\n\n".join(block["text"] for block in blocks)
            images = [{"bbox": _bbox(info["bbox"]), "width": info["width"],
                       "height": info["height"]} for info in page.get_image_info()]
            sections = []
            for block in blocks:
                for line in block["lines"]:
                    if _section_candidate(line["text"]):
                        sections.append({"title": line["text"], "bbox": line["bbox"],
                                         "block_id": block["block_id"], "page": number,
                                         "pdf_sha256": digest, "candidate": True})
            pages.append({"page": number, "width": float(page.cropbox.width),
                          "height": float(page.cropbox.height), "rotation": page.rotation,
                          "text": text, "text_layer_status": "present" if text.strip() else "absent",
                          "blocks": blocks, "images": images,
                          "captions": _captions(blocks, number, digest), "sections": sections})
    text_pages = sum(page["text_layer_status"] == "present" for page in pages)
    manifest = {"schema_version": SCHEMA_VERSION, "extractor_version": EXTRACTOR_VERSION,
                "pymupdf_version": fitz.VersionBind, "pdf_sha256": digest,
                "coordinate_space": "unrotated_page_points", "page_count": len(pages),
                "text_layer_status": ("present" if text_pages == len(pages) else
                                      "partial" if text_pages else "absent"), "pages": pages}
    manifest["manifest_sha256"] = _integrity(manifest)
    return manifest


def _valid_cache(manifest, digest):
    try:
        return (isinstance(manifest, dict)
                and manifest["schema_version"] == SCHEMA_VERSION
                and manifest["extractor_version"] == EXTRACTOR_VERSION
                and manifest["pymupdf_version"] == fitz.VersionBind
                and manifest["pdf_sha256"] == digest
                and manifest["manifest_sha256"] == _integrity(manifest)
                and isinstance(manifest["pages"], list)
                and manifest["page_count"] == len(manifest["pages"]) > 0
                and [page["page"] for page in manifest["pages"]]
                == list(range(1, manifest["page_count"] + 1)))
    except (KeyError, TypeError, ValueError):
        return False


def prepare_pdf(path, cache_dir=None, rebuild=False):
    """Return a versioned manifest; a cache hit never invokes the PDF parser."""
    source = Path(path).expanduser().resolve(strict=True)
    pdf_bytes = source.read_bytes()
    digest = _digest(pdf_bytes)
    directory = Path(cache_dir) if cache_dir is not None else DEFAULT_CACHE_DIR
    safe_version = re.sub(r"[^A-Za-z0-9_.-]", "_", fitz.VersionBind)
    cache_path = directory / f"{digest}-{EXTRACTOR_VERSION}-{safe_version}.json"
    manifest = None
    if not rebuild:
        try:
            cached = json.loads(cache_path.read_text(encoding="utf-8"))
            if _valid_cache(cached, digest):
                manifest = cached
        except (OSError, ValueError, UnicodeError):
            pass
    hit = manifest is not None
    if manifest is None:
        manifest = _extract(pdf_bytes, digest)
        directory.mkdir(parents=True, exist_ok=True)
        temp_path = None
        try:
            with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=directory,
                                             prefix=".pdf-", suffix=".tmp", delete=False) as stream:
                temp_path = Path(stream.name)
                json.dump(manifest, stream, ensure_ascii=False, allow_nan=False)
                stream.write("\n")
            os.replace(temp_path, cache_path)
        finally:
            if temp_path is not None and temp_path.exists():
                temp_path.unlink()
    manifest["source_path"] = str(source)
    manifest["cache"] = {"path": str(cache_path.resolve()), "hit": hit}
    return manifest


def parse_page_range(spec, page_count):
    """Parse one-based page numbers/ranges, reject invalid or reversed ranges."""
    if spec is None:
        return list(range(1, page_count + 1))
    if isinstance(spec, str):
        numbers = set()
        for token in spec.split(","):
            match = re.fullmatch(r"\s*(\d+)\s*(?:-\s*(\d+)\s*)?", token)
            if not match:
                raise ValueError("pages must be numbers/ranges such as 1,3-5")
            start = int(match.group(1))
            end = int(match.group(2) or start)
            if not 1 <= start <= end <= page_count:
                raise ValueError(f"page range {token!r} is outside 1..{page_count} or reversed")
            numbers.update(range(start, end + 1))
    else:
        try:
            raw = list(spec)
        except TypeError as exc:
            raise ValueError("pages must be a range string or list of page numbers") from exc
        if any(type(n) is not int or not 1 <= n <= page_count for n in raw):
            raise ValueError(f"pages must be integers in 1..{page_count}")
        numbers = set(raw)
    if not numbers:
        raise ValueError("page selection is empty")
    return sorted(numbers)


def select_pages(manifest, pages=None):
    """Select cached page objects; does not parse the PDF."""
    wanted = set(parse_page_range(pages, manifest["page_count"]))
    return [page for page in manifest["pages"] if page["page"] in wanted]


def search_manifest(manifest, terms, pages=None):
    """Return full matching text blocks and provenance (case-insensitive OR)."""
    if isinstance(terms, str):
        terms = [terms]
    terms = list(terms)
    if not terms or any(not isinstance(term, str) or not term.strip() for term in terms):
        raise ValueError("search needs at least one non-empty keyword")
    results = []
    for page in select_pages(manifest, pages):
        for block in page["blocks"]:
            # Join whitespace so searches can cross PDF line breaks.
            normalized = " ".join(block["text"].split()).casefold()
            matches = [term for term in terms if " ".join(term.split()).casefold() in normalized]
            if matches:
                results.append({"page": page["page"], "pdf_sha256": manifest["pdf_sha256"],
                                "block_id": block["block_id"], "bbox": block["bbox"],
                                "text": block["text"], "matched_terms": matches})
    return results


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("pdf")
    parser.add_argument("--cache-dir")
    parser.add_argument("--pages", help="one-based page numbers/ranges, e.g. 1,3-5")
    parser.add_argument("--search", action="append", help="repeat for case-insensitive OR search")
    parser.add_argument("--json", action="store_true", help="JSON is the default output")
    parser.add_argument("--rebuild", action="store_true", help="ignore and replace the extraction cache")
    args = parser.parse_args(argv)
    try:
        manifest = prepare_pdf(args.pdf, args.cache_dir, args.rebuild)
        result = dict(manifest)
        if args.search:
            result.pop("pages")
            result["matches"] = search_manifest(manifest, args.search, args.pages)
        elif args.pages is not None:
            result["pages"] = select_pages(manifest, args.pages)
        print(json.dumps(result, ensure_ascii=False, indent=2, allow_nan=False))
    except (OSError, ValueError, RuntimeError) as exc:
        parser.exit(2, f"error: {exc}\n")


if __name__ == "__main__":
    main()

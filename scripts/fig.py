"""Inspect and crop original PDF figures without redrawing or annotations.

    python scripts/fig.py scan paper.pdf [--page N] [--json]
    python scripts/fig.py page paper.pdf --page N --out p.png [--dpi 200]
    python scripts/fig.py crop paper.pdf --page N --bbox x0,y0,x1,y1 --out f.png
    python scripts/fig.py batch paper.pdf --manifest crops.json --out-dir figures

Batch input: {"pdf_sha256": "...", "crops": [{"page": 1, "figure_number": "2",
"bbox": [30, 50, 500, 350], "out": "fig2.png", "dpi": 220, "pad": 0}]}.
Page numbers start at one. Bounding boxes use unrotated PDF points, top-left
origin, as reported by scan. Every batch output must remain inside out-dir
(default: the JSON manifest's directory). Existing files require --overwrite.
"""

import argparse
import hashlib
import json
import math
import os
from pathlib import Path
import tempfile

import fitz

try:
    from .pdf_prepare import prepare_pdf, select_pages
except ImportError:
    from pdf_prepare import prepare_pdf, select_pages


MAX_DPI = 1200
OUTPUT_SUFFIXES = {".png", ".jpg", ".jpeg", ".ppm", ".pnm", ".pam", ".pbm", ".pgm", ".ps"}


def scan(path, only_page=None, json_output=False, pages=None, cache_dir=None, rebuild=False):
    if only_page is not None and pages is not None:
        raise ValueError("use either --page or --pages, not both")
    manifest = prepare_pdf(path, cache_dir, rebuild)
    selected = select_pages(manifest, [only_page] if only_page is not None else pages)
    result = dict(manifest)
    result["pages"] = selected
    if json_output:
        print(json.dumps(result, ensure_ascii=False, indent=2, allow_nan=False))
    else:
        print(f"pages: {manifest['page_count']}")
        print(f"text layer: {manifest['text_layer_status']} (no OCR)")
        for page in selected:
            images = [image for image in page["images"] if image["bbox"][2] - image["bbox"][0] > 80]
            if not images and not page["captions"]:
                continue
            print(f"\n-- page {page['page']}")
            for item in images:
                x0, y0, x1, y1 = item["bbox"]
                print(f"   img  bbox={x0:.0f},{y0:.0f},{x1:.0f},{y1:.0f}  {x1-x0:.0f}x{y1-y0:.0f}pt")
            for caption in page["captions"]:
                bbox = ",".join(f"{value:.0f}" for value in caption["bbox"])
                text = " ".join(caption["text"].split())
                print(f"   cap  bbox={bbox}  {text}")
    return result


def _validate_geometry(document, pno, dpi, bbox=None, pad=0):
    if type(pno) is not int or not 1 <= pno <= document.page_count:
        raise ValueError(f"page must be an integer in 1..{document.page_count}")
    if type(dpi) is not int or not 1 <= dpi <= MAX_DPI:
        raise ValueError(f"dpi must be an integer in 1..{MAX_DPI}")
    if isinstance(pad, bool) or not isinstance(pad, (int, float)) or not math.isfinite(pad) or pad < 0:
        raise ValueError("pad must be a finite non-negative number")
    page = document[pno - 1]
    bounds = fitz.Rect(0, 0, page.cropbox.width, page.cropbox.height)
    clip = None
    effective_bbox = list(bounds)
    if bbox is not None:
        if (not isinstance(bbox, (list, tuple)) or len(bbox) != 4
                or any(isinstance(v, bool) or not isinstance(v, (int, float))
                       or not math.isfinite(v) for v in bbox)):
            raise ValueError("bbox needs four finite numbers: x0,y0,x1,y1")
        x0, y0, x1, y1 = bbox
        if not (0 <= x0 < x1 <= bounds.width and 0 <= y0 < y1 <= bounds.height):
            raise ValueError(f"bbox must have positive area within page bounds {list(bounds)}")
        # Padding clips at the page edge; the source bbox must be in bounds.
        effective = fitz.Rect(x0-pad, y0-pad, x1+pad, y1+pad) & bounds
        effective_bbox = list(effective)
        clip = effective * page.rotation_matrix
    return page, clip, effective_bbox


def _validate_output(out, source, overwrite=False, root=None):
    target = Path(out).expanduser().resolve()
    if root is not None and not target.is_relative_to(root):
        raise ValueError(f"output escapes --out-dir: {out}")
    if target == source:
        raise ValueError("output must not replace the source PDF")
    if target.suffix.lower() not in OUTPUT_SUFFIXES:
        raise ValueError("output must use a supported image suffix, such as .png")
    if target.exists() and (not target.is_file() or not overwrite):
        raise ValueError(f"output already exists: {target}; use --overwrite to replace an image")
    for parent in target.parents:
        if parent.exists() and not parent.is_dir():
            raise ValueError(f"output parent is not a directory: {parent}")
    return target


def _stage_image(pixmap, target):
    target.parent.mkdir(parents=True, exist_ok=True)
    descriptor, name = tempfile.mkstemp(prefix=".crop-", suffix=target.suffix, dir=target.parent)
    os.close(descriptor)
    staged = Path(name)
    try:
        pixmap.save(staged)
    except Exception:
        staged.unlink(missing_ok=True)
        raise
    return staged


def render(path, pno, out, dpi, bbox=None, pad=0, overwrite=False):
    """Render a page/crop, preserving the original positional Python interface."""
    source = Path(path).expanduser().resolve(strict=True)
    target = _validate_output(out, source, overwrite)
    pdf_bytes = source.read_bytes()
    with fitz.open(stream=pdf_bytes, filetype="pdf") as document:
        if document.needs_pass:
            raise ValueError("PDF is password-protected")
        page, clip, effective = _validate_geometry(document, pno, dpi, bbox, pad)
        pixmap = page.get_pixmap(dpi=dpi, clip=clip)
        staged = _stage_image(pixmap, target)
        try:
            _validate_output(target, source, overwrite)
            os.replace(staged, target)
        finally:
            staged.unlink(missing_ok=True)
        result = {"page": pno, "bbox": effective, "out": str(target), "dpi": dpi,
                  "width": pixmap.width, "height": pixmap.height,
                  "pdf_sha256": hashlib.sha256(pdf_bytes).hexdigest(),
                  "image_sha256": hashlib.sha256(target.read_bytes()).hexdigest()}
    print(f"saved {out}  {result['width']}x{result['height']}px  {dpi}dpi")
    return result


def batch_crop(path, manifest, out_dir=None, overwrite=False):
    """Validate all crops, stage all images, then publish batch outputs.

    PDF hash checks prevent applying coordinates to another PDF/version. Figure
    numbers are supplied by the reader, never inferred from output order.
    """
    if isinstance(manifest, (str, os.PathLike)):
        manifest_path = Path(manifest).expanduser().resolve(strict=True)
        plan = json.loads(manifest_path.read_text(encoding="utf-8-sig"))
        default_root = manifest_path.parent
    else:
        plan = manifest
        default_root = Path.cwd()
    if not isinstance(plan, dict) or not isinstance(plan.get("crops"), list) or not plan["crops"]:
        raise ValueError("manifest must contain a non-empty crops list")
    source = Path(path).expanduser().resolve(strict=True)
    pdf_bytes = source.read_bytes()
    digest = hashlib.sha256(pdf_bytes).hexdigest()
    if plan.get("pdf_sha256") != digest:
        raise ValueError("manifest pdf_sha256 is missing or does not match this PDF")
    root = Path(out_dir).expanduser().resolve() if out_dir is not None else default_root.resolve()
    prepared = []
    destinations = set()
    staged_files = []
    results = []
    with fitz.open(stream=pdf_bytes, filetype="pdf") as document:
        if document.needs_pass:
            raise ValueError("PDF is password-protected")
        for index, entry in enumerate(plan["crops"], start=1):
            if not isinstance(entry, dict):
                raise ValueError(f"crop {index} must be an object")
            if ("figure_number" not in entry or isinstance(entry["figure_number"], bool)
                    or not isinstance(entry["figure_number"], (str, int))
                    or not str(entry["figure_number"]).strip()):
                raise ValueError(f"crop {index} needs the paper's figure_number")
            if "bbox" not in entry:
                raise ValueError(f"crop {index} needs bbox")
            relative = entry.get("out")
            if not isinstance(relative, str) or not relative.strip() or Path(relative).is_absolute():
                raise ValueError(f"crop {index} out must be a relative image path inside --out-dir")
            target = _validate_output(root / relative, source, overwrite, root)
            key = os.path.normcase(str(target))
            if key in destinations:
                raise ValueError(f"duplicate batch output: {target}")
            destinations.add(key)
            dpi = entry.get("dpi", 220)
            pad = entry.get("pad", 0)
            _, clip, effective = _validate_geometry(document, entry.get("page"), dpi, entry["bbox"], pad)
            prepared.append((entry, target, dpi, clip, effective))

        try:
            for entry, target, dpi, clip, effective in prepared:
                page = document[entry["page"] - 1]
                pixmap = page.get_pixmap(dpi=dpi, clip=clip)
                staged = _stage_image(pixmap, target)
                staged_files.append((staged, target))
                results.append({"page": entry["page"], "figure_number": str(entry["figure_number"]),
                                "pdf_sha256": digest, "bbox": list(entry["bbox"]),
                                "effective_bbox": effective, "out": str(target), "dpi": dpi,
                                "width": pixmap.width, "height": pixmap.height,
                                "image_sha256": hashlib.sha256(staged.read_bytes()).hexdigest()})
            for _, target in staged_files:
                _validate_output(target, source, overwrite, root)
            for staged, target in staged_files:
                os.replace(staged, target)
        finally:
            for staged, _ in staged_files:
                staged.unlink(missing_ok=True)
    return {"pdf_sha256": digest, "source_path": str(source),
            "coordinate_space": "unrotated_page_points", "crops": results}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="cmd", required=True)
    scan_parser = sub.add_parser("scan")
    scan_parser.add_argument("pdf")
    group = scan_parser.add_mutually_exclusive_group()
    group.add_argument("--page", type=int)
    group.add_argument("--pages")
    scan_parser.add_argument("--json", action="store_true")
    scan_parser.add_argument("--cache-dir")
    scan_parser.add_argument("--rebuild", action="store_true")
    for name in ("page", "crop"):
        command = sub.add_parser(name)
        command.add_argument("pdf")
        command.add_argument("--page", type=int, required=True)
        command.add_argument("--out", required=True)
        command.add_argument("--dpi", type=int, default=200)
        command.add_argument("--overwrite", action="store_true")
        if name == "crop":
            command.add_argument("--bbox", required=True)
            command.add_argument("--pad", type=float, default=6)
    batch_parser = sub.add_parser("batch")
    batch_parser.add_argument("pdf")
    batch_parser.add_argument("--manifest", required=True)
    batch_parser.add_argument("--out-dir")
    batch_parser.add_argument("--overwrite", action="store_true")
    args = parser.parse_args(argv)
    try:
        if args.cmd == "scan":
            scan(args.pdf, args.page, args.json, args.pages, args.cache_dir, args.rebuild)
        elif args.cmd == "page":
            render(args.pdf, args.page, args.out, args.dpi, overwrite=args.overwrite)
        elif args.cmd == "crop":
            bbox = tuple(float(value) for value in args.bbox.split(","))
            render(args.pdf, args.page, args.out, args.dpi, bbox, args.pad, args.overwrite)
        else:
            result = batch_crop(args.pdf, args.manifest, args.out_dir, args.overwrite)
            print(json.dumps(result, ensure_ascii=False, indent=2, allow_nan=False))
    except (OSError, ValueError, RuntimeError) as exc:
        parser.exit(2, f"error: {exc}\n")


if __name__ == "__main__":
    main()

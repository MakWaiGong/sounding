"""Crop PDF figures and chain them through Notion upload into ready-to-paste image tags.

    # Step 1: crop figures (one command, no whole-page renders)
    python scripts/fig_upload.py crop paper.pdf --citekey dc73fc6cd9 \
        --fig "3:35,130,565,750:fig1" \
        --fig "7:35,45,565,710:fig3" \
        --fig "9:35,45,565,710:fig4"

    # Step 2: upload each cropped image via Notion MCP (create_file_upload + curl POST),
    #          then feed the returned markdown_source values back:
    python scripts/fig_upload.py tag --citekey dc73fc6cd9 \
        --source fig1=file-upload://uuid-aaa \
        --source fig3=file-upload://uuid-bbb \
        --source fig4=file-upload://uuid-ccc

    # tag prints <image source="file-upload://uuid-aaa" /> lines ready to paste.
    # It FAILS if any cropped figure has no uploaded source — you cannot skip upload.
"""

import argparse
import json
import sys
from pathlib import Path

import fitz

SKILL_DIR = Path(__file__).resolve().parent.parent
NOTES_DIR = SKILL_DIR / "notes"


def _manifest_path(citekey: str) -> Path:
    return NOTES_DIR / "figs" / citekey / "manifest.json"


def crop(pdf: str, citekey: str, figs: list[str], dpi: int = 200, overwrite: bool = False):
    out_dir = NOTES_DIR / "figs" / citekey
    out_dir.mkdir(parents=True, exist_ok=True)
    doc = fitz.open(pdf)
    entries = []
    for spec in figs:
        page_str, bbox_str, label = spec.split(":", 2)
        page = int(page_str)
        bbox = [float(v) for v in bbox_str.split(",")]
        if len(bbox) != 4:
            raise ValueError(f"bad bbox in {spec!r}: need x0,y0,x1,y1")
        x0, y0, x1, y1 = bbox
        page_obj = doc[page - 1]
        clip = fitz.Rect(x0, y0, x1, y1) * page_obj.rotation_matrix
        pix = page_obj.get_pixmap(dpi=dpi, clip=clip)
        out_path = out_dir / f"{label}.png"
        if out_path.exists() and not overwrite:
            raise FileExistsError(f"{out_path} exists; use --overwrite")
        pix.save(out_path)
        entries.append({
            "label": label,
            "page": page,
            "bbox": bbox,
            "local_path": str(out_path),
            "width": pix.width,
            "height": pix.height,
            "notion_source": None,
        })
        print(f"  cropped {label}: {out_path.name}  {pix.width}x{pix.height}px")
    manifest = {"citekey": citekey, "figures": entries}
    mp = _manifest_path(citekey)
    mp.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\nmanifest: {mp}")
    print(f"next: upload each local_path via Notion MCP, then run:")
    print(f"  python scripts/fig_upload.py tag --citekey {citekey} --source <label>=file-upload://<uuid> ...")


def tag(citekey: str, sources: list[str]):
    mp = _manifest_path(citekey)
    if not mp.exists():
        sys.exit(f"manifest not found: {mp}; run crop first")
    manifest = json.loads(mp.read_text(encoding="utf-8"))
    source_map = {}
    for s in sources:
        label, uri = s.split("=", 1)
        source_map[label.strip()] = uri.strip()
    missing = []
    for fig in manifest["figures"]:
        label = fig["label"]
        if label in source_map:
            fig["notion_source"] = source_map[label]
        if not fig.get("notion_source"):
            missing.append(label)
    if missing:
        sys.exit(f"ERROR: {len(missing)} figure(s) not uploaded: {', '.join(missing)}\n"
                 f"You must upload each cropped image via Notion MCP before tagging.")
    mp.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"<!-- paste these image tags at the right anchors in your Notion page -->")
    for fig in manifest["figures"]:
        print(f'<image source="{fig["notion_source"]}" />')


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)

    c = sub.add_parser("crop")
    c.add_argument("pdf")
    c.add_argument("--citekey", required=True)
    c.add_argument("--fig", action="append", required=True,
                   help="page:bbox_x0,y0,x1,y1:label  e.g. 3:35,130,565,750:fig1")
    c.add_argument("--dpi", type=int, default=200)
    c.add_argument("--overwrite", action="store_true")

    t = sub.add_parser("tag")
    t.add_argument("--citekey", required=True)
    t.add_argument("--source", action="append", default=[],
                   help="label=file-upload://uuid  e.g. fig1=file-upload://abc-123")

    args = ap.parse_args()
    if args.cmd == "crop":
        crop(args.pdf, args.citekey, args.fig, args.dpi, args.overwrite)
    else:
        tag(args.citekey, args.source)


if __name__ == "__main__":
    main()

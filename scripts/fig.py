"""Extract figure regions from a paper PDF.

Usage:
  python tools/fig.py scan  <pdf> [--page N]        # page count + image/caption blocks
  python tools/fig.py page  <pdf> --page N --out p.png [--dpi 200]
  python tools/fig.py crop  <pdf> --page N --bbox x0,y0,x1,y1 --out f.png [--dpi 220] [--pad 6]

bbox is in PDF points, origin top-left, as reported by `scan`.
"""
import argparse
import sys

import fitz


def scan(path, only_page=None):
    doc = fitz.open(path)
    print(f"pages: {doc.page_count}")
    for pno in range(doc.page_count):
        if only_page is not None and pno + 1 != only_page:
            continue
        page = doc[pno]
        imgs = [i for i in page.get_image_info() if i["bbox"][2] - i["bbox"][0] > 80]
        caps = []
        for b in page.get_text("blocks"):
            text = b[4].strip().replace("\n", " ")[:90]
            if text.lower().startswith(("figure", "fig.", "scheme", "table")):
                caps.append((round(b[0]), round(b[1]), round(b[2]), round(b[3]), text))
        if not imgs and not caps:
            continue
        print(f"\n-- page {pno + 1}")
        for i in imgs:
            x0, y0, x1, y1 = i["bbox"]
            w, h = x1 - x0, y1 - y0
            print(f"   img  bbox={x0:.0f},{y0:.0f},{x1:.0f},{y1:.0f}  {w:.0f}x{h:.0f}pt")
        for c in caps:
            print(f"   cap  bbox={c[0]},{c[1]},{c[2]},{c[3]}  {c[4]}")


def render(path, pno, out, dpi, bbox=None, pad=0):
    doc = fitz.open(path)
    page = doc[pno - 1]
    clip = None
    if bbox:
        x0, y0, x1, y1 = bbox
        clip = fitz.Rect(x0 - pad, y0 - pad, x1 + pad, y1 + pad) & page.rect
    pix = page.get_pixmap(dpi=dpi, clip=clip)
    pix.save(out)
    print(f"saved {out}  {pix.width}x{pix.height}px  {dpi}dpi")


def main():
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("scan")
    s.add_argument("pdf")
    s.add_argument("--page", type=int)

    for name in ("page", "crop"):
        p = sub.add_parser(name)
        p.add_argument("pdf")
        p.add_argument("--page", type=int, required=True)
        p.add_argument("--out", required=True)
        p.add_argument("--dpi", type=int, default=200)
        if name == "crop":
            p.add_argument("--bbox", required=True)
            p.add_argument("--pad", type=float, default=6)

    a = ap.parse_args()
    if a.cmd == "scan":
        scan(a.pdf, a.page)
    elif a.cmd == "page":
        render(a.pdf, a.page, a.out, a.dpi)
    else:
        bbox = tuple(float(v) for v in a.bbox.split(","))
        if len(bbox) != 4:
            sys.exit("--bbox needs x0,y0,x1,y1")
        render(a.pdf, a.page, a.out, a.dpi, bbox, a.pad)


if __name__ == "__main__":
    main()

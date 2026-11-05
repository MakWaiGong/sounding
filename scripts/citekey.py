"""Derive a machine citekey: identity, not meaning.

    key = sha1(seed)[:10]
    seed = "doi:" + norm(doi)                     when a DOI exists
           "title:" + norm(title) | norm(surname) | year    otherwise

Why a hash instead of the raw DOI or Zotero's item key: DOI is the preferred
bibliographic identity, but contains '/' and may contain characters that are
unsafe in filenames, BibTeX keys, and command arguments. The Zotero library is a
backup that can be rebuilt. A hash recomputed from DOI or title joins the rebuilt
item to the same Notion row; an item key would not, because Zotero mints a new one
on import.

The key is deliberately unreadable. What you read is the title; the key only has
to be unique, stable, and computable by both sides.

    python citekey.py --doi 10.1038/s41586-021-03819-2
    python citekey.py --title "ProAR: Probabilistic Autoregressive Modeling for Molecular Dynamics" \
        --author Cheng --year 2026
"""
import argparse
import hashlib
import json
import re
import sys

from paper_identity import normalize_doi


def norm(s):
    return "".join(ch for ch in str(s or "").lower() if ch.isalnum())


def surname(authors):
    """Last token of the first author, as Zotero stores 'First Last'."""
    first = (authors or "").split(";")[0].strip()
    if not first:
        return ""
    return first.rsplit(" ", 1)[-1]


def derive(doi=None, title=None, author=None, year=None):
    """author may be 'First Last' or a bare surname; only the surname enters the seed."""
    if doi:
        canonical = normalize_doi(doi)
        if not canonical:
            raise ValueError("invalid DOI; use a bare DOI or a doi.org URL")
        # Preserve the seed of existing bare-DOI keys; URLs are aliases of it.
        seed = "doi:" + norm(canonical)
    elif title and norm(title) and author and norm(surname(author)) and year:
        seed = "title:%s|%s|%s" % (norm(title), norm(surname(author)), norm(str(year or "")))
    else:
        raise ValueError("need a DOI, or a title with author and year")
    return hashlib.sha1(seed.encode("utf-8")).hexdigest()[:10]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--doi")
    ap.add_argument("--title")
    ap.add_argument("--author", help="first author, 'First Last'")
    ap.add_argument("--year")
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args()
    try:
        key = derive(a.doi, a.title, a.author, a.year)
    except ValueError as e:
        sys.exit(str(e))
    if a.json:
        print(json.dumps({"citekey": key, "seed": "doi" if norm(a.doi) else "title"}))
    else:
        print(key)


if __name__ == "__main__":
    main()

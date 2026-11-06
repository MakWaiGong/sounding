# Controlled vocabulary

One family, one tag per concept. Rules:

- lowercase English, hyphens inside a phrase, no plurals, no synonyms.
- Reuse before creating. If a near-match exists, use it and note the intended nuance in the note body.
- Keep the family under ~40 options. When it grows past that, a tag is too fine-grained and belongs in the note text.
- The same tag strings go to the cloud library and to Zotero, so both sides can be browsed the same way.
- On the cloud side the family is a multi-select field. On the Zotero side tags are flat, so the family is carried by a prefix (`d:`), taken from `config.local.json` → `zotero_tag_prefix`.
- 领域 is not part of this vocabulary. It is a link column the user fills by hand in the cloud table; the publisher neither writes nor checks it.

## 数据类型 (data)

PDB · AlphaFold-DB · UniProt · ChEMBL · PubChem · DrugBank · LINCS · DepMap · PRISM ·
scRNA-seq · bulk-RNA-seq · spatial-transcriptomics-data · ATAC-seq · proteomics-data ·
metabolomics-data · imaging-data · MRI · CT · histology · EHR · claims-data ·
mouse · human-cohort · cell-line · organoid · in-house-wet-lab · simulated · benchmark-only

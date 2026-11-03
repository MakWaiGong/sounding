# Controlled vocabulary

Three families, one tag per concept. Rules:

- lowercase English, hyphens inside a phrase, no plurals, no synonyms.
- Reuse before creating. If a near-match exists, use it and note the intended nuance in the note body.
- Keep each family under ~40 options. When it grows past that, a tag is too fine-grained and belongs in the note text.
- The same tag strings go to Notion and to Zotero, so both sides can be browsed the same way.
- On the Notion side the three families are three separate multi-select fields — Notion multi-select has no nesting, so a single field cannot hold a dimension and its values at once.
- On the Zotero side tags are flat, so each family is carried by a prefix (`p:` / `m:` / `d:`). The prefixes come from `config.local.json` → `zotero_tag_prefix`.
- A concept that would appear in two families needs two distinct strings — hence `spatial-transcriptomics` (问题域) and `spatial-transcriptomics-data` (数据类型).

## 问题域 (problem)

protein-design · protein-structure-prediction · protein-function-prediction · binder-design ·
perturbation-prediction · virtual-cell · gene-regulatory-network · single-cell-analysis ·
omics-integration · spatial-transcriptomics · multi-omics ·
molecular-glue · targeted-degradation · allosteric-modulation · small-molecule-design ·
phenotypic-screening · drug-repurposing · target-identification · drug-combination · resistance ·
network-pharmacology · natural-product · gut-microbiome ·
medical-imaging · segmentation · grading-staging · knee-OA · cartilage ·
biomolecular-condensate · LLPS · phase-separation ·
synthetic-biology · pathway-engineering · metabolic-engineering · directed-evolution ·
foundation-model-benchmark · data-scarcity · label-noise · evaluation-methodology

## 方法族 (method)

transformer · attention · diffusion · flow-matching · GNN · CNN · RNN · VAE · GAN ·
contrastive-learning · self-supervised · masked-modeling · multi-task · transfer-learning ·
meta-learning · reinforcement-learning · bayesian · gaussian-process · causal-inference ·
foundation-model · LLM · prompt-engineering · retrieval · knowledge-graph ·
molecular-dynamics · docking · free-energy · density-functional ·
CRISPR-screen · high-content-imaging · mass-spectrometry · cryo-EM · sequencing · proteomics-pipeline ·
statistical-model · survival-analysis · meta-analysis · ablation-study

## 数据类型 (data)

PDB · AlphaFold-DB · UniProt · ChEMBL · PubChem · DrugBank · LINCS · DepMap · PRISM ·
scRNA-seq · bulk-RNA-seq · spatial-transcriptomics-data · ATAC-seq · proteomics-data ·
metabolomics-data · imaging-data · MRI · CT · histology · EHR · claims-data ·
mouse · human-cohort · cell-line · organoid · in-house-wet-lab · simulated · benchmark-only

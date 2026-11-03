# Shared evidence rules

These rules apply to every paper type. Templates reference them; they are not restated per template. Read this file once, then follow the template for structure.

---

## Figures

Three rules, all of them about not inventing anything:

1. **The caption is the paper's caption, verbatim.** Copy it from the PDF, including the `Figure N:` prefix. Undo the PDF's end-of-line hyphenation (`explic- itly` → `explicitly`, `time- coupled` → `time-coupled`) and rejoin the lines, but change no word. A caption is the authors' statement of what the figure shows; rewording it silently substitutes your reading for theirs.
2. **The number is the paper's number.** If the first image in the note is the paper's Figure 2, it is Figure 2. Never renumber by order of appearance in the note, and never call it "图一" because it comes first there. The same applies when citing a figure in prose: use the paper's number.
3. **Your own explanation goes elsewhere.** Reconstruct what the figure shows in the section body — block by block, in the figure's own layout order. If a Chinese gloss helps, put it on its own line marked `译：`, so the original and the gloss never mix.

If the caption is too long to sit under the image, keep it whole and let the body carry the reading. Truncating is allowed only at a sentence boundary, marked with `…`, and never at the cost of dropping what the figure is for.

When the source language is not English, quote the caption in the source language. Do not translate in place.

---

## Symbol table

Mandatory when the paper has equations.

Every symbol that appears in the equations: its meaning, its type (scalar / vector / matrix / set / distribution), and its shape where relevant. Ordered by first appearance.

- Include symbols used only once.
- Do not list symbols the paper defines but never uses.

| 符号 | 含义 | 类型/形状 |
| --- | --- | --- |
| $x_i$ | 第 $i$ 个样本的输入 | $\mathbb{R}^{d}$ |
| $\theta$ | 模型参数 | — |
| $\mathcal{L}$ | 总目标 | 标量 |

---

## Loss terms

For each term in the objective, record two things:

1. **What failure mode it prevents.** Not what it computes — what would go wrong without it.
2. **What happens if it is removed or its weight goes to zero.** If the paper's ablation covers the term, cite that ablation row. If not, write `原文未做消融`.

No derivations. If a derivation is load-bearing for understanding the method, state the result and say which section it comes from.

---

## Metrics

A metric is never explained alone. Every entry answers three questions: what it measures, how it is computed, and **what it hides** — because the metric beside it in the same table is usually there to cover exactly that blind spot.

Two metrics in one row are almost always there because each hides a failure the other exposes. Say which failure.

Definitions must appear **before** the table they are used in. Otherwise the rearranged table is just a pile of numbers.

The standard catalog — CS metrics, wet-lab statistics, and the pairs that must be read together — is in `reference-metrics.md`. Use it, and extend it locally when a paper introduces something not covered.

---

## Wet-lab sample sizes and statistics

Mandatory for any experiment with biological samples. Six items, no exceptions:

| 项 | 记什么 |
| --- | --- |
| n | 数字 + 它数的是什么：独立个体 / 独立实验 / 同一材料的技术重复 |
| 重复 | 几次独立实验，代表性图像来自几次 |
| 统计检验 | 具体检验名，配对与否，方差齐性假设 |
| 离散度 | mean ± SD 还是 ± SEM，写明 |
| p 值 | 数值 + 它对应哪两组比较 |
| 多重比较 | 是否校正，Bonferroni 还是 FDR |

The judgement that matters: `n = 3 只细胞系的技术重复` and `n = 3 次独立实验` are different evidence grades. When the paper is vague, copy the original wording and mark `表述不清`.

---

## Coverage markers

Use these verbatim when the paper does not report something. They are facts about coverage, not judgments about quality.

- `原文未报告` — the paper does not report it.
- `只在附录给了这个数` — the number exists but only in the supplement.
- `未给置信区间` — the statistic is given without dispersion.
- `表述不清` — the paper's wording does not settle the question.
- `原文未做消融` — the claim is asserted without an ablation row.
- `推断` — the point is reasoned from related work, not stated by the authors.

---

## Evidence-strength note

State which class of evidence carries the claim, and which classes only show correlation. Typical classes: 遗传, 生化, 成像, 组学, 临床样本, 消融, 统计.

If a claim rests on one class of evidence only, say so in the same sentence as the claim.

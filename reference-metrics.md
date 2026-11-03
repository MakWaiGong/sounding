# Metrics and statistics

This is the catalog. The rules for *how* to write a metric entry are in `reference-evidence.md`.

A metric is never explained alone. Every entry answers three things: what it measures, how it is computed, and **what it hides** — because the metric beside it in the same table is usually there to cover exactly that blind spot.

## CS metrics

| 指标 | 衡量什么 | 怎么算 | 盲区 |
| --- | --- | --- | --- |
| Accuracy | 整体正确率 | 正确 / 总数 | 类别不平衡时被多数类主导 |
| Precision | 报阳里有多少真阳 | TP / (TP + FP) | 忽略漏检，可通过少报提高 |
| Recall | 真阳里抓到多少 | TP / (TP + FN) | 忽略误报，可通过多报提高 |
| F1 | P 与 R 的调和平均 | 2PR / (P + R) | 不计 TN，阈值依赖 |
| Specificity | 真阴里排除多少 | TN / (TN + FP) | 单独看会掩盖低召回 |
| AUROC | 阈值无关的排序能力 | P(正样本得分 > 负样本得分) | 对类先验不敏感，正类极稀时高 AUC 仍不可用 |
| AUPRC | 正类稀少时的排序能力 | PR 曲线下面积 | 依赖正类比例，跨数据集不可直接比 |
| MAE | 平均绝对偏差 | mean(\|y − ŷ\|) | 对大误差不敏感，量纲依赖 |
| RMSE | 均方根误差 | sqrt(mean((y − ŷ)²)) | 被少数大误差主导 |
| R² | 相对均值的解释度 | 1 − SS_res / SS_tot | 负值合法，跨任务不可比 |
| IoU / Dice | 分割重叠度 | \|A∩B\|/\|A∪B\|；2\|A∩B\|/(\|A\|+\|B\|) | 小目标上惩罚极重，对边界像素敏感 |
| PSNR | 像素级保真 | 由 MSE 换算，dB | 与感知质量弱相关 |
| SSIM | 结构相似 | 局部窗口内亮度/对比/结构 | 对纹理与语义错误不敏感 |
| LPIPS | 感知距离 | 预训练网络特征距离 | 依赖所选网络，跨数据集不可比 |
| FID | 生成分布与真实分布的距离 | Inception 特征的高斯 Fréchet 距离 | 只看均值和协方差，样本量偏置，不反映个体多样性 |
| Perplexity | 语言模型的平均分支数 | exp(交叉熵) | 词表不同不可比 |
| Top-1 / Top-5 | 检索与分类命中率 | 首位命中 / 前五命中 | 类内差异被平均掉 |
| mAP | 检测与检索的综合排序质量 | 各类 AP 的平均 | 依赖 IoU 阈值设定 |
| FLOPs | 理论计算量 | 按算子统计 | 与实际延迟弱相关 |
| Latency / Throughput | 实际速度 | 端到端计时 | 依赖硬件、批大小、显存带宽 |
| pass@k | k 次采样至少一次通过 | 组合估计 | 采样温度与预算改变即不可比 |

**必须成对解释的**：

- **AUROC 与 AUPRC**：AUROC 对正类比例不敏感，看起来总是体面；AUPRC 会随正类变稀而崩塌。筛查类任务里只报 AUROC 等于没报可用性。
- **Precision 与 Recall**：任何一个都能靠调阈值单方面做好，所以同时出现才有意义。只报一个的表格要标 `阈值未说明`。
- **Accuracy 与 F1**：不平衡数据里 Accuracy 高而 F1 低，说明模型只学会了多数类。
- **PSNR/SSIM 与 LPIPS**：前两个是信号层，后一个是感知层。三者同升才算真提升，PSNR 升而 LPIPS 降通常意味着过度平滑。
- **FID 与生成式 precision/recall**：FID 奖平均，precision/recall 分别罚不真实和缺多样性。只看 FID 会漏掉模式崩塌。
- **FLOPs 与 latency**：小 batch、内存带宽受限、算子不受支持时，FLOPs 低反而更慢。部署类结论必须看 latency。

## Wet-lab statistics

Term glossary. The six-item reporting checklist (n / repeats / test / dispersion / p / multiple-comparison correction) is in `reference-evidence.md`; this table is about what each term means and how it gets misused.

| 项 | 含义与写法 | 常见误用 |
| --- | --- | --- |
| n | 数的是什么：独立个体、独立实验、技术重复 | 把技术重复写成 n，夸大证据 |
| SD | 样本离散度 | 与 SEM 混用 |
| SEM | 均值的标准误 = SD/√n | 随 n 增大自动变小，常被用来显得更精确 |
| t-test | 两组均值差，需近似正态 | 不说明配对与否、方差不齐时未用 Welch |
| Mann–Whitney U | 非参，比较分布位置 | 三组以上反复用 |
| ANOVA + post hoc | 三组以上的整体检验 | 只用 ANOVA 不做两两校正 |
| χ² / Fisher | 分类计数关联 | 期望频数小仍用 χ² |
| log-rank / Cox | 生存曲线与风险比 HR | 只报 p 不报 HR 与 CI |
| Pearson | 线性相关 | 存在离群点或非线性 |
| Spearman | 单调相关 | 被当作因果 |
| effect size | Cohen's d 或 fold change + CI | 只报 p，忽略效应大小 |
| multiple testing | Bonferroni（严格）或 BH-FDR（探索） | 多组比较未校正 |
| IC50 / EC50 | 半最大效应浓度 | 未给拟合模型与 CI |
| 95% CI | 估计区间 | 与 SD 混用，n 小时被省略 |

**必须成对解释的**：

- **SD 与 SEM**：SD 描述个体差异，SEM 描述均值精度。同样一组数据，报 SEM 会让误差棒短 √n 倍。
- **p 值与效应量**：p 小只说明不太可能是偶然，不说明差别大。样本量足够时微不足道的差异也能 p < 0.001。
- **p 值与 CI**：CI 同时给方向和精度，比 p 值信息量大。
- **Pearson 与 Spearman**：一个测线性，一个测单调。相关性结论的措辞取决于用哪个。
- **fold change 与绝对量**：低基数上的两倍可能毫无意义。

## 写作规则

- 指标先定义后使用。主表里出现但正文没定义的指标，由读笔记的人补上定义。
- 论文只给 p 不给 n／效应量／检验名，写 `原文未报告`，不要推测。
- 同一结论跨数据集比较指标时，先确认指标定义与数据划分一致，否则标 `不可直接比较`。
- 加粗的不是数字，而是"这个数字在什么条件下成立"。

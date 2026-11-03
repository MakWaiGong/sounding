# Topic page template

A topic page is a workbench for one question, not a reading list. The unit of the page is a **route**, not a paper: what has been tried, how far it got, where it stopped. A page with no routes is a folder.

One page per question. Five active pages is a lot; eight is a symptom.

---

## Template

```
# <Topic 名：一句可执行的问题>

状态：活跃 | 暂停 | 放弃 | 已完成
问题：<一句话，可判定> 
上次复盘：<date>

## 我现在的判断
<一句话立场> 置信度：低/中/高
<为什么这么判断，两三行>

## 路线图

路线 A <名字> — 证据强度：强/中/弱
  代表工作：<citekey>, <citekey>
  走到哪：<一句，带数字>
  卡点：<一句，说清为什么停>

路线 B <名字> — 证据强度：中
  代表工作：<citekey>
  走到哪：
  卡点：

路线 C <名字> — 证据强度：弱
  代表工作：
  走到哪：
  卡点：

## 空白
<我自己认为没人解决的那一格，以及为什么它值得做>

## 已读
<linked view：pillar = 本页>

## 排除
<读过的但与本问题无关的 + 一句为什么排除>

## 下一步
- [ ] 读 <citekey>，为了确认 <具体问题>
- [ ] 查 <数据集/代码> 能不能支持 <路线 X>
```

## Rules

- **每条路线必须有一个卡点。** 卡点是空的，说明这篇方向还没读懂，或者这条路已经死了却没人敢写。
- **每条路线至少挂一个 citekey。** 没有代表工作支撑的路线是想象。
- **排除区要写。** 记录"读过但不走这条"比记录"读过什么"更省时间——半年后你会重新想到同一条路。
- **问题可改，路线可删。** 改问题的时候在下面留一行 `v2026-11：从 <旧问题> 改为 <新问题>，因为 <原因>`，别把历史抹掉。
- 论文只在配置里指定的 papers 库里存一份；这里通过 relation 引用，不复制内容。relation 属性名见 `config.local.json` → `notion.properties`（默认 `topic` / `domain` / `wave` / `group`）。
- 一个方向读超过 30 篇仍在加路线、没有任何路线被排除或推进，说明问题没定住，回到第一行重写问题。

## 何时新建 topic

判据：**新的问题是否需要不同的成败标准。**

如果判成败的标准相同，就是已有 topic 里的一条新路线；如果标准变了（从"能不能预测"变成"能不能在湿实验里复现"），才是新 topic。

## 何时暂停

连续两个月没有新论文进入、也没有新路线被推进，改状态为 `暂停`。不要删——季度复盘时要看它为什么停。

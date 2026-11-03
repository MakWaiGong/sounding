# 落库：字段映射、幂等、回写

库是唯一的索引来源，笔记是正文。字段填不对，三个月后整库不可查。

字段名一律来自真实 schema，不凭印象写。概念名 → 实际字段名的对应关系存在 `config.local.json` → `notion.properties`。

---

## 1 先体检

第一次用、或换库之后先跑：

```bash
python scripts/check_config.py
```

它检查三件事：配置项是否齐、映射字段在库里是否存在、select 的合法值是否一致。任何一项报错，先修配置或库，不要开始写笔记。

运行期再确认一次：写之前 `fetch` 一次 `Papers` 的 schema，拿到当前真实字段列表。**配置文件是缓存，schema 才是事实。**

---

## 2 映射表

左列是配置里的概念名，右列是默认实际字段名。和你的库不一致时改这张表，不要改笔记。

### 必填：缺一个就停

| 概念 | 字段 | 类型 | 值 |
| --- | --- | --- | --- |
| `title` | 标题 | title | 短标题 |
| `venue` | 发表 | text | `Nature Methods`、`NeurIPS`，**不带年份** |
| `year` | 年份 | number | 四位数字 |
| `type` | 类型 | select | `conference` / `journal` / `review` / `other` |
| `citekey` | citekey | text | Zotero citationKey |
| `read_level` | 读法 | select | `扫` / `精读` |
| `problem` | 问题域 | multi_select | 见 `reference-tags.md` |
| `method` | 方法族 | multi_select | 同上 |
| `data` | 数据类型 | multi_select | 同上 |
| `verdict` | 去留 | select | `值得跟进` / `存档` / `仅作背景` |

`check_config.py` 把 `verdict` 也算必填。理由：速览第 10 行每次都要写，字段空着等于索引丢了这一维。若你的库确实不想要这一列，把它从 `scripts/check_config.py` 的 `REQUIRED` 移到 `OPTIONAL`。

### 选填：有就填，没有不报错

| 概念 | 字段 | 类型 | 值 |
| --- | --- | --- | --- |
| `topic` | 课题 | relation | 已有课题页 |
| `domain` | 领域 | relation | 已有领域页 |
| `wave` | 潮 | relation | 已有潮页 |
| `group` | 课题组 | relation | 已有组页 |

`verdict` 取速览第 10 行的「去留」，同一篇里必须一致。

四个 relation **只连已经存在的页面**。页面不存在就留空，并在 `待查` 记一行；不要顺手新建孤页，孤页会把地图搅乱。

### 观潮字段（库里已有，按需填）

| 概念 | 字段 | 类型 | 值 |
| --- | --- | --- | --- |
| `confidence` | 置信度 | select | `猜测` / `有据` / `复核` |
| `role` | 角色 | select | `创世` / `追随` / `旁支` / `方法件` / `未定` |
| `generation` | 世代 | relation（自指） | 指向定义这一代的那篇 |
| `generation_name` | 世代名 | text | 这一代的名字 |
| `opened` | 开创了什么 | text | 写成「从 X 变成 Y」 |
| `borrowed_from` | 借自 | relation（自指） | 它借了谁的组件 |
| `followers` | 追随者 | relation（自指） | 谁在跟着它做 |

这些是地图层的判断，不是从论文里抄来的事实。**没读透就不填**；判不出来就用 `未定`，不要留空装懂。

自指的 relation 只连已有的行。目标论文还没入库时，先在笔记的 `## 定位` 里写，等它入库再连。

---

## 3 缺字段怎么办

这是本流程唯一允许「停下」的写库错误，也是最容易悄悄烂掉的地方。

必填字段缺失时，报告格式固定：

```
缺字段：发表（概念 venue，应为 text）
库里现有：标题、年份、读法、标签
建议：新建属性「发表」(text) 后重跑
```

然后停。不要换近似字段、不要写进页面正文、不要只填一半、不要塞进 `标签`。

名字接近不算存在。`刊物` 与 `发表` 是两个字段，`year` 与 `年份` 也是。

---

## 4 幂等

按 `citekey` 精确查，不要用标题模糊搜（同一篇的预印本与见刊版标题常常不同）。

| 命中 | 动作 |
| --- | --- |
| 1 条 | 更新正文与属性。**不新建。** |
| 0 条 | 新建一行 |
| 多条 | 停。列出 page id 与各自标题，请用户合并 |

更新时正文整体替换——笔记是重建出来的，不是增量补丁。若原笔记里出现模板之外、明显是人手写的段落，先列出来问一句再覆盖。

---

## 5 标签

三族三个独立的多选字段，不是一列。

- 词表在 `reference-tags.md`：小写英文、短语内连字符、不用复数、不用同义词。
- **先复用再新建。** 建之前跑 `zotero_find.py --tags` 看库里实际在用什么。`scRNA` / `single-cell` / `单细胞` 三个并存，等于整库检索废掉。
- 每族一到三个。选了五个，说明这篇没聚焦，回摘要重判。
- 一族超过约 40 个选项，说明这个标签太细；它的位置在正文里，不在字段里。

---

## 6 回写 Zotero

三件事，缺一不可：

1. `Extra` 加一行 `Notion: <页面 URL>`。已有则替换那一行，不要留成两行。
2. 三族标签写进 Zotero 条目，加前缀：`p:` 问题域、`m:` 方法族、`d:` 数据类型。前缀取自 `config.local.json` → `zotero_tag_prefix`。Zotero 标签是扁平的，前缀是唯一能留住维度的办法。
3. 笔记里写了与另一篇的关系，就去那一篇补一条反向关系。单向关系半年后等于没有。

用 `scripts/zotero_write.py` 做前两步：

```bash
python scripts/zotero_write.py --citekey <citekey> --show
python scripts/zotero_write.py --citekey <citekey> \
  --tags p:protein-design,m:diffusion,d:PDB \
  --extra https://www.notion.so/<page>
```

要点：

- 它先读条目再合并，不整块覆盖。带 `p:` / `m:` / `d:` 前缀的标签算 skill 自己的，会被替换；你自己加的其他标签原样保留。
- 需要 `zotero.user_id` 与 `zotero.api_key`（或用环境变量 `ZOTERO_USER_ID` / `ZOTERO_API_KEY`）。key 在 https://www.zotero.org/settings/keys 建，勾 library access + write access，要写子笔记再加 notes access。
- `--dry-run` 只打印改动。写失败会打印 HTTP 状态与响应体。

失败就报错，不要静默跳过：告诉用户哪一步失败、条目 key 是什么；笔记本身照常产出。

**不会做的事**：不建条目、不下载 PDF、不在 PDF 上画批注。批注（annotation）是本地表里的对象，Web API 没有对应端点；能在 API 层写的只有笔记（note）。

---

## 7 从不做的事

- 不改库结构。不擅自加属性、删字段、改 select 选项；要改先说明原因，由用户决定。
- 不把正文塞进属性，也不把属性值展开进正文。
- 不为同一篇建第二行。
- 不用 relation 指向不存在的页面。

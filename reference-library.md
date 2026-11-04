# 落库：字段映射、幂等、回写

库是唯一的索引来源，笔记是正文。字段填不对，三个月后整库不可查。

字段名一律来自真实 schema，不凭印象写。概念名 → 实际字段名的对应关系存在 `config.local.json` → `notion.properties`。

---

## 1 先体检

第一次用、或换库之后先跑：

```bash
python scripts/check_config.py
python scripts/check_config.py --schema-json notes/schema.json
```

第一条离线检查映射配置，不要求 Notion token，也不宣称检查过真实库。第二条检查从 MCP 获取并整理成 `{"properties": {"真实字段名": {"type": "rich_text"}}}` 格式的 schema JSON，包括字段类型和 select 合法值。保留实际 `select.options`，不要从配置反造 schema。任何一项报错，先修配置或库。直接调用旧版 Notion database API 的检查需显式加 `--online`，并设置 `NOTION_TOKEN`。

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
| `citekey` | citekey | text | 机器键：`sha1` 前 10 位 |
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

citekey 用来标识论文。新记录由 `scripts/citekey.py` 对规范化 DOI，或标题、一作姓、年份计算机器键，取 SHA-1 前 10 位；不要在别处重写算法。已有条目先沿用 `citationKey` 或 Extra 的 `Citation Key:`；已有笔记用 item key 时继续沿用。`zotero_add.py` 未传 `--citekey` 时优先保留找到的旧键，仅在新记录上生成键。补 DOI、改标题、重建附件不自动改已有键。

`paper_identity.py` 统一解析这些身份别名，按 DOI 比较时保留 DOI 中有意义的标点。多个条目命中同一身份即报歧义；旧字段与 Extra 冲突时也不任选一个。Notion 仍按最终采用的 citekey 精确查重，不用标题猜更新目标。

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

顺序是先条目后笔记：笔记要引用一个在库里真实存在的 citekey。库里没有条目，就先建，不要用一个临时串去写库。

### 6.0 集合：`sounding`

Notion 是主库，Zotero 只承担"存 PDF 与书目信息"的角色。所以库里有一个专用集合 `sounding`（名字取自 `config.local.json` → `zotero.collection`），凡是被本 skill 读过的论文都归档在这里。

它回答的问题是：**这个 skill 维护的那批东西，边界在哪**。由此得到三个用法，也仅此三个：

- 导出它，就是一次完整备份（条目 + PDF）。
- 打开它，就是"这些论文我按 sounding 读过"，不用在几百条里翻。
- 它为空或丢了，重跑一次 `zotero_add.py` 就能重建。

它**不是**查询主键。找一篇论文仍然按 citekey，与集合无关；集合成员关系是投影，可以随时重建。这样即使你在 Zotero 里大改结构、合并集合、删掉重建，也不会影响 skill 找到东西。

两条纪律：

- skill 只往 `sounding` 写，不往里搬别的东西；你自己随手导入的论文不进这个集合，备份边界因此不会被稀释。
- 一个条目可以同时在 `sounding` 和课题集合里（如 `分子胶新`）。Zotero 的集合是多对多，两边不冲突：课题集合供你按项目浏览，`sounding` 供备份与审计。

`zotero_write.py` / `zotero_add.py` 都只做并集：读取现有 `collections` 再追加，绝不覆盖——写入 `collections` 字段是整表替换，漏读就会把条目从其它集合里踢出去。

**笔记里的 PDF 指针指向这个集合。** 正文头部写两条：`本地 PDF：<Zotero 附件的绝对路径>（Zotero 集合 sounding 内的附件）` 与 `Zotero：zotero://select/library/items/<item key>`。不写散落在阅读文件夹里的临时路径——那个路径下次就不在了，而 `sounding` 里的附件才是这份 skill 维护的那一份。

### 6.1 条目不存在时：建条目并挂 PDF

```bash
python scripts/zotero_add.py --pdf "<绝对路径>" \
  --title "<论文全称>" --authors "First Last;First Last" \
  --year 2026 --venue "AAAI" --type conference \
  --notion "<Notion URL>" \
  --tags p:protein-structure-prediction,m:molecular-dynamics,d:simulated
```

`--citekey` 省略时脚本优先保留既有键，仅新记录自动计算机器键；已有 Notion 记录时应传其采用的键以核对一致性。

- `--type` 与笔记里的 `类型` 一致，脚本据此选 Zotero 的 itemType（conference → `conferencePaper`，journal/review → `journalArticle`，other → `document`）。
- `--venue` 只放刊名／会议名，不带年份，与 Notion 的 `发表` 字段同值。
- 本地身份解析与服务端查重都拒绝多命中；优先用 DOI 等强标识核对。唯一匹配才更新，保留未提供的元数据和手写标签。
- PDF 作为 `imported_file` 子条目上传。上传成功后才回填 `md5`／`mtime`；如果中途失败，脚本会删掉留下的空附件再重试，不会积累一堆无文件的条目。
- `Extra` 自动写入两行：`Citation Key: <key>` 与 `Notion: <url>`。写 `Citation Key` 是为了让 Better BibTeX 不再自己生成一个不同的 key，否则同一篇会有两个 citekey。

### 6.2 条目已存在时：写标签与链接

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

**不会做的事**：不下载付费墙后的 PDF、不在 PDF 上画批注。批注（annotation）是本地表里的对象，Web API 没有对应端点；能在 API 层写的只有笔记（note）。

---

### 6.3 Notion 图像与完成状态

正文仍由 Notion MCP 写入。首次发布时先检查当前工具的图片上传或嵌入能力：本地文件路径不能当远端图片地址，`zotero://` 指针也不能代替图片。

1. 把已核对的裁图通过工具支持的上传方式上传，或使用用户已有的可访问托管地址。记录每张图的源 PDF 指纹、页码、裁图哈希和上传返回的标识；批量裁图清单可提供这些来源信息。
2. 按正文位置插入图片并写属性。回读同一页面，核对 citekey、正文和图片块/文件引用；工具能预览时检查图像可见。
3. 工具不支持上传、上传失败或只能核对块而不能确认图像时，明确记录缺失项。笔记可先保存，图片保持待完成，不宣称整篇已发布完成。不要为绕过上传而公开私人 PDF 或图片。
4. 记录 Notion page ID 后再回写 Zotero。回写失败只重试未完成步骤；远端结果不确定时先读回状态，避免重复创建页面或附件。

`run_state.py` 是本地执行记录，不会代替 MCP 上传，也不会自行证明远端成功。按 `reference-engineering.md` 保存实际工具响应与回读结果；源 PDF 或笔记变化后，重新检查受影响步骤。Notion 成功后的本地稿只作可删除的工作缓存，成品仍以 Notion 为准。

---

## 7 从不做的事

- 不改库结构。不擅自加属性、删字段、改 select 选项；要改先说明原因，由用户决定。
- 不把正文塞进属性，也不把属性值展开进正文。
- 不为同一篇建第二行。
- 不用 relation 指向不存在的页面。

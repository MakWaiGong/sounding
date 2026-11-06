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

运行期再确认一次：本次会话或批次第一次写之前 `fetch` 一次 `Papers` 的 schema，拿到当前真实字段列表；同一 data source 内复用，不逐篇重复 fetch，遇到 schema 错误立即丢弃缓存并重新获取。**配置文件是长期缓存，当前批次 fetch 到的 schema 才是事实。**

---

## 2 映射表

左列是配置里的概念名，右列是默认实际字段名。和你的库不一致时改这张表，不要改笔记。

### 必填：缺一个就停

| 概念 | 字段 | 类型 | 值 |
| --- | --- | --- | --- |
| `title` | 标题 | title | 重命名名（算法类「方法名：一句话灵感」；science·review 类「一句话中文核心发现」）；与 PDF 副本文件名一致，见 §6.0 |
| `venue` | 发表 | text | `Nature Methods`、`NeurIPS`，**不带年份** |
| `year` | 年份 | number | 四位数字 |
| `type` | 类型 | select | `algorithm` / `science` / `review` / `other`（骨架名，不是发表处；判定见 `SKILL.md` §3） |
| `citekey` | citekey | text | 稳定操作键：优先由规范化 DOI 派生的 `sha1` 前 10 位；不存原始 DOI |
| `data` | 数据类型 | multi_select | 见 `reference-tags.md` |
| `doi` | DOI | text | `10.xxxx/xxxx`，从 `## 定位素材` 的 `原文：` 行取；没有就不写，不报错 |

`去留`（`值得跟进` / `存档` / `仅作背景`）只活在笔记的速览第 10 行，不进库表，`check_config.py` 也不检查它。`置信度` 已从表里删掉。

### 选填：有就填，没有不报错

| 概念 | 字段 | 类型 | 值 |
| --- | --- | --- | --- |
| `topic` | 课题 | relation | 已有课题页 |
| `domain` | 领域 | relation | 已有领域页 |
| `wave` | 潮 | relation | 已有潮页 |
| `group` | 课题组 | relation | 已有组页 |

`DOI` 从 `## 定位素材` 的 `原文：<论文真名> · <发表> <年> · DOI 10.xxxx/xxxx` 一行取；缺 DOI 时表里留空，发布不中断。

四个 relation **只连已经存在的页面**。页面不存在就留空，并在 `待查` 记一行；不要顺手新建孤页，孤页会把地图搅乱。`潮` 与用于地图判断的 `课题组` 只在用户确认观潮结论后填写；单篇阶段的事实写进 `## 定位素材`，不提前转成 relation。

### 观潮字段（库里已有，按需填）

| 概念 | 字段 | 类型 | 值 |
| --- | --- | --- | --- |
| `role` | 角色 | select | `创世` / `追随` / `旁支` / `方法件` / `未定` |
| `generation` | 世代 | relation（自指） | 指向定义这一代的那篇 |
| `generation_name` | 世代名 | text | 这一代的名字 |
| `opened` | 开创了什么 | text | 写成「从 X 变成 Y」 |
| `borrowed_from` | 借自 | relation（自指） | 它借了谁的组件 |
| `followers` | 追随者 | relation（自指） | 谁在跟着它做 |

这些是地图层的判断，不是从论文里抄来的事实。单篇 sounding 分析默认全部留空，正文状态写 `待回顾`；空字段不等于 `无对应潮`。只有用户在回顾中确认结论后才填写。`未定` 只用于用户已经把论文纳入回顾范围、但决定暂不确定角色的情形，不能用来代替 `待回顾`。

自指的 relation 只连已有的行。用户已确认结论但目标论文还没入库时，先在笔记的 `## 定位` 里写，等它入库再连。状态定义、回顾与回写顺序见 `reference-wave.md`。

---

## 3 缺字段怎么办

这是本流程唯一允许「停下」的写库错误，也是最容易悄悄烂掉的地方。

必填字段缺失时，报告格式固定：

```
缺字段：发表（概念 venue，应为 text）
库里现有：标题、年份、标签
建议：新建属性「发表」(text) 后重跑
```

然后停。不要换近似字段、不要写进页面正文、不要只填一半、不要塞进 `标签`。

名字接近不算存在。`刊物` 与 `发表` 是两个字段，`year` 与 `年份` 也是。

---

## 4 幂等

按 `citekey` 精确查，不要用标题模糊搜（同一篇的预印本与见刊版标题常常不同）。

DOI 是论文身份的首选来源，但不原样写进 citekey。原始 DOI 含 `/`，部分 DOI 还含括号、冒号或其他不适合 Windows 文件名、BibTeX key 与命令参数的字符；而本工程的 citekey 同时用于 Notion 幂等、本地工作文件名与 Zotero `Citation Key`。因此新记录由 `scripts/citekey.py` 对规范化 DOI，或无 DOI 时对标题、一作姓、年份计算机器键，取 SHA-1 前 10 位；不要在别处重写算法。

已有条目先沿用 `citationKey` 或 Extra 的 `Citation Key:`；已有笔记用 item key 时继续沿用。`zotero_add.py` 未传 `--citekey` 时优先保留找到的旧键，仅在新记录上生成键。补 DOI、改标题、重建附件不自动改已有键。DOI 仍是查询别名和冲突核对依据，不因哈希而丢失。

`paper_identity.py` 统一解析这些身份别名，按 DOI 比较时保留 DOI 中有意义的标点。多个条目命中同一身份即报歧义；旧字段与 Extra 冲突时也不任选一个。Notion 仍按最终采用的 citekey 精确查重，不用标题猜更新目标。

| 命中 | 动作 |
| --- | --- |
| 1 条 | 更新正文与属性。**不新建。** |
| 0 条 | 新建一行 |
| 多条 | 停。列出 page id 与各自标题，请用户合并 |

更新时正文整体替换——笔记是重建出来的，不是增量补丁。若原笔记里出现模板之外、明显是人手写的段落，先列出来问一句再覆盖。

---

## 5 标签

数据类型一个多选字段。领域不在这里：它是用户在云端表里手填的链接列，skill 不写不查。

- 词表在 `reference-tags.md`：小写英文、短语内连字符、不用复数、不用同义词。
- **先复用再新建。** 建之前跑 `zotero_find.py --tags` 看库里实际在用什么。`scRNA` / `single-cell` / `单细胞` 三个并存，等于整库检索废掉。
- 一到三个。选了五个，说明这篇没聚焦，回摘要重判。
- 超过约 40 个选项，说明这个标签太细；它的位置在正文里，不在字段里。

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

落库的是**重命名副本**：收到 PDF 先按 §2 的 `title` 规则生成重命名名，把副本存到 `zotero.data_dir` 下的 `zotero.collection` 目录（`config.local.json`，本机为 `<data_dir>\sounding`），`zotero_add.py --pdf` 传这个副本。副本文件名 = 重命名名 = Notion 页面 `标题`，三处一致。命名方案与判定（算法类 / science · review 类）见 `SKILL.md` 的「PDF 重命名副本」小节。

### 6.1 条目不存在时：建条目并挂 PDF

```bash
python scripts/zotero_add.py --pdf "<重命名副本的绝对路径>" \
  --title "<论文真实标题>" --authors "First Last;First Last" \
  --year 2026 --venue "AAAI" --type conference \
  --notion "<Notion URL>" \
  --tags d:simulated
```

`--pdf` 传第 6.0 节生成的重命名副本（附件文件名即重命名名，脚本用 `basename` 取）；`--title` 仍是论文真实标题，作为 Zotero 的书目信息，区别于 Notion 的 `标题`（重命名名）——两者各司其职，不要混用。

`--citekey` 省略时脚本优先保留既有键，仅新记录自动计算机器键；已有 Notion 记录时应传其采用的键以核对一致性。

- `--type` 是 **Zotero 条目类型**，与笔记第 3 行的 `类型`（骨架：`algorithm` / `science` / `review` / `other`）是两件事：`conference` → `conferencePaper`，`journal`／`review` → `journalArticle`，`other` → `document`。发在会议上的算法论文传 `conference`，发在期刊的算法论文传 `journal`——骨架看这篇怎么立论，条目类型看它发在哪。
- `--venue` 只放刊名／会议名，不带年份，与 Notion 的 `发表` 字段同值。
- 本地身份解析与服务端查重都拒绝多命中；优先用 DOI 等强标识核对。唯一匹配才更新，保留未提供的元数据和手写标签。
- PDF 作为 `imported_file` 子条目上传。上传成功后才回填 `md5`／`mtime`；如果中途失败，脚本会删掉留下的空附件再重试，不会积累一堆无文件的条目。
- `Extra` 自动写入两行：`Citation Key: <key>` 与 `Notion: <url>`。写 `Citation Key` 是为了让 Better BibTeX 不再自己生成一个不同的 key，否则同一篇会有两个 citekey。

### 6.2 条目已存在时：写标签与链接

三件事，缺一不可：

1. `Extra` 加一行 `Notion: <页面 URL>`（飞书后端则为 `Feishu: <页面 URL>`）。已有则替换那一行，不要留成两行。
2. 数据类型写进 Zotero 条目，加前缀：`d:` 数据类型。前缀取自 `config.local.json` → `zotero_tag_prefix`。Zotero 标签是扁平的，前缀是唯一能留住维度的办法。领域那类归口不在这里写——用户在云端表里手填。
3. 笔记里写了与另一篇的关系，就去那一篇补一条反向关系。单向关系半年后等于没有。

用 `scripts/zotero_write.py` 做前两步：

```bash
python scripts/zotero_write.py --citekey <citekey> --show
python scripts/zotero_write.py --citekey <citekey> \
  --tags d:PDB \
  --extra https://www.notion.so/<page>
```

要点：

- 它先读条目再合并，不整块覆盖。带 `d:` 前缀的标签算 skill 自己的，会被替换；你自己加的其他标签原样保留。
- 条目解析顺序：本地快照 → API 按**精确标题**搜（先试 `## 定位素材` 里 `原文：` 的论文真名，再试笔记标题）→ API 按 citekey 搜。标题优先是因为 Zotero 的 `q=` 不索引 `Extra`，按 citekey 搜不到只写在 `Citation Key` 里的条目。
- 需要 `zotero.user_id` 与 `zotero.api_key`（或用环境变量 `ZOTERO_USER_ID` / `ZOTERO_API_KEY`）。key 在 https://www.zotero.org/settings/keys 建，勾 library access + write access，要写子笔记再加 notes access。
- `--dry-run` 只打印改动。写失败会打印 HTTP 状态与响应体。

失败就报错，不要静默跳过：告诉用户哪一步失败、条目 key 是什么；笔记本身照常产出。

**不会做的事**：不下载付费墙后的 PDF、不在 PDF 上画批注。批注（annotation）是本地表里的对象，Web API 没有对应端点；能在 API 层写的只有笔记（note）。

---

### 6.3 图像与完成状态

后端二选一。**飞书后端**：裁图同样用 `fig_upload.py crop` 产出 manifest，但上传与插入由 `scripts/publish.py` 一次做完（不需要 MCP，也不需要 `tag` 一步），走法与回读标准见 `reference-feishu.md`。**Notion 后端**：下面三步是硬依赖。

正文仍由 Notion MCP 写入。首次发布前必须先按 `reference-notion.md` 完成 MCP 安装、schema 与读写探针；只读成功不够。随后检查当前工具的图片上传或嵌入能力：本地文件路径不能当远端图片地址，`zotero://` 指针也不能代替图片。

1. **裁图**：用 `scripts/fig_upload.py crop` 一次裁完所有主图，不要用 `fig.py page` 整页渲染当图。命令格式：
   ```bash
   python scripts/fig_upload.py crop paper.pdf --citekey <key> \
       --fig "3:35,130,565,750:fig1" \
       --fig "7:35,45,565,710:fig3"
   ```
   产出 `notes/figs/<citekey>/manifest.json`，每张图的 `notion_source` 初始为 null。
2. **上传**：对 manifest 中每张裁图，调 Notion MCP `notion_create_file_upload`（filename 用 `<label>.png`），拿到 upload_url 后立即 `curl.exe -F "file=@<local_path>"` 上传。多张图批量申请上传槽后并发上传，单个失败只重试该图。
3. **出标签**：把每张图上传返回的 `markdown_source`（`file-upload://uuid`）喂回脚本：
   ```bash
   python scripts/fig_upload.py tag --citekey <key> \
       --source fig1=file-upload://<uuid1> --source fig3=file-upload://<uuid3>
   ```
   **若有图未上传，tag 直接报错退出**——这是硬依赖，不允许跳过上传只裁图。产出的 `<image source=...>` 标签按正文位置一次性插入 Notion 页面。
4. **图注**：每张图下面跟一行斜体图注，不是"这张图长什么样"，而是"从这张图你该带走什么"。必须包含三要素：
   - **支撑哪个子命题**（P1/P2/P3/P4，或"概念框架"）
   - **关键内容**：算法类写清数学对应/架构设计决策/对比数字；实验类写清样本量/效应量/统计显著性
   - **一句 insight**：读者看完图后应该记住的结论，不是重复图标题

   **算法类架构图/流程图额外加读图引导**：按图里的空间方向（从左到右/从上到下）逐个说清关键模块是什么、箭头表示什么数据流/信号流。算法类论文的架构图读者第一眼往往看不懂结构，图注要当导游。数据图（折线/柱状/热图）不需要这步。

   反例："9个token mixer×8个数据集受控对比"（只说了做了什么）。正例："P3 受控对比：参数规模相同只换token mixer，静态图像Conv最优、神经形态linear attention最优、MQAR检索只有2D矩阵记忆能做——SparseLA在中等记忆任务上效率/性能平衡最好"。
5. 最后回读同一页面一次，核对 citekey、正文和全部图片块；工具不支持上传或上传失败时，任务状态保持「发布受阻（未完成）」，不宣称完成。不要为绕过上传而公开私人 PDF 或图片。
6. 记录 page ID（飞书是 doc_id）后再回写 Zotero：`Extra` 写 `Notion: <url>` 或 `Feishu: <url>`，与发布后端一致。回写失败只重试未完成步骤；远端结果不确定时先读回状态，避免重复创建页面或附件。

`run_state.py` 是本地执行记录，不会代替 MCP 上传或 `publish.py` 的回读，也不会自行证明远端成功。按 `reference-engineering.md` 保存实际工具响应、分段耗时与回读结果；源 PDF 或笔记变化后，重新检查受影响步骤。发布成功后本地稿只作可删除的工作缓存，成品以云端为准。

---

### 6.4 双后端迁移

把一篇的主后端从 Notion 换到飞书（或反向）时，只有一条路：**把同一份 md 重发到新后端**。`publish.py` 按 citekey 幂等，命中的是同一行、同一篇文档。不做 page→page 搬运——两边块模型不同，callout、公式、图注、表格搬过去只会退化，仓库里也没有反向转换器。

迁移期间守五条：

1. **元数据只认一边**。飞书 `Papers` 表是结构化真源，世代、潮、角色、领域只在那里改；旧后端的 relation 列冻结，不追着改。旧页面里的 `方法族`/`问题域`/`去留`/`置信度` 留在原地作历史，不回填、不删（除非用户明确要求）。
2. **笔记里留迁移标记**。可以手写，也可以让脚本写：
   ```bash
   python scripts/publish.py notes/<citekey>.md --mark-migrated notion
   ```
   省略值时会从 Zotero `Extra` 的 `Notion:` / `Feishu:` 行推断旧后端；推断不出就报错，要求显式传值。脚本在 `## 定位素材` 下写入或改写一行 `迁移：<YYYY-MM-DD> 从 Notion 迁入；主后端 = 飞书`（重复执行只改日期，不叠加），随后照常发布，这行会随正文进云端——这也是"读者一眼能看出这篇是从哪儿搬来的"的唯一线索。
3. **旧页面标只读**。在旧页面顶部插一条提示：Notion 用 `notion-update-page` 的 `command: insert_content` + `position: {type: start}`，写清"已迁移，本页只读"、链到新文档、说明结构化信息以新表为准。页面保留作历史，不删。
4. **缺失侧补发**。只有一边有笔记时，"照抄"= 用那份 md 重发到目标后端，不是复制页面内容；重发前先确认这份 md 是最新的（`rev` 对不上就重发）。
5. **不长期两边各写**。云端正文是清空重建、不可合并，哪边改过无法判定，重发会静默冲掉另一边的手工改动。迁移窗口一结束就锁掉旧后端，之后新改动只写主后端。

---

## 7 从不做的事

- 不改库结构。不擅自加属性、删字段、改 select 选项；要改先说明原因，由用户决定。
- 不把正文塞进属性，也不把属性值展开进正文。
- 不为同一篇建第二行。
- 不在迁移后同时维护两份正文：主后端只有一处可写（见 §6.4）。
- 不用 relation 指向不存在的页面。

# 观潮 / Sounding

**观潮**是用户主导的远看：集中比较一批论文，决定潮往哪走、推着哪些领域、各课题组站在哪一格。
**测深**是近看：放下铅锤，量清楚这一个点的深度。

两个名字不是互译。一个命名宏观层，一个命名微观层，缺一个都不完整。

这是一套**读文献的方法**，不是一套笔记模板。它把「读了 20 篇」变成「这个方向现在缺什么」，中间是四个顺序固定的动作：

| 动作 | 问的问题 |
| --- | --- |
| 选 | 这一批里先完整分析哪几篇 |
| 拆 | 这篇的结论靠什么立住，证据到哪为止 |
| 放 | 横向比较这一批，由用户决定潮的边界、第几代、谁在推 |
| 判 | 这个方向缺什么，缺的那一格可不可测 |

模板是它的产物，不是它的定义。用户交来一篇论文时不做扫读分流，直接完整分析；真正被规定的是如何重建论证，以及什么话在证据不足时不许写。

## 产物

单篇读完先产出笔记与定位素材；一批回顾后再产出位置和方向判断：

- **笔记** —— 含 10 行速览（几个月后只会重看这一段）、三种骨架之一（按论文结构选：conference 四段式（科学故事 / 灵感 / 具体算法 / 基线对比） / journal 三段式 / other 通用式）、从原 PDF 裁下并逐块解释的图、符号表、每个损失项在防哪种失败、指标定义与互补盲区、湿实验六项（n、重复、检验、离散度、p、多重比较校正）、与已读的关系
- **定位素材** —— 单篇只留可核对的能力变化、比较口径、课题组事实和前后作线索，状态为 `待回顾`；不自动判 wave
- **位置** —— 回顾时由 agent 对齐多篇证据与候选划分，用户确认潮、世代、课题组、角色、开创性、证据强度与推翻条件后才回写
- **判断** —— 这个方向缺什么，判据是什么，什么证据会推翻它

四条贯穿全篇的规则：

- **复述，不评判。** 覆盖度的事实照记（`原文未报告`、`未给置信区间`），价值判断不写。
- **每个数字带出处。** 后面跟 `Fig. 3` / `Table 2` / `Sec. 4.2`。
- **图裁不画，图注照抄，下面带一行中文。** 裁原 PDF，不重画、不叠标注。图注逐字取原文，图号沿用论文编号——笔记里第一张图也可能是 Figure 2。图注下另起一行 `中文：`，写这张图怎么看而不是直译；逐块重建放正文；表格重打成 markdown。
- **判断要可被推翻。** 写不出「什么证据会推翻它」的，是印象，不是判断。

## 脚本

| 脚本 | 做什么 |
| --- | --- |
| `scripts/setup.py` | 首次配置：探测 Zotero 路径、写 key、认 Notion 库；缺 Notion 配置时返回失败 |
| `scripts/check_config.py` | 离线体检，或对 MCP 导出的 schema 校验字段类型和 select 值 |
| `scripts/citekey.py` | 为新记录计算机器键，旧键保持不变 |
| `scripts/paper_identity.py` | 共用身份解析、DOI 别名与只读 SQLite 一致性快照 |
| `scripts/zotero_find.py` | 查作者／标题／年份／DOI／citekey，支持 JSON 一次返回元信息和附件 |
| `scripts/zotero_add.py` | 建条目、上传 PDF、并入 `sounding` 集合，写 `Citation Key` 与 `Notion` 到 Extra |
| `scripts/zotero_write.py` | 回写三族标签与 Notion 链接，合并式，不动手写标签 |
| `scripts/pdf_prepare.py` | 按 PDF 指纹缓存逐页原文、图注候选和坐标，支持检索 |
| `scripts/fig.py` | 扫图注、裁图、渲染页面，支持 JSON 清单批量裁图 |
| `scripts/validate_note.py` | 检查笔记结构、图片路径与来源定位；不代替语义核对 |
| `scripts/run_state.py` | 记录实际完成步骤和待办，识别源文件/笔记变化 |
| `scripts/notion_workspace.py` | 缓存工作区级 MCP 读写探针，避免逐篇重复验收 |

## 安装

它是一个标准的 Agent Skill：一个目录，入口是带 YAML frontmatter 的 `SKILL.md`。凡是认这个格式的 agent 都能直接用——Cursor、Claude Code、Codex CLI，以及其他实现了 Agent Skills 的工具。脚本用自身路径定位文件，不假设被装在哪里，所以不需要为不同 agent 改代码。

把整个目录放进那个 agent 的 skills 目录：

| Agent | 个人级 | 项目级 |
| --- | --- | --- |
| Cursor | `~/.cursor/skills/sounding` | `.cursor/skills/sounding` |
| Claude Code | `~/.claude/skills/sounding` | `.claude/skills/sounding` |
| Codex CLI | `~/.codex/skills/sounding` | — |

三者的布局一致：`<skills>/<name>/SKILL.md`。用别的工具就查它的文档，只要是同一格式，放进去即可。

用在多个 agent 里时**不要复制多份**——复制就会不同步。做链接：

```bash
# macOS / Linux
ln -s /path/to/sounding ~/.cursor/skills/sounding
ln -s /path/to/sounding ~/.claude/skills/sounding

# Windows：目录联接，不需要管理员权限
mklink /J "%USERPROFILE%\.cursor\skills\sounding" "D:\code\sounding"
```

脚本需要 Python 3.10+，在仓库目录安装依赖：

```bash
python -m pip install -e .
```

文档里的 `python` 在 macOS / Linux 上通常是 `python3`。

安装只准备 Python 依赖；skill 仍从这个目录使用，不复制配置。开发检查用 `python -m pip install -e ".[dev]"`，然后运行 `python -m unittest discover -s tests -v`。CI 在 Windows/Linux 与 Python 3.10/3.12 上执行离线测试；测试使用临时 PDF、SQLite 与模拟 API 响应，不需要用户的 Notion/Zotero 凭证。

## 首次使用

配置不随仓库走。第一次调用时 skill 会问你三项，也可以自己跑引导脚本：

```bash
python scripts/setup.py
```

skill 把官方 Remote Notion MCP 声明为必需依赖，地址是 `https://mcp.notion.com/mcp`。首次安装需完成 OAuth 和一次创建—回读—更新—清理探针；探针按工作区缓存，逐篇发布不重复执行。详细标准见 [reference-notion.md](reference-notion.md)。配置脚本问的只有这些，其余能探测就探测：

| 要问的 | 怎么答 |
| --- | --- |
| Zotero 数据目录 | 含 `zotero.sqlite` 的目录，脚本会先自动找 |
| Zotero API key | https://www.zotero.org/settings/keys 新建，勾 library + write（写子笔记再加 notes）。可以跳过 |
| Notion Papers 库链接 | 粘整条 URL 即可 |

`user_id` 由脚本拿 key 反查。跑完 `python scripts/setup.py --check` 能看到本地配置状态（数据库能读多少条、key 属于谁）；它不会冒充 MCP 连通性测试。随后必须按 `reference-notion.md` 用真实 Notion 工具跑读写探针。

## 配置

`config.example.json` 是出厂默认，`config.local.json` 是你自己的副本（git 忽略）。通常不用手改，用 `scripts/setup.py` 生成即可；要手改就：

```bash
cp config.example.json config.local.json
```

配置只有一份。多个 agent 共用同一个 clone 时，配置就在 clone 里，自动共享；如果 clone 分散在几处，用 `SOUNDING_CONFIG` 指到同一个文件，不必把 API key 复制到每个目录：

```bash
# Windows
setx SOUNDING_CONFIG "%USERPROFILE%\.sounding\config.local.json"
# macOS / Linux
export SOUNDING_CONFIG=~/.sounding/config.local.json
```

`setup.py` 会写到这个位置，`--check` 也会从它读。

然后改 `config.local.json`：

| 键 | 是什么 |
| --- | --- |
| `zotero.db_path` | `zotero.sqlite` 的绝对路径 |
| `zotero.data_dir` | Zotero 存放数据的主目录 |
| `zotero.user_id` | API 用的 userID（不是用户名），keys 页面顶部可见 |
| `zotero.api_key` | Web API key，只给回写脚本用 |
| `notion.papers_database_id` | 每篇一行的 Papers 库 |
| `notion.papers_data_source_id` | 同库的 data source，`collection://…` |
| `notion.properties` | 概念名 → 你库里的真实字段名 |
| `notion.option_values` | select 字段的合法值 |
| `zotero_tag_prefix` | 三族标签在 Zotero 侧的前缀 |

环境变量可覆盖文件：`ZOTERO_DB`、`ZOTERO_DATA_DIR`、`NOTION_PAPERS_DB`、`NOTION_PAPERS_DS`。本地配置按嵌套字段覆盖默认值，未填写的默认项保留；显式空值仍保留为空。

`config.local.json` 是你自己的副本，不进版本库。

### 库里需要的字段

| 概念 | 类型 | 说明 |
| --- | --- | --- |
| `title` | title | 论文标题 |
| `venue` | text | 刊名或会议名，不带年份 |
| `year` | number | |
| `type` | select | `conference` / `journal` / `review` / `other` |
| `citekey` | text | 稳定操作键：优先由规范化 DOI 派生的 `sha1` 前 10 位；原始 DOI 只作身份与查询别名 |
| `problem` | multi-select | 问题域 |
| `method` | multi-select | 方法族 |
| `data` | multi-select | 数据类型 |
| `verdict` | select | `值得跟进` / `存档` / `仅作背景` |
| `topic` / `domain` / `wave` / `group` | relation | 选填；`wave` 与地图层的 `group` 只在用户确认回顾结论后连接 |
| `confidence` | select | `猜测` / `有据` / `复核`，用户确认定位后填写 |
| `role` | select | `创世` / `追随` / `旁支` / `方法件` / `未定`，用户确认定位后填写 |
| `generation` / `generation_name` / `opened` | relation / text / text | 世代与「从 X 变成 Y」，用户确认定位后填写 |
| `borrowed_from` / `followers` | relation | 借自谁、谁在跟，用户确认定位后填写 |

三族标签是三个独立的多选字段，因为 Notion 多选不支持嵌套。Zotero 侧标签是扁平的，靠 `p:` / `m:` / `d:` 前缀保留维度。

任何映射字段在库里不存在，skill 停下报告，不丢值。

## Zotero 里怎么放

Notion 是主库，Zotero 只存 PDF 与书目信息。被本 skill 读过的条目统一进一个集合 `sounding`（`config.local.json` → `zotero.collection`，`--no-collection` 可关）。

| 用途 | 怎么用 |
| --- | --- |
| 备份 | 导出 `sounding` 就是完整一份（条目 + PDF） |
| 浏览 | 打开它，就是"按这套方法读过的"那一批，不必在几百条里翻 |
| 重建 | 集合没了重跑 `zotero_add.py`，成员关系是投影不是事实源 |

它不是查询主键：找论文一律按 citekey，与集合无关。所以你在 Zotero 里合并集合、改组结构，都不影响 skill 落库。

一个条目可以同时在 `sounding` 和课题集合（如 `分子胶新`）里——Zotero 集合是多对多，两边不冲突：课题集合供按项目浏览，`sounding` 供备份与审计。写入时脚本先读现有 `collections` 再追加，因为该字段是整表替换，漏读会把条目从别的集合踢出去。

笔记正文头部的 PDF 指针也指这里：`本地 PDF：<Zotero 附件绝对路径>（Zotero 集合 sounding 内的附件）` 与 `Zotero：zotero://select/library/items/<item key>`。不写阅读文件夹里的临时路径——`sounding` 里的那一份才是这份 skill 维护的副本。

## Notion 工具

笔记的成品位置是 Notion 页面，Notion MCP 是发布硬依赖。工具不可用时先安装或连接官方 Remote Notion MCP、完成 OAuth，并跑一次读写探针；不能把 `notes/<citekey>.md` 当作完成后的替代品。

本地稿可以用于故障恢复，但此时任务必须标为「发布受阻（未完成）」。页面写入后要按同一 page ID 回读正文、属性和图片引用，成功后再把 Notion URL 回写 Zotero。

## 文件

```
SKILL.md                 流程与规则
reference-conference.md  四段式模板，逐节验收
reference-journal.md     三段式模板，逐节验收
reference-other.md       综述 / benchmark / 数据集 / 立场论文的骨架
reference-evidence.md    所有类型的共用规则：符号、损失、指标、湿实验统计
reference-metrics.md     标准指标与统计术语表
reference-tags.md        三族受控词表
reference-library.md     字段映射、幂等、回写
reference-engineering.md 缓存、批量裁图、机械校验、运行记录的命令约定
reference-notion.md      Notion MCP 安装、读写探针、发布完成与阻塞标准
reference-wave.md        批量观潮：定位素材、候选划分、人工决策与回写
reference-topic.md       课题页模板
config.example.json      出厂默认
config.local.json        你自己的副本（git 忽略）
scripts/zotero_find.py   在本地 Zotero 库里查条目与 PDF
scripts/zotero_write.py  把标签与 Notion 链接写回条目（需要 API key）
scripts/setup.py         首次配置：探测、校验、写 config.local.json
scripts/fig.py           在页面上找出图片块并裁出
scripts/config.py        脚本共用的配置读取
scripts/check_config.py  离线校验配置，或对照导出的真实 schema
scripts/pdf_prepare.py   缓存逐页原文与来源定位
scripts/paper_identity.py 共用身份查询与只读快照
scripts/validate_note.py  机械验收
scripts/run_state.py      运行进度与失效检查
scripts/notion_workspace.py 工作区级 Notion 探针缓存
schemas/run.schema.json   运行状态格式
tests/                   离线行为测试
pyproject.toml           Python 版本与依赖
```

`reference-evidence.md` 存在的意义是规则只写一次：模板只放结构与验收，规则改了只改一处。

## DOI 与 citekey

DOI 足以作为论文身份的首选依据，但不适合原样承担本工程的 citekey。citekey 还要进入 Windows 文件名、命令参数、Notion 精确查重和 Zotero `Citation Key`；DOI 固定含 `/`，有些还含括号或冒号。因此新论文优先对规范化 DOI 计算稳定短键；没有 DOI 才使用标题、一作姓和年份。DOI 本身继续保存在书目元数据中，并可直接用于检索与冲突核对。

## 脚本

```bash
python scripts/zotero_find.py "attention is all you need"
python scripts/zotero_find.py --citekey yangArbovirusSuppressionLectin2025 --pdf
python scripts/zotero_find.py --list-collections
python scripts/zotero_find.py --tags
python scripts/zotero_find.py --citekey yangArbovirusSuppressionLectin2025 --json

python scripts/zotero_write.py --citekey key --show
python scripts/zotero_write.py --citekey key --tags p:x,m:y,d:z --extra https://www.notion.so/…

python scripts/fig.py scan paper.pdf --page 3
python scripts/fig.py crop paper.pdf --page 3 --bbox 197,72,415,394 --out fig1.png --dpi 220
python scripts/pdf_prepare.py paper.pdf --pages 2-5 --search loss
python scripts/fig.py batch paper.pdf --manifest crops.json --out-dir notes/figs/example

python scripts/check_config.py    # 离线配置检查
python scripts/check_config.py --schema-json notes/schema.json
```

`zotero_find.py` 用 SQLite backup 创建只读内存快照，包含已提交的 WAL 内容，每次调用使用当前一致状态。`fig.py scan` 打印图片块边界和完整候选图注，批量裁图仍需人工确认区域。缓存和运行记录默认放在 git 忽略的 `notes/` 下；它们可删除、可重建，不是第二份笔记主库。完整命令、校验边界和失败续做见 [reference-engineering.md](reference-engineering.md)。

## 判类型

用户给出论文后直接执行完整 sounding，不先做扫读或价值筛选。全文不足时索取全文，不生成三行替代笔记。

看**论文的结构**，不看发在哪。Nature、NeurIPS、TPAMI 都不在这条线的任何一边。

| 信号 | 类型 |
| --- | --- |
| Figure 1 是模块图，章节是 Method/Experiments | `conference` |
| Figure 1 是流程或机制图，章节是 Results/Discussion | `journal` |
| 机制示意图与独立方法模块并存（干湿结合） | `journal` |
| 都不是——综述、benchmark、数据集、立场论文 | `other` |

## 观潮

单篇笔记回答「这篇做了什么」，默认只留下 `## 定位素材`，状态为 `待回顾`。它不自动回答「属于哪波潮」，也不填写 wave、世代、角色或开创性字段。`待回顾` 与用户回顾后明确给出的 `无对应潮` 是两种不同状态。

观潮是独立的批量回顾。agent 准备多篇对齐表、支持／反证／未知和候选划分；用户决定潮的边界与命名、是否分代、论文角色和哪些能力变化算开创。确认后 agent 再统一回写笔记、relation、潮页与反向关系。

约 5–8 篇同主题待回顾材料、可能改写边界的新论文、既有潮无法容纳的新证据，或用户设定的定期节点，都可以触发一次回顾提醒，但不授权 agent 自动定位。能力台阶、群体迁移与公共沉淀是比较维度，不再是跨领域的硬门槛；完整流程见 `reference-wave.md`。

## License

MIT.

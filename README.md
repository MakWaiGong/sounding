# 观潮 / Sounding

**观潮**是远看：潮往哪走、推着哪些领域、各课题组站在哪一格。
**测深**是近看：放下铅锤，量清楚这一个点的深度。

两个名字不是互译。一个命名宏观层，一个命名微观层，缺一个都不完整。

这个 skill 把一篇论文拆成一份结构化笔记。它不是摘要器——它重建论证：此前难在哪、作者看到了什么别人没看到的、他做了什么、证据到底支撑到哪。

## 产物

每篇一份笔记，含：

- **10 行速览** —— 几个月后只会重看这一段
- **四种骨架**，按论文结构选：conference 四段式（痛点、灵感、算法、极限对比）/ journal 三段式（痛点、空白与核心发现、论证链）/ other 通用式（综述、benchmark、数据集、立场）/ scan 三行
- **原图**，从 PDF 裁下、逐块解释，不重画
- **符号表**、**每个损失项在防哪种失败**、**指标定义与互补盲区**
- **湿实验六项**：n、重复、检验、离散度、p、多重比较校正
- **与已读的关系**
- 固定**四行元信息头**：标题 / 发表 / 类型 / citekey
- 做了观潮时的 **`## 定位`**：潮 / 世代 / 课题组 / 开创了什么 / 置信度与推翻条件

三条贯穿全篇的规则：

- **复述，不评判。** 覆盖度的事实照记（`原文未报告`、`未给置信区间`），价值判断不写。
- **每个数字带出处。** 后面跟 `Fig. 3` / `Table 2` / `Sec. 4.2`。图裁不画，表格重打成 markdown。
- **不静默丢字段。** 映射对不上就停下报告。

## 安装

```bash
git clone <repo> ~/.cursor/skills/sounding
```

裁图脚本需要 Python 3 + `pymupdf`：

```bash
pip install pymupdf
```

## 首次使用

配置不随仓库走。第一次调用时 skill 会问你三项，也可以自己跑引导脚本：

```bash
python scripts/setup.py
```

它问的只有这些，其余能探测就探测：

| 要问的 | 怎么答 |
| --- | --- |
| Zotero 数据目录 | 含 `zotero.sqlite` 的目录，脚本会先自动找 |
| Zotero API key | https://www.zotero.org/settings/keys 新建，勾 library + write（写子笔记再加 notes）。可以跳过 |
| Notion Papers 库链接 | 粘整条 URL 即可 |

`user_id` 由脚本拿 key 反查。跑完 `python scripts/setup.py --check` 能看到每一项的真实状态（数据库能读多少条、key 属于谁）。

## 配置

`config.example.json` 是出厂默认，`config.local.json` 是你自己的副本（git 忽略）。通常不用手改，用 `scripts/setup.py` 生成即可；要手改就：

```bash
cp config.example.json config.local.json
```

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

环境变量可覆盖文件：`ZOTERO_DB`、`ZOTERO_DATA_DIR`、`NOTION_PAPERS_DB`、`NOTION_PAPERS_DS`。

`config.local.json` 是你自己的副本，不进版本库。

### 库里需要的字段

| 概念 | 类型 | 说明 |
| --- | --- | --- |
| `title` | title | 论文标题 |
| `venue` | text | 刊名或会议名，不带年份 |
| `year` | number | |
| `type` | select | `conference` / `journal` / `review` / `other` |
| `citekey` | text | Zotero citationKey，幂等主键 |
| `read_level` | select | `扫` / `精读` |
| `problem` | multi-select | 问题域 |
| `method` | multi-select | 方法族 |
| `data` | multi-select | 数据类型 |
| `verdict` | select | `值得跟进` / `存档` / `仅作背景` |
| `topic` / `domain` / `wave` / `group` | relation | 选填，只在用户点名时连 |
| `confidence` | select | `猜测` / `有据` / `复核`，观潮用 |
| `role` | select | `创世` / `追随` / `旁支` / `方法件` / `未定`，观潮用 |
| `generation` / `generation_name` / `opened` | relation / text / text | 世代与「从 X 变成 Y」，观潮用 |
| `borrowed_from` / `followers` | relation | 借自谁、谁在跟，观潮用 |

三族标签是三个独立的多选字段，因为 Notion 多选不支持嵌套。Zotero 侧标签是扁平的，靠 `p:` / `m:` / `d:` 前缀保留维度。

任何映射字段在库里不存在，skill 停下报告，不丢值。

## Notion 工具

Notion 读写走 Notion MCP。工具不可用时，笔记写到 `notes/<citekey>.md`，并告诉你待上传。

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
reference-wave.md        观潮判据：潮、世代、课题组、开创了什么
reference-topic.md       课题页模板
config.example.json      出厂默认
config.local.json        你自己的副本（git 忽略）
scripts/zotero_find.py   在本地 Zotero 库里查条目与 PDF
scripts/zotero_write.py  把标签与 Notion 链接写回条目（需要 API key）
scripts/setup.py         首次配置：探测、校验、写 config.local.json
scripts/fig.py           在页面上找出图片块并裁出
scripts/config.py        脚本共用的配置读取
scripts/check_config.py  校验配置能对上真实数据库
```

`reference-evidence.md` 存在的意义是规则只写一次：模板只放结构与验收，规则改了只改一处。

## 脚本

```bash
python scripts/zotero_find.py "attention is all you need"
python scripts/zotero_find.py --citekey yangArbovirusSuppressionLectin2025 --pdf
python scripts/zotero_find.py --list-collections
python scripts/zotero_find.py --tags

python scripts/zotero_write.py --citekey key --show
python scripts/zotero_write.py --citekey key --tags p:x,m:y,d:z --extra https://www.notion.so/…

python scripts/fig.py scan paper.pdf --page 3
python scripts/fig.py crop paper.pdf --page 3 --bbox 197,72,415,394 --out fig1.png --dpi 220

python scripts/check_config.py    # 需要 NOTION_TOKEN
```

`zotero_find.py` 读的是数据库副本，Zotero 开着也能用。`fig.py scan` 会打印图片块的边界框和同页的图注块，图和图注可以对上号。

## 判类型

看**论文的结构**，不看发在哪。Nature、NeurIPS、TPAMI 都不在这条线的任何一边。

| 信号 | 类型 |
| --- | --- |
| Figure 1 是模块图，章节是 Method/Experiments | `conference` |
| Figure 1 是流程或机制图，章节是 Results/Discussion | `journal` |
| 机制示意图与独立方法模块并存（干湿结合） | `journal` |
| 都不是——综述、benchmark、数据集、立场论文 | `other` |

## 观潮

单篇笔记回答「这篇做了什么」，观潮回答「这篇在整片潮里站在哪」。

三条判据同时成立才算一波潮：有台阶、有迁移、有沉淀。缺哪条，就用缺的那条命名它（渐进线 / 单组自嗨 / 一时热闹）。世代、课题组、开创了什么，以及置信度与推翻条件，写法见 `reference-wave.md`。

## License

MIT.

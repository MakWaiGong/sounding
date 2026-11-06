# 落库：飞书后端

飞书是 `publish.py` 的第二条发布路径：一篇笔记 = 多维表格里一行 `Papers` + 一篇云文档正文。和 Notion 那条路径的区别在**谁解析 markdown**：Notion MCP 直接吃 notion-flavored markdown，飞书要自己转成 block，所以这件事必须由脚本做，不能由 agent 手搓 API。

## 1 一条命令

```bash
python scripts/publish.py notes/<citekey>.md \
  --data PDB,scRNA-seq \
  --zotero
```

- `--data`：数据类型，逗号分隔，值是 `reference-tags.md` 里的词。缺了就是缺字段，脚本报出概念名后停（`reference-library.md` §3），不会静默丢。领域不在命令行里——它是表里由用户手填的链接列，脚本不写不查。
- `--read`：`扫` / `精读`，默认 `精读`。
- `--zotero`：写 `Extra: Feishu: <url>` 和 `d:` 前缀的数据类型标签。条目不在库里时跳过并说明，不新建条目——建条目是 `zotero_add.py` 的事。
- `--dry-run`：只打印计划（解析出的属性、要不要新建、缺什么），不写。
- `--allow-partial`：确实要留空发布时才用；正常流程不用。

退出码非零表示没发布成功，读 stderr 那一行。

## 2 脚本做了什么

1. 解析笔记头四行（`标题` / `发表` / `类型` / `citekey`）、`## 定位素材` 里 `原文：` 行的 DOI，速览第 1 行起的编号行；`发表` 末尾的 `(2026)` 是年份，`年份` 字段由它派生。`去留` 不再进表，只留在笔记速览第 10 行。
2. 按 citekey **精确**查 `Papers`：1 行更新、0 行新建、多行停下报 record_id（`reference-library.md` §4）。
3. 有记录就复用 `notes/.feishu-index.json` 里的 doc_id，先清空正文再整篇重写——笔记是重建的，不是打补丁。
4. 图从 `notes/figs/<citekey>/manifest.json` 取，内联到正文首次提及 `Fig. N` 的位置（跳过 `Extended Data` / `Supplementary`，没被提到的落文末）。
5. 回读：`raw_content` 文本长度、块数、图片块数、表格数四项都要对上，否则非零退出。
6. 通过后写索引（doc_id / url / record_id / rev）与表格里的 `笔记` 链接字段。

`rev` 是笔记 md 的 sha1 前 12 位；同一份笔记重跑是幂等的，改过内容重跑会覆盖同一篇文档。

## 3 库结构

| 位置 | 内容 |
| --- | --- |
| 多维表格 base | 「sounding 论文库」，token 在 `notes/.feishu-workspace.json` |
| 表 `论文 Papers` | 19 字段：12 标量/多选 + 7 关联 |
| 表 `课题` / `领域` / `潮` / `课题组` | 关联目标，只有已存在的行才连 |
| 文件夹 `sounding` | 笔记文档本体，`folder_token` 同上 |

首次配置：

```bash
python scripts/feishu_setup.py --apply    # 建 base、文件夹、五张表、关联字段，写回 config.local.json
python scripts/feishu_setup.py --probe --apply   # 记录 CRUD + 文档写入 + 传图 + 回读 + 清理
```

`--apply` 之前先看 `--help`；只有 `--apply` 才会真的建，只有 `--probe` 才会写测试记录（探针 citekey 是 `__sounding_probe__`，跑完自动删）。

## 4 文档形态

- 正文由 `blocks/convert`（`content_type: markdown`）转块后插入；返回的 `block_id` / `parent_id` / `children` 必须剥掉再插，否则 `1770001`。
- **速览做成 callout**（`block_type 19`）：`background_color` / `border_color` 用 light blue（5）、`emoji_id: "pushpin"`。callout 创建时自带 1 个空 text 子块，第一行复用这个子块、其余走 `append_block_children`；`去留` 行按值着色（值得跟进=绿 4 / 存档=灰 7 / 仅作背景=橙 2）——这一行只存在于笔记与 callout 里，不进库表。
- **公式**：笔记里直接写 `$...$`（行内）或 `$$...$$`（独占一行）。`blocks/convert` 会把它转成 text 块里的 equation 元素（`{"equation": {"content": ...}}`），不需要新块类型；表格 cell、callout 首行、图注都走同一个 `inline_elements` 切分。
- **图注**：`manifest.json` 每张图可加一条 `图注：Fig. N —— <解释>` 写在 `## 定位素材` 下，渲染成图片正下方的灰色斜体 text 块（`text_color: 7`），块内容形如 `Fig. N　<解释>（原 PDF 第 N 页）`；没有图注就只写 `Fig. N（原 PDF 第 N 页）`。图注行不会重复进正文。
- **标题着色**：convert 出的 heading 块做后处理，给每个 text_run 设 `text_element_style.text_color`——H1/H2=蓝 5、H3=紫 6、H4=灰 7。
- **图内联**：按每个 fig 的 `Fig. N` / `Figure N` 首次提及插位（`Extended Data Fig.` / `Supplementary` 跳过），同一行提多张就按提及顺序依次插，没被提到的落文末。
- **表格不能从 convert 结果直接插**（`1770029 block not support to create`）。表格一律自己建空表（`block_type 31`，`row_size` / `column_size` / `header_row`），再用 `blocks/batch_update` 一次性把 `R*C` 个 cell 里自带的空 text 块改掉。**单次建表 `row_size` 上限 9**（≥10 报 `1770001`）；更大的表先建 9 行，再用一次 `batch_update` 的 `insert_table_row` 补齐。
- 图片三步，缺一步就是空白块：建空图片块（`block_type 27`）→ `medias/upload_all`（`parent_type=docx_image`、`parent_node` = **图片块 id**、`extra.drive_route_token` = 文档 id）→ `PATCH` 块 `{"replace_image": {"token": <file_token>}}`。
- 清正文用 `children/batch_delete`（`start_index 0`，`end_index` 给超过实际块数的值也接受）。
- 图片块、表格块的 token 只有回读才能证明写进去了；`code: 0` 不代表成功。

## 5 已知坑（带真实错误码）

| 现象 | 原因 | 正解 |
| --- | --- | --- |
| 文档顺序整体反转、图跑到最前 | `children` 接口 `index: 0` 是**插到最前面** | 追加必须传 `index: -1` |
| 追加图片块后块里 token 为空、`code: 0` | 载荷键名错，服务端静默忽略 | 三步流程，最后 `replace_image` |
| `1770009` / `1770029` | 尝试直接创建只读块（cell、带 children 的表） | cell 由建表产生；表先建空再 `batch_update` |
| `1770001 invalid param`（建表） | 单次建表 `row_size` ≥ 10 | 先建 9 行，再用 `insert_table_row` 补齐 |
| `1770024 invalid operation` / `99992402 field validation failed`（改文本） | `batch_update` 操作名错（如 `update_text`），或 element 缺 `text_run` 包裹 | 用 `update_text_elements`，元素形如 `{"text_run":{"content","text_element_style"}}` |
| `99992402 field validation failed` | 读记录条件写成了 dict 而不是 `(field, operator, values)` 三元组 | 见 `feishu_api.search_records` |
| `1061003 not found` | 删文件时 `type` 参数与真实类型不符（docx / file） | 按列表里的 `type` 传 |
| `99991672` / `1069902` | 应用没有该文档/文件夹权限 | 文档右上「添加文档应用」，或 `grant_tenant_access` |
| 段落顺序错乱、某段被挪到文首（`code: 0`，无报错） | `blocks/convert` 返回的 `blocks` 是**无序 map**，文档顺序只在 `first_level_block_ids` 里 | 按 `first_level_block_ids` 重排后再 `append_children` |

## 6 延迟基线（同一台机器，连接复用）

| 动作 | p50 |
| --- | --- |
| `raw_content` 读回 | 98–390ms |
| 文件列表 | 430–510ms |
| 建文档 | ~960ms |
| 整篇 94 块 + 4 表 + 4 图 | 18.6s |

写一篇的耗时主要在图片上传和表格填充；表格填充已从「每 cell 两次请求」改成「一次树读 + 一次 batch_update」，整篇从 77s 降到 18.6s。要再快只能并行上传图片。

## 7 什么时候停

- citekey 命中多行 → 停，报 record_id，用户先合并。
- 必填属性缺 → 停，报概念名与字段名。
- 回读四项有任一不符 → 停，把实测数字打出来（不要只说「可能没写进去」）。
- 权限错误 → 停，说清是哪个文档要加哪个应用；不要反复重试同一个 403。

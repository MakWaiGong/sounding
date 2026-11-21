# 落库：飞书后端

**归档专用**：仅明确保存云端时执行本文件的配置、探针、写入、上传与回读。普通阅读默认文本，不因归档条件受阻；归档复用已有完整解读，不重做 sounding。

现有用户库只按真实 schema 操作，不跑 `--apply` 恢复已删列。旧探针若会写读法或已删字段则不直接执行；隔离探针只操作自己新建的临时记录，省略领域／读法并清理。实际没有“数据类型”列时不传 `--data`；仅在 dry-run 确认唯一缺项为该已删概念、其他必填元信息完整时，才用 `--allow-partial` 兼容发布，不能放过其他真正缺项。

飞书是 `publish.py` 的第二条发布路径：一篇笔记 = 多维表格里一行 `Papers` + 一篇云文档正文。和 Notion 那条路径的区别在**谁解析 markdown**：Notion MCP 直接吃 notion-flavored markdown，飞书要自己转成 block，所以这件事必须由脚本做，不能由 agent 手搓 API。

## 0 先用私有配置验证应用身份

1. 用 `scripts/config.py` 解析实际配置路径：`SOUNDING_CONFIG` 优先，未设置才读取当前 skill 目录的 `config.local.json`。飞书字段经 `config.feishu()` 应用环境变量覆盖；不要只看公开示例，或在另一个 clone 里寻找密钥。
2. 复用用户自己的 `app_id` / `app_secret`，也支持 `FEISHU_APP_ID` / `FEISHU_APP_SECRET`。Secret 与访问令牌只用于本机鉴权和官方 API 请求，不写公开文件，不输出到聊天或日志；检查结果只报告路径、字段存在性及脱敏错误。
3. **应用 API 与浏览器登录态不同。** 通过现有 API 客户端获取应用令牌并只读检查目标库/表，确认资源 ID、真实字段与权限；浏览器的访客页、扫码页或用户会话 401 不能代替应用权限检查。只有应用 API 实际返回缺凭据、scope 或资源权限错误，才按对应原因处理，不能先要求用户扫码。
4. 已有目标库先复用配置与本地 workspace/index，并核对正文关联方式。下面的 `feishu_setup.py --apply` 用于明确授权的初始化；不能因没找到当前 clone 的 workspace 就擅自新建另一套库。只读检查通过不代表写入/图片已通过，仍须按发布流程回读同一记录与正文。
5. 在本机私有配置 `feishu.editor_open_ids` 中填写经用户明确确认的、属于当前飞书应用的本人账号 open_id 列表。应用 ID 不等于用户 open_id；不能猜测账号归属或把账号 ID 输出、截图或提交。列表缺失、为空、重复或格式无效时，真实发布必须在创建或改写文档之前停止；dry-run 仍可只读预览。
6. 论文领域的 domain 内容和关系仅由用户手写；读法也仅由用户填写且默认留空，“精读”表示用户在笔记生成后已亲自阅读。自动创建/更新负载省略这两个字段并保留已有值；不要与飞书租户域名配置混淆。

## 1 一条命令

```bash
python scripts/publish.py notes/<citekey>.md \
  --data PDB,scRNA-seq \
  --zotero
```

- `--data`：数据类型，逗号分隔，值是 `reference-tags.md` 里的词。缺了就是缺字段，脚本报出概念名后停（`reference-library.md` §3），不会静默丢。领域不在命令行里——它是表里由用户手填的链接列，脚本不写不查。
- `--read`：旧命令兼容入口，参数会忽略；“读法”仅由用户填写。
- `--zotero`：写 `Extra: Feishu: <url>` 和 `d:` 前缀的数据类型标签。条目不在库里时跳过并说明，不新建条目——建条目是 `zotero_add.py` 的事。
- `--dry-run`：只打印计划（解析出的属性、要不要新建、缺什么），不写。
- `--allow-partial`：确实要留空发布时才用；正常流程不用。

退出码非零表示对应笔记未完成；查看 stderr 或批量结果中的逐篇状态。

### 多篇并发与恢复

```bash
python scripts/publish_batch.py notes/<key1>.md notes/<key2>.md --workers 3
```

- 批量入口对已准备和逐篇校验的笔记受控并发；批量前识别重复 citekey／DOI，`--workers` 不得超过 `paper_concurrency`。结果按输入顺序列出状态和成功笔记的飞书链接；逐篇查看正文回读结果，不能因为部分成功就报告全部成功。`--allow-partial` 只在本文件开头规定的已删字段情形使用；`--zotero` 是可选回链，回链待处理不改变飞书正文完成状态。
- 私有配置的 `feishu.paper_concurrency` 限制同一目标库内同时发布的不同论文数，`feishu.image_concurrency` 限制每篇同时上传的图片数；均默认为 3，取值为整数 1–8。达到论文限额或同篇已有任务时，后发任务报忙退出，不自动重复提交；不同运行中的论文限额配置不一致也会报忙。
- 同一操作系统用户的所有 sounding 副本必须使用同一个恢复目录：默认 `~/.sounding/feishu`，可统一通过 `SOUNDING_FEISHU_STATE_DIR` 指定。该机制只协调遵守新协议的本机进程，不保护旧版本、不同机器或用户在飞书里的手工编辑。
- 恢复目录包含持久化注册表、锁和图片检查点；检查点可能包含笔记正文的块快照、文档 ID 和图片引用，应按个人笔记保护，**不要当临时文件清理**。已确认上传的图片在重试时会回读验证并复用。
- 创建、文档骨架或上传结果不确定时，脚本会停止并保留状态：检查飞书实际内容，先备份状态，再根据错误提示人工修复；不要删除检查点或盲目重新发布。明确的限流拒绝可按错误提示稍后重试；长于等待上限的 Retry-After 不会被缩短。

## 2 脚本做了什么

1. 解析笔记头（兼容 `发表`，也支持 `期刊`；`发表时间：YYYY-MM` 为可选头）及 `## 定位素材` 的 DOI。飞书列名是 `期刊` / `发表时间`；只有年份时写年份文本，已存在的完整年月不会被年份值降级。年份冲突在创建或重写正文之前报错。速览按正文骨架浓缩，不固定第十行为 `去留`。
2. 真实发布前验证私有配置中的本人账号；再按 citekey **精确**查 `Papers`：1 行更新、0 行新建、多行停下（`reference-library.md` §4）。
3. 按目标库和论文身份获取本机跨进程锁，读写 `notes/.feishu-index.json` 时在锁内重新读取并合并当前论文；文档、记录的创建意图和恢复身份保存在共享恢复目录。结果不确定须停止核查，不凭旧索引重复创建。
4. **先授予本人管理权限并回读。** 对每个指定账号单独授予 `full_access`（可管理，不是转让所有权），回读确认后才改写正文。不得回显或提交账号 ID，不改其他协作者；授权失败不算归档完成。 发布期间不要同时通过其他方式修改同一文档的协作者；飞书接口没有原子条件写入，前后回读不能排除所有并发冲突。
5. 从 `notes/figs/<citekey>/manifest.json` 取图，发布前校验所有清单图片可读且图号唯一（如 `fig1`、`fig2`）；缺图、重复或不支持的图号会停止，不静默省略。先按顺序构建正文、图片占位和图注，再用独立连接受限并发上传，逐个挂载并回读。每张图插到**解释它的叙述之后**，不把表格、速览、参考文献或定位素材当图像锚点。已有正文会被整篇重写，手工修改应先保存。
6. 新建文档默认使用 `{"link_share_entity": "anyone_readable"}`，持链接者只读；复用已有文档时保留其原有公开范围，不擅自将私有文档公开。不要为了让本人编辑而给所有持链接者编辑权限。然后创建或更新表格记录。
7. 回读 `raw_content` 文本、块、图片和表格数量；全部通过后将 doc_id / url / record_id / rev 写入完成索引，否则不得报告成功。

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
- **速览做成 callout**（`block_type 19`）：`background_color` / `border_color` 用 light blue（5）、`emoji_id: "pushpin"`。callout 创建时自带 1 个空 text 子块，第一行复用这个子块、其余走 `append_block_children`；若出现与正文一致的 `去留` 行，则按值着色（值得跟进=绿 4 / 存档=灰 7 / 仅作背景=橙 2）——这一行只存在于笔记与 callout 里，不进库表。
- **公式**：笔记里直接写 `$...$`（行内）或 `$$...$$`（独占一行）。`blocks/convert` 会把它转成 text 块里的 equation 元素（`{"equation": {"content": ...}}`），不需要新块类型；表格 cell、callout 首行、图注都走同一个 `inline_elements` 切分。
- **图注**：`manifest.json` 每张图可加一条 `图注：Fig. N —— <解释>` 写在 `## 定位素材` 下，渲染成图片正下方的灰色斜体 text 块（`text_color: 7`），块内容形如 `Fig. N　<解释>（原 PDF 第 N 页）`；没有图注就只写 `Fig. N（原 PDF 第 N 页）`。图注行不会重复进正文。
- **标题着色**：convert 出的 heading 块做后处理，给每个 text_run 设 `text_element_style.text_color`——H1/H2=蓝 5、H3=紫 6、H4=灰 7。
- **图内联**：按每个 fig 的 `Fig. N` / `Figure N` **叙述提及**插位——非叙述行（`PDF 与来源`/速览/符号表/指标/与已读的关系/待查/定位素材/实验清单/参考文献/表格行/代码块）里的提及一律不作锚点，图插在提及所在段落或列表项的末尾；同一段提到多张图时按提及顺序分散到各自叙述处，不堆叠；`Extended Data Fig.` / `Supplementary` 跳过，正文没提过的图落文末。
- **表格不能从 convert 结果直接插**（`1770029 block not support to create`）。表格一律自己建空表（`block_type 31`，`row_size` / `column_size` / `header_row`），设置 `column_width` 以尽量铺满桌面正文，再用 `blocks/batch_update` 一次性把 `R*C` 个 cell 里自带的空 text 块改掉。**单次建表 `row_size` 上限 9**（≥10 报 `1770001`）；更大的表先建 9 行，再用 `insert_table_row` 补齐。飞书列宽是固定像素，不是百分比，窄窗口可能需要横向滚动；不得将其描述为自动响应式全宽。
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

这组数字是历史串行基线，不代表当前并发实现的实测性能。当前图片数据可受限并发上传，文档骨架和挂载回读仍按顺序处理；实际收益取决于网络、文档结构和飞书限流，不能用并发数直接推算加速倍数。

## 7 什么时候停

- citekey 命中多行 → 停，报 record_id，用户先合并。
- 必填属性缺 → 停，报概念名与字段名。
- 回读四项有任一不符 → 停，把实测数字打出来（不要只说「可能没写进去」）。
- 权限错误 → 停，说清是哪个文档要加哪个应用；不要反复重试同一个 403。

## 8 非阻塞 PDF 归档与可选 Zotero

正文和内嵌图片回读成功后，笔记即可完成；源 PDF 上传是独立归档步骤，不是图片上传。先读取已有表的真实 schema，使用实际附件字段/文件机制，不猜字段、不改表结构。当前发布脚本不自动执行源 PDF 归档。上传完成需同一记录回读到真实 attachment/file token；未完成或失败只记 `archival_pending`。本地源/重命名 PDF 始终保留到验证成功，之后也不自动删除。

`run_state.py init --backend feishu` 默认要求 `pdf_prepared`、`note_validated`、`feishu_written`、`images_verified`，并独立跟踪 `pdf_uploaded`；Zotero 不在默认步骤中。`note_complete`、`publication_complete` 与兼容的 `complete` 只看正文和图片；`archival_pending`/`archival_complete` 单独报告。旧状态中的显式 Zotero 步骤继续保留，但不阻塞正文完成。

飞书正文回执绑定完整 destination、source/note SHA256、record_id/readback_record_id、document_id/readback_document_id、实际正文 document_url、readback_checked=true 和 domain_preserved=true；对应 ID 必须一致。图片回执绑定相同正文及实际 block_id；PDF 回执绑定当前 source SHA256、destination、同记录回读及 attachment_token/file_token。回执仅记录真实观察，脚本不会联网代为证明成功。

`--zotero` 是显式可选回链，不自动创建条目、集合成员或上传 PDF。需要旧版 Zotero PDF 归档时另行明确选择；缺条目或 Zotero 操作失败不改变笔记完成状态。

## 9 发表时间与期刊

飞书论文表的“期刊”为文本，保留原“发表”列的期刊/会议内容；“发表时间”为文本，明确月份时写 YYYY-MM。历史仅有年份时保留 YYYY，原为空仍为空，不猜月份或某一天。旧笔记头“发表：刊名 (年份)”继续兼容；也支持“期刊：刊名”和可选头“发表时间：YYYY-MM”，冲突年份或非法月份会拒绝。内部整数 year 保留给可选书目回链。

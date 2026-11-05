# Notion MCP：安装、配置、验收与发布阻塞

Notion 是 sounding 笔记的发布目标，不是可选导出格式。本地 `notes/` 只放工作稿、裁图、校验结果与运行记录；它不能代替 Notion 页面，也不能作为任务完成的依据。

官方 Remote Notion MCP 地址固定为：

```text
https://mcp.notion.com/mcp
```

`agents/openai.yaml` 已把它声明为 skill 的必需依赖。客户端若不读取 skill dependency，就在客户端中手动添加同一地址并完成 OAuth；不要退回已停止积极维护的本地 `@notionhq/notion-mcp-server`，除非用户明确要求自托管。

---

## 1 安装与连接

Codex CLI：

```bash
codex mcp add notion --url https://mcp.notion.com/mcp
codex mcp login notion
codex mcp list
```

没有本地浏览器时：

```bash
codex mcp login notion --no-browser
```

在 ChatGPT／Codex 图形界面中，安装 skill 后检查 Notion dependency 是否显示为待连接，完成 Connect／OAuth，再重启或刷新工具目录。其他 MCP 客户端使用同一 streamable HTTP 地址，并按该客户端的方式完成 OAuth。

凭证不写进仓库、`config.local.json`、命令示例或聊天正文。连接使用 OAuth；本地配置只保存 Papers 数据库与 data source 的 ID。

---

## 2 首次配置

1. 连接 Notion MCP。
2. 让用户给出 Papers 数据库页面，或在其明确指定的范围内查找。
3. fetch 数据库，取得数据库 ID 与 `<data-source url="collection://...">`。
4. 把两者写入 `config.local.json`：

```json
{
  "notion": {
    "papers_database_id": "<database uuid>",
    "papers_data_source_id": "collection://<data source uuid>"
  }
}
```

5. fetch data source 的真实 schema，按 `reference-library.md` 核对字段名、类型与 select 选项。不得用 `config.example.json` 反造 schema。
6. 运行 `python scripts/check_config.py --schema-json <MCP 导出的 schema.json>`。离线配置通过不等于 MCP 通过。

---

## 3 工作区级安装验收：读写闭环

安装 skill 或更换 Notion 工作区后，必须在正式分析论文前完成一次探针。**探针属于工作区，不属于论文；缓存有效时绝不逐篇重跑。** 全部通过才算 Notion 已配置：

1. **工具发现**：能看到 fetch、query、create、update，以及 delete／archive 能力。
2. **认证**：工具不返回未连接、OAuth 过期或权限不足。
3. **读库**：fetch 配置的 Papers 数据库成功，并取得目标 data source。
4. **读 schema**：fetch data source 成功，字段与 `config.local.json` 映射一致。
5. **写探针**：在目标 Papers data source 创建一条标题带 `sounding-mcp-probe` 与时间戳的测试记录；只填 schema 允许的最小字段。
6. **回读与更新**：fetch 测试记录，更新一个无副作用的测试属性或正文标记，再次 fetch 确认变化可见。
7. **清理**：删除或归档测试记录，并确认查询不再把它当作正常论文。

记录每一步的真实 tool response、page ID 和清理结果。只做工具发现或只读 schema 不能证明发布链路可用。若客户端没有可恢复的删除／归档能力，在写探针前告诉用户会留下什么测试记录；不能清理时不要静默制造孤页。

探针通过后把结果写进 `notes/notion-probe.json`，再记录工作区缓存：

```bash
python scripts/notion_workspace.py record --evidence notes/notion-probe.json
python scripts/notion_workspace.py status
```

探针回执至少含当前 `database_id`、`data_source_id`、`probe_page_id`，以及值为 `true` 的 `read_checked`、`write_checked`、`update_checked`、`cleanup_checked`。脚本只核对并缓存 agent 已观察到的结果，不接触 Notion，也不能伪造探针成功。

每篇发布开始只查本地缓存。仅在以下情况重跑探针：缓存不存在；数据库、data source、字段映射或选项配置变化；换工作区；实际 MCP 返回 OAuth、权限、schema 或写入错误。观察到这类错误时先执行：

```bash
python scripts/notion_workspace.py invalidate --reason "<实际错误>"
```

超时、单张图片上传失败或一次页面写入失败不自动等同于工作区失效；先按 page ID／citekey 回读并重试未完成子步骤。

---

## 4 每篇笔记的快速发布与验收

工作区缓存有效后，逐篇路径保持短且可恢复：

1. 本批次首次写入时 fetch 一次 data source schema；同一 data source、同一会话／批次内复用，出现 schema 错误才重新 fetch。
2. 按 citekey 精确查询。批量论文的查询可并行；每篇仍单独判断零、一或多命中。
3. 用一次 create/update 写属性与完整正文，不把正文拆成逐段远端调用。
4. 所有裁图先在本地完成。批量申请上传槽，再以 3–4 个并发上传；保留每个响应返回的 `markdown_source`，不要为了同一文件再调用 attachment 转换。
5. 收齐成功的图片引用后，用一次页面更新把图片放到正文对应位置；不要逐图执行“上传—插入—回读”。
6. 最后只回读一次同一 page ID，统一核对 citekey、正文、字段与全部图片引用；失败时只续做缺失子步骤。

不同论文的本地分析和图片上传可以并行；同一篇的幂等查询必须先于建页。并发调用逐项保存成功响应，不能因一个失败而丢掉其他已完成上传。

sounding 笔记只有满足以下条件才算发布完成：

- 按 citekey 精确查询后更新唯一行，或在零命中时新建一行；
- 正文、属性和图片写入目标页面；
- 回读同一 page ID，确认 citekey、正文、字段和图片引用；
- 保存 Notion page ID／URL 的成功回执；
- 再把该 URL 回写 Zotero `Extra`。

Notion 与图片回执在 `timings_ms` 里分别记录实际毫秒耗时。至少区分 `schema_fetch`、`citekey_query`、`page_write`、`upload_slots`、`upload_bytes`、`images_attach`、`readback`；这组计时用于诊断，不改变完成标准。

创建请求超时或返回状态不确定时，先按 citekey 和预期 page ID 回读，确认是否已创建；不要盲目重试造成重复行。

---

## 5 工具不可用时

按顺序处理：

1. 查工具目录，确认是否只是未加载或未授权。
2. 安装／连接 `https://mcp.notion.com/mcp`，完成 OAuth。
3. 运行第 3 节的读写探针。
4. 探针通过后继续发布原笔记。

若因工作区策略、网络或用户尚未完成 OAuth 而无法继续，保留本地工作稿和运行状态，明确报告：

```text
状态：发布受阻（未完成）
已完成：本地笔记与机械校验
缺少：Notion MCP <安装／OAuth／权限／schema／写入> 的哪一步
恢复点：<通过探针后从 notion_written 继续>
```

此时不得说「笔记已完成」「已经落库」，不得把本地文件当成交付成品，也不得提前回写一个不存在的 Notion URL。阻塞解除后只续做未完成的远端步骤，不重建已经校验过且未变化的本地材料。

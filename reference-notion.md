# Notion MCP：安装、配置、验收与发布阻塞

Notion 是精读笔记的发布目标，不是可选导出格式。本地 `notes/` 只放工作稿、裁图、校验结果与运行记录；它不能代替 Notion 页面，也不能作为任务完成的依据。

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

## 3 安装验收：读写闭环

安装 skill 或更换 Notion 工作区后，必须在正式精读前完成一次探针。全部通过才算 Notion 已配置：

1. **工具发现**：能看到 fetch、query、create、update，以及 delete／archive 能力。
2. **认证**：工具不返回未连接、OAuth 过期或权限不足。
3. **读库**：fetch 配置的 Papers 数据库成功，并取得目标 data source。
4. **读 schema**：fetch data source 成功，字段与 `config.local.json` 映射一致。
5. **写探针**：在目标 Papers data source 创建一条标题带 `sounding-mcp-probe` 与时间戳的测试记录；只填 schema 允许的最小字段。
6. **回读与更新**：fetch 测试记录，更新一个无副作用的测试属性或正文标记，再次 fetch 确认变化可见。
7. **清理**：删除或归档测试记录，并确认查询不再把它当作正常论文。

记录每一步的真实 tool response、page ID 和清理结果。只做工具发现或只读 schema 不能证明发布链路可用。若客户端没有可恢复的删除／归档能力，在写探针前告诉用户会留下什么测试记录；不能清理时不要静默制造孤页。

---

## 4 每篇笔记的发布验收

精读笔记只有满足以下条件才算发布完成：

- 按 citekey 精确查询后更新唯一行，或在零命中时新建一行；
- 正文、属性和图片写入目标页面；
- 回读同一 page ID，确认 citekey、正文、字段和图片引用；
- 保存 Notion page ID／URL 的成功回执；
- 再把该 URL 回写 Zotero `Extra`。

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

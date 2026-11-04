# 工程工具：缓存、裁图、校验与续做

这些工具减少重复提取与重复写入，不替代阅读全文、看原图或判断证据含义。默认中间文件放在 git 忽略的 `notes/`；可删除后重建。

## 1 PDF 只提取一次

```bash
python scripts/pdf_prepare.py paper.pdf
python scripts/pdf_prepare.py paper.pdf --pages 1,3-5
python scripts/pdf_prepare.py paper.pdf --search "ablation" --search "loss"
```

输出 JSON manifest。页码从 1 开始；坐标是未旋转页面的 PDF point。manifest 带 PDF SHA-256、文字层状态、逐页原文、文字块、图片块、完整图注候选与章节候选。图注和章节边界是启发式候选，引用前仍需查看原 PDF。

缓存键包含 PDF 字节指纹、提取器版本和 PyMuPDF 版本。相同字节从不同路径打开会复用缓存；PDF 字节、提取器版本、库版本变化，或缓存损坏时会重建。`--rebuild` 可强制重建，`--cache-dir` 可更换位置。脚本不 OCR、不改原 PDF；精读遇到 `text_layer_status=absent` 应停止。

## 2 裁原图

先查看候选：

```bash
python scripts/fig.py scan paper.pdf --json > scan.json
python scripts/fig.py page paper.pdf --page 3 --out page3.png
python scripts/fig.py crop paper.pdf --page 3 --bbox 197,72,415,394 --out fig2.png --dpi 220
```

单图输出已存在时默认拒绝覆盖；确认目标后加 `--overwrite`。批量裁图清单：

```json
{
  "pdf_sha256": "<64 位源 PDF 指纹>",
  "crops": [
    {"page": 3, "figure_number": "2", "bbox": [197, 72, 415, 394], "out": "fig2.png", "dpi": 220, "pad": 6}
  ]
}
```

```bash
python scripts/fig.py batch paper.pdf --manifest crops.json --out-dir notes/figs/<citekey>
```

批处理先校验整批源指纹、页码、坐标、输出重名与越界，再渲染；任一项预检或渲染失败，不发布前面已裁的部分。结果 JSON 含每张图片的 SHA-256，供上传回读与运行记录使用。裁图区域必须人工看过；表格仍重打为 Markdown。

## 3 机械检查笔记

```bash
python scripts/validate_note.py notes/<citekey>.md --read-level deep \
  --source-manifest notes/.pdf-cache/<manifest>.json --citekey <citekey> \
  --output notes/<citekey>.validation.json

python scripts/validate_note.py notes/<citekey>.md --read-level scan --citekey <citekey>
```

它检查元信息、精读十行速览、类型对应的主要章节、关系与待查、图片路径、来源页码和可识别图号。未知图号只警告，因为候选图注可能漏提；越界页码与无文字层精读会报错。`valid=true` 只表示机械结构通过，不表示某句话受到论文证据支持。

## 4 中断后只续做未完成步骤

长精读、Notion 图片上传或 Zotero 回写可能部分失败。初始化运行记录：

```bash
python scripts/run_state.py init --file notes/<citekey>.run.json \
  --citekey <citekey> --source paper.pdf --note notes/<citekey>.md
python scripts/run_state.py status --file notes/<citekey>.run.json
```

默认步骤为 `pdf_prepared,note_validated,notion_written,images_verified,zotero_written`。只做本地任务或扫读时可用 `--steps` 选择适用步骤；未配置的步骤不会被说成已完成。

记录实际产物：

```bash
python scripts/run_state.py record --file notes/<citekey>.run.json \
  --step pdf_prepared --evidence notes/.pdf-cache/<manifest>.json
python scripts/run_state.py record --file notes/<citekey>.run.json \
  --step note_validated --evidence notes/<citekey>.validation.json
python scripts/run_state.py record --file notes/<citekey>.run.json \
  --step notion_written --evidence notes/<citekey>.notion.json
python scripts/run_state.py record --file notes/<citekey>.run.json \
  --step images_verified --evidence notes/<citekey>.images.json
python scripts/run_state.py record --file notes/<citekey>.run.json \
  --step zotero_written --evidence notes/<citekey>.zotero.json
```

远端回执是 agent 对真实工具响应与回读结果的记录，不是脚本自行联网验证。Notion 回执至少含当前 `source_sha256`、`note_sha256`、`page_id`、`readback_checked: true`；图片回执另含 `images` 的本地路径、SHA-256、远端 block ID，或空数组与 `no_images_reason`；Zotero 回执另含 `item_key`。失败可用 `--failed "<原因>"` 记录，步骤仍为 pending。

源 PDF 改变会使所有步骤失效；笔记改变保留 PDF 提取状态，清除后续完成状态；已记录图片发生变化时，图片步骤重新 pending。再次运行 `init` 更新指纹后继续。状态格式见 `schemas/run.schema.json`。

## 5 配置与发布边界

```bash
python scripts/check_config.py
python scripts/check_config.py --schema-json notes/notion-schema.json
```

第一条只检查本地配置结构；第二条对照从 Notion MCP 获取的真实 schema 导出。离线通过不能表述为“真实数据库已验证”。Notion 图片的上传与回读、Zotero 回写顺序见 `reference-library.md`。

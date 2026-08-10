# MinerU 全量数学资料 OCR 设计

**日期：** 2026-08-10

## 1. 目标

将项目 `math_text/` 下的 21 本数学 PDF（共 6,132 页）通过 MinerU 在线 API 使用 `vlm` 模型完成 OCR，并将结果按书籍隔离保存到 `ocr_text/`，为后续 embedding 提供稳定、可追溯、不会跨书混淆的本地数据。

本阶段只负责 OCR、结构化落盘和质量检查，不执行 embedding，也不自动改写 MinerU 原始识别结果。

## 2. 已确认的 OCR 配置

```json
{
  "model_version": "vlm",
  "language": "ch",
  "is_ocr": true,
  "enable_formula": true,
  "enable_table": true
}
```

## 3. 书籍识别与分片

- 扫描 `math_text/*.pdf`，每个 PDF 生成唯一 `book_id`。
- `book_id` 使用稳定序号加清洗后的文件名，例如 `01_fudan_shufen_textbook_1`。
- 每本书按原始页码连续切分，每个上传分片最多 180 页。
- 分片命名为 `0001_pages_0001_0180.pdf`，页码使用原 PDF 的 1-based 页码。
- 每个分片的 manifest 记录：原文件绝对路径、文件 SHA-256、总页数、分片编号、起止页、分片 SHA-256 和当前处理状态。
- 180 页上限同时规避已知的单文件页数和文件大小边界；拆分后仍保留原书与原页码映射。

## 4. API 批处理与断点恢复

- 从项目根目录 `.env` 读取 `MinerU_API`，密钥不写入 manifest、日志或标准输出。
- 按最多 40 个分片组成一个 API 批次，使用预签名 URL 上传，沿用已验证的无额外 `Content-Type` PUT 逻辑。
- API 任务状态保存到 `ocr_text/_jobs/`，包括请求配置、批次 ID、轮询记录、最终状态、下载 ZIP 和解压状态。
- 任务重启时根据 manifest 和已存在的完整结果文件判断：已完成分片跳过，失败分片可重试，未完成批次继续轮询。
- 只有状态为 `done` 且 ZIP 通过完整性检查的分片才进入“可合并”状态。
- API 返回的签名下载 URL 只保存在本地任务日志，报告和 embedding 数据不包含 API token。

## 5. 本地目录结构

```text
ocr_text/
├── manifest.json
├── _jobs/
│   ├── batch_0001/
│   └── ...
├── 01_fudan_shufen_textbook_1/
│   ├── book.json
│   ├── chunks/
│   │   ├── 0001_pages_0001_0180/
│   │   │   ├── origin.pdf
│   │   │   ├── full.md
│   │   │   ├── *_content_list.json
│   │   │   ├── *_content_list_v2.json
│   │   │   ├── layout.json
│   │   │   └── images/
│   │   └── ...
│   ├── full.md
│   ├── content_list.jsonl
│   ├── formulas.jsonl
│   ├── tables.jsonl
│   ├── images/
│   └── quality/
│       ├── summary.json
│       └── anomalies.jsonl
└── ...
```

- `chunks/` 保存每个分片的完整 MinerU 原始结果，防止合并时丢失审计信息。
- 书籍级 `full.md`、`content_list.jsonl`、`formulas.jsonl` 和 `tables.jsonl` 只合并同一个 `book_id` 的分片。
- 图片文件复制到书籍级 `images/`，文件名加入分片前缀，避免不同分片之间同名覆盖。
- 每条 JSONL 记录都包含 `book_id`、`book_name`、`source_pdf`、`source_page`、`chunk_id` 和 `mineru_page_index`。

## 6. 结构化数据规则

### 6.1 正文

以 `content_list_v2.json` 的顶层块为主，保留标题、正文、例题、定义、定理、公式和表格。页眉、页脚、页码和页下注释默认标记为噪声，不从原始结果删除。

### 6.2 公式

每个公式保存：

- MinerU 原始 LaTeX
- 清洗后的 LaTeX
- 书籍 ID 和原始页码
- bbox
- `layout.json` 图片路径
- 数字断裂、括号不平衡和可疑命令风险标记

清洗版只用于后续 embedding；原始 LaTeX 永久保留。

### 6.3 表格

每个表格保存：

- 原始 `table_body`
- 解析后的行列结构
- 表格图片路径
- 原始页码和 bbox
- `rowspan`、`colspan`
- 表格质量风险标记

对列数突变、数值粘连、异常空单元格、跨行单元格和重复数字进行检测。

### 6.4 图片和版面

- 保留所有 MinerU `images/` 图片。
- 统一转换图片引用为书籍级相对路径。
- 对无法关联的图片、缺失图片和未引用图片分别记录。
- 不将几何图、教材插图或表格图片转成纯文本后丢弃。

## 7. 质量门禁

一个分片只有满足以下条件才允许合并到书籍级结果：

1. API 状态为 `done`。
2. ZIP 完整性检查通过。
3. 存在 `full.md`、`content_list.json`、`content_list_v2.json` 和 `layout.json`。
4. 分片页数与 manifest 一致，且原始页码映射连续。
5. 图片引用没有指向不存在的文件。

书籍级质量报告额外记录：

- OCR 页数和缺页列表
- 内容块统计
- 公式数量和公式图片覆盖率
- 数字断裂数量
- 表格数量、行列数和异常表格页
- 页眉页脚噪声数量
- 图片引用完整性

任何风险只进入 `quality/anomalies.jsonl`，不自动替换 VLM 输出。后续可以针对异常页重新调用 pipeline。

## 8. 验收标准

- `math_text/` 的 21 本 PDF 全部出现在 `ocr_text/manifest.json`。
- 总页数为 6,132 页，书籍之间无跨目录内容。
- 每本书的书籍级 `full.md` 和 JSONL 文件只由该书分片组成。
- 每条结构化记录都能回溯到书籍、原始 PDF 和原始页码。
- 失败或未完成分片不会被静默合并。
- 可重复运行而不重复上传已完成分片。
- 完成后提供书籍级汇总和异常清单，不把异常页伪装成已校验内容。

## 9. 范围外事项

- 不生成向量 embedding。
- 不修改原始 `math_text/` PDF。
- 不自动修复不确定的公式或表格。
- 不在不同书籍之间做章节或知识点合并；跨书知识图谱和检索归一化放到后续阶段。

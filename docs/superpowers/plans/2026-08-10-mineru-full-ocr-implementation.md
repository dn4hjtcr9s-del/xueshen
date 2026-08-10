# MinerU Full OCR Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 将 `math_text/` 下 21 本、6,132 页 PDF 通过 MinerU VLM OCR，按书籍隔离、可恢复地落盘到 `ocr_text/`。

**Architecture:** 使用一个小型 Python 包分离 manifest/分片、API 客户端、结果合并和 CLI 编排。所有状态由 `ocr_text/manifest.json` 与 `ocr_text/_jobs/` 持久化；每本书只读取自己的 chunk 结果并生成书籍级 Markdown、JSONL 和质量报告。

**Tech Stack:** Python 3.13、标准库 `urllib/http.client/zipfile/json`、现有 `.venv` 中的 `pypdf`、`unittest`。

## Global Constraints

- MinerU 配置固定为 `vlm`、`language=ch`、`is_ocr=true`、`enable_formula=true`、`enable_table=true`。
- 每个 PDF 分片最多 180 页，每个 API 批次最多 40 个分片。
- 所有 OCR 结果必须位于 `ocr_text/<book_id>/`，不得跨书合并。
- API 密钥只从项目 `.env` 的 `MinerU_API` 读取，不写入任何产物。
- 原始 MinerU 文件全部保留；清洗数据与风险标记不能覆盖原始识别结果。
- 新增或修改的 Python 模块使用简体中文模块说明和必要注释。
- 当前目录不是 Git 仓库，因此计划执行不包含 commit 步骤，以测试证据代替提交门禁。

---

### Task 1: 稳定书籍清单与 PDF 分片

**Files:**
- Create: `scripts/mineru_ocr/__init__.py`
- Create: `scripts/mineru_ocr/manifest.py`
- Create: `tests/test_mineru_ocr_manifest.py`

**Interfaces:**
- Produces: `build_manifest(math_dir: Path, output_dir: Path, max_pages: int = 180) -> dict`
- Produces: `materialize_pending_chunks(manifest: dict, output_dir: Path) -> dict`
- Produces: `save_manifest_atomic(path: Path, manifest: dict) -> None`

- [ ] 写测试：稳定 `book_id`、180 页边界、原页码映射、重复运行不改变 ID。
- [ ] 运行测试并确认因模块尚不存在而失败。
- [ ] 实现文件扫描、SHA-256、PDF 页数、分片元数据和原子写 manifest。
- [ ] 实现按需生成 chunk PDF，并校验实际页数与分片 SHA-256。
- [ ] 运行 manifest 测试并确认通过。

### Task 2: MinerU API 客户端与持久化批次

**Files:**
- Create: `scripts/mineru_ocr/client.py`
- Create: `tests/test_mineru_ocr_client.py`

**Interfaces:**
- Consumes: manifest 中状态为 `prepared`/`retry` 的 chunk。
- Produces: `MinerUClient.submit_batch(chunks: list[dict]) -> dict`
- Produces: `MinerUClient.poll_batch(batch_id: str) -> list[dict]`
- Produces: `download_and_extract_result(result: dict, chunk_dir: Path) -> None`
- Produces: `safe_extract_zip(zip_path: Path, destination: Path) -> None`

- [ ] 写测试：无 `Content-Type` 预签名 PUT、API 密钥不进入请求日志、批次 payload、ZIP 路径穿越拒绝、ZIP 完整性。
- [ ] 运行测试并确认失败。
- [ ] 移植已验证的 MinerU v4 上传/轮询/下载逻辑，增加重试和原子日志。
- [ ] 实现安全 ZIP 解压和必需文件校验。
- [ ] 运行客户端测试并确认通过。

### Task 3: 按书籍合并 OCR 与质量标记

**Files:**
- Create: `scripts/mineru_ocr/merge.py`
- Create: `tests/test_mineru_ocr_merge.py`

**Interfaces:**
- Produces: `validate_chunk_result(chunk: dict, chunk_dir: Path) -> dict`
- Produces: `merge_book(book: dict, output_dir: Path) -> dict`
- Produces: `merge_all_books(manifest: dict, output_dir: Path) -> dict`

- [ ] 写测试：两个 chunk 页码连续合并、图片重命名、不同书籍不串数据、页眉页脚过滤标记、公式/表格异常记录。
- [ ] 运行测试并确认失败。
- [ ] 实现 MinerU v2/content list/layout 读取和原页码映射。
- [ ] 实现 `full.md`、`content_list.jsonl`、`formulas.jsonl`、`tables.jsonl`、书籍级图片与质量文件。
- [ ] 实现公式数字断裂和表格结构风险检测，不覆盖原始输出。
- [ ] 运行合并测试并确认通过。

### Task 4: 可恢复 CLI 编排

**Files:**
- Create: `scripts/mineru_ocr/runner.py`
- Create: `scripts/run_mineru_ocr.py`
- Create: `tests/test_mineru_ocr_runner.py`

**Interfaces:**
- Produces CLI: `prepare`、`run`、`resume`、`merge`、`status`。
- `run/resume` 每批最多取 40 个未完成 chunk；每次状态变化原子更新 manifest。

- [ ] 写测试：已完成 chunk 跳过、失败 chunk 可重试、未完成批次恢复轮询、每批最多 40 个。
- [ ] 运行测试并确认失败。
- [ ] 实现编排状态机和命令行参数。
- [ ] 运行 runner 测试并确认通过。

### Task 5: 本地全量准备与安全门禁

**Files:**
- Create/update: `ocr_text/manifest.json`
- Create: `ocr_text/<book_id>/book.json`
- Create: `ocr_text/<book_id>/chunks/*.pdf`

**Interfaces:**
- Consumes CLI `prepare`。
- Produces 21 本书、6,132 页、每片不超过 180 页的可上传清单。

- [ ] 运行全部单元测试和 `py_compile`。
- [ ] 执行 `prepare` 生成所有分片。
- [ ] 断言 21 本、6,132 页、所有 chunk 页码连续且 SHA-256 存在。
- [ ] 随机抽取首、中、末分片用 `PdfReader` 验证页数。

### Task 6: 提交 MinerU 并下载结果

**Files:**
- Update: `ocr_text/manifest.json`
- Create/update: `ocr_text/_jobs/batch_*/`
- Create: `ocr_text/<book_id>/chunks/<chunk_id>/raw/`

**Interfaces:**
- Consumes CLI `run --batch-size 40`。
- Produces所有 chunk 的批次 ID、状态、ZIP、解压结果和校验记录。

- [ ] 提交第一批最多 40 个 chunk，并持久化批次后再上传。
- [ ] 轮询到终态，下载所有 `done` ZIP 并安全解压。
- [ ] 对失败项写明 API 错误并进入 retry 状态。
- [ ] 继续提交剩余 chunk，直到没有未完成任务。

### Task 7: 书籍级合并与最终验证

**Files:**
- Create/update: `ocr_text/<book_id>/full.md`
- Create/update: `ocr_text/<book_id>/*.jsonl`
- Create/update: `ocr_text/<book_id>/quality/*.json*`
- Create: `ocr_text/summary.json`

**Interfaces:**
- Consumes CLI `merge` 与 `status --json`。
- Produces 21 本独立书籍结果和全量质量汇总。

- [ ] 仅对通过 chunk 门禁的书籍执行合并。
- [ ] 校验每本书页数、页码连续性、JSONL `book_id` 一致性和图片引用。
- [ ] 运行全量单元测试、语法检查和结果完整性断言。
- [ ] 输出完成书籍、失败书籍和异常页统计，不把不完整书籍标记为完成。

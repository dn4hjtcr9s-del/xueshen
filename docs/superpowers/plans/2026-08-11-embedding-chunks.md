# 教材 Embedding Chunk 构建 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 构建从 clean OCR 记录到 800/100 token-aware、可审计、稳定可复现 embedding chunks 的完整流水线。

**Architecture:** 清洗阶段把 raw block 的精确 `source_refs` 保留到 clean 记录；embedding 阶段依次执行读取校验、标题继承、角色分类、语义单元组合、token-aware 切块、稳定标识和质量报告。核心逻辑按纯函数和小型 dataclass 拆分，CLI 只负责参数、文件遍历和原子发布。

**Tech Stack:** Python 3.13、标准库 dataclasses/HTMLParser/hashlib/uuid、Pydantic（输出校验）、tiktoken（生产 tokenizer）、pytest、ruff、mypy。

## Global Constraints

- 输入内容以 `clean_text/<book_id>/clean_content_list.jsonl` 为准，raw OCR 只用于精确定位校验。
- 固定默认参数 `chunk_size=800`、`chunk_overlap=100`。
- 800 tokens 约束最终 `embedding_text`；overlap 只作用于同语义单元可拆正文。
- 公式和表格不得静默截断；无图注图片不得进入 embedding。
- 所有新增或修改模块提供简体中文模块说明和必要中文注释。
- 采用 TDD：生产代码前先写失败测试，并分别验证 RED/GREEN。

---

### Task 1: 清洗溯源链

**Files:**
- Modify: `scripts/clean_ocr.py`
- Create: `tests/test_clean_ocr_provenance.py`

**Interfaces:**
- Produces: clean JSONL 的 `source_page_end: int` 与 `source_refs: list[dict[str, object]]`。
- Consumes: raw record 的 `source_page/mineru_page_index/block_index/chunk_id/source_pdf/raw`。

- [ ] 写测试：单 block、跨段落合并、跨公式合并都保留有序 refs 和 raw hash。
- [ ] 运行 `pytest tests/test_clean_ocr_provenance.py -q`，确认因缺少 `source_refs` 失败。
- [ ] 为 raw block 构造规范 source ref，并在两个 merge 函数中合并 refs。
- [ ] 输出 `source_page_end` 和 `source_refs`。
- [ ] 重跑测试并提交。

### Task 2: Schema、读取和 raw 精确校验

**Files:**
- Create: `scripts/embedding_chunks/__init__.py`
- Create: `scripts/embedding_chunks/schemas.py`
- Create: `scripts/embedding_chunks/source_reader.py`
- Create: `scripts/embedding_chunks/provenance.py`
- Create: `tests/embedding_chunks/test_source_reader.py`
- Create: `tests/embedding_chunks/test_provenance.py`

**Interfaces:**
- Produces: `SourceRef`、`CleanRecord`、`SemanticSegment`、`SemanticUnit`、`ChunkRecord`、`ExcludedRecord`。
- Produces: `read_clean_records(path)`、`RawSourceIndex.from_jsonl(path)`、`validate_source_refs(record, index)`。

- [ ] 写失败测试覆盖 JSONL 解析、缺失 refs、精确键命中、raw hash 不匹配。
- [ ] 实现不可变 dataclass 和显式 JSON 序列化。
- [ ] 实现流式读取与 raw key `(source_page, block_index)` 索引。
- [ ] 运行新测试并提交。

### Task 3: 角色分类、标题栈和正文筛选

**Files:**
- Create: `scripts/embedding_chunks/role_classifier.py`
- Create: `scripts/embedding_chunks/heading_tracker.py`
- Create: `tests/embedding_chunks/test_role_classifier.py`
- Create: `tests/embedding_chunks/test_heading_tracker.py`

**Interfaces:**
- Produces: `classify_record(record, chapter_path) -> Classification`。
- Produces: `HeadingTracker.update(level, title)` 与 `HeadingTracker.path`。

- [ ] 写失败测试覆盖 1–4 级标题覆盖、答案、索引、参考文献、定义/定理/证明/例/解/习题/附录和无图注图片。
- [ ] 实现最小规则并给排除项稳定 reason code。
- [ ] 运行测试并提交。

### Task 4: 表格线性化和语义单元恢复

**Files:**
- Create: `scripts/embedding_chunks/table_formatter.py`
- Create: `scripts/embedding_chunks/semantic_units.py`
- Create: `tests/embedding_chunks/test_table_formatter.py`
- Create: `tests/embedding_chunks/test_semantic_units.py`

**Interfaces:**
- Produces: `linearize_table(html, caption) -> str`。
- Produces: `build_semantic_units(records) -> tuple[list[SemanticUnit], list[ExcludedRecord]]`。

- [ ] 写失败测试覆盖实体解码、rowspan/colspan 的可读降级、caption、图片路径排除。
- [ ] 写失败测试覆盖定理+证明、例题+解、公式上下文、表格、图注和标题边界。
- [ ] 用 `HTMLParser` 实现无第三方依赖的表格解析。
- [ ] 实现状态机式语义组合并运行测试。
- [ ] 提交。

### Task 5: Tokenizer 与 800/100 安全切块

**Files:**
- Modify: `pyproject.toml`
- Modify: `uv.lock`
- Create: `scripts/embedding_chunks/tokenizer.py`
- Create: `scripts/embedding_chunks/chunker.py`
- Create: `tests/embedding_chunks/test_tokenizer.py`
- Create: `tests/embedding_chunks/test_chunker.py`
- Create: `tests/embedding_chunks/test_overlap.py`

**Interfaces:**
- Produces: `Tokenizer` protocol、`TiktokenTokenizer`、测试用 `WhitespaceTokenizer`。
- Produces: `chunk_semantic_unit(unit, tokenizer, chunk_size, overlap)`。

- [ ] 写失败测试证明前缀计入 800、正文 overlap 为 100、角色边界不 overlap、不可拆原子超限会排除。
- [ ] 添加 tiktoken optional dependency 并更新 lock。
- [ ] 实现 encode/decode protocol 和安全 segment packing。
- [ ] 运行测试并提交。

### Task 6: 稳定 ID、哈希和质量门禁

**Files:**
- Create: `scripts/embedding_chunks/identifiers.py`
- Create: `scripts/embedding_chunks/quality.py`
- Create: `tests/embedding_chunks/test_identifiers.py`
- Create: `tests/embedding_chunks/test_quality.py`

**Interfaces:**
- Produces: `content_hash(text)`、`source_hash(refs)`、`stable_chunk_id(...)`。
- Produces: `validate_chunks(chunks, tokenizer, chunk_size) -> QualityReport`。

- [ ] 写失败测试覆盖 UUIDv5 稳定性、输入变化、重复 ID、路径污染、HTML、token 超限和页码不一致。
- [ ] 实现哈希规范化和聚合质量报告。
- [ ] 运行测试并提交。

### Task 7: Builder、报告、manifest 和 CLI

**Files:**
- Create: `scripts/embedding_chunks/builder.py`
- Create: `scripts/build_embedding_chunks.py`
- Create: `tests/embedding_chunks/test_builder.py`
- Create: `tests/embedding_chunks/test_cli.py`

**Interfaces:**
- Produces: `BuildConfig`、`build_book(config, book_id)`、`build_all(config)`。
- CLI: `python scripts/build_embedding_chunks.py --all --clean-root ... --raw-root ... --output-root ...`。

- [ ] 写失败测试，以微型双书 fixture 验证 deterministic ordering、chunks/exclusions、book reports、quality report 和 manifest。
- [ ] 实现临时目录构建、质量通过后原子替换输出目录。
- [ ] 支持 `--book`/`--all`、默认 800/100、tokenizer 参数和非零失败退出。
- [ ] 运行测试并提交。

### Task 8: 全量数据再生成与验收

**Files:**
- Generated: `/Users/kebofeier/Desktop/xueshen/clean_text/*/clean_content_list.jsonl`
- Generated: `/Users/kebofeier/Desktop/xueshen/embedding_artifacts/v1/*`

**Interfaces:**
- Consumes: Tasks 1–7 的 CLI。
- Produces: 正式 `embedding_artifacts/v1` 数据集。

- [ ] 使用更新后的 cleaner 对 21 本书重新生成 clean artifacts。
- [ ] 运行 chunk builder 生成 v1 artifacts。
- [ ] 检查质量报告、排除原因分布、token 上限和 source ref 命中率。
- [ ] 运行 `ruff check scripts tests/embedding_chunks tests/test_clean_ocr_provenance.py`。
- [ ] 运行 `mypy scripts/embedding_chunks scripts/build_embedding_chunks.py`。
- [ ] 在隔离数据库上运行完整 `pytest -q`，预期全部通过。
- [ ] 提交代码与报告摘要；大型生成数据遵循 `.gitignore`，不入 Git。

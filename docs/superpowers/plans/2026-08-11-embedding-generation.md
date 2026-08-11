# 教材 Embedding 生成 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 从已发布的 Chunk Artifact 可靠生成 `text-embedding-v4`、1024 维向量 Artifact，不接触数据库，并提供阶段三可严格校验的稳定导入接口。

**Architecture:** 使用 OpenAI-compatible 同步客户端和线程池完成有界并发；每个稳定批次先原子写入 shard，恢复时从 shard 重建缓存与状态，最后压实为 `embeddings.jsonl`、`failures.jsonl`、`usage.json` 和带哈希的 manifest。配置、向量校验、API 调用、Artifact I/O 和调度相互独立，单元测试全部注入 Fake Client。

**Tech Stack:** Python 3.13、标准库 dataclass/concurrent.futures/hashlib/json、OpenAI Python SDK 2.x、pytest、ruff、mypy。

## Global Constraints

- 阶段二不得连接 PostgreSQL，不得读取 `DATABASE_URL` 或 `RAG_DATABASE_URL`。
- 不修改根目录 Memory `alembic.ini`、`alembic/versions/`、`backend/memory/`、`docker-compose.yml`。
- 模型固定为 `text-embedding-v4`，维度固定为 1024。
- 默认 batch size 为 10，并发为 4；配置可覆盖但维度校验不可关闭。
- API key 只能来自显式配置，不得打印、持久化或提交。
- 每批结果先原子写 Artifact，数据库不是阶段二落点。
- 所有新建或修改的代码模块使用简体中文模块说明和必要注释。

---

### Task 1: 配置、数据模型和向量校验

**Files:**
- Create: `scripts/embedding_generation/__init__.py`
- Create: `scripts/embedding_generation/schemas.py`
- Create: `scripts/embedding_generation/settings.py`
- Create: `scripts/embedding_generation/validation.py`
- Create: `tests/embedding_generation/test_settings.py`
- Create: `tests/embedding_generation/test_validation.py`

**Interfaces:**
- Produces: `EmbeddingSettings.from_sources(env_file, environ, overrides) -> EmbeddingSettings`。
- Produces: `ChunkInput.from_dict(payload) -> ChunkInput`、`EmbeddingVector`、`UsageStats`。
- Produces: `embedding_input_hash(text) -> str`、`embedding_cache_key(...) -> str`、`validate_vector(vector, dimensions) -> tuple[float, ...]`。

- [ ] 编写失败测试：env 文件、环境变量和覆盖值优先级；`DASHSCOPE_API_KEY` fallback；secret 不进入 repr；默认模型/维度/batch/concurrency。
- [ ] 编写失败测试：1024 维成功；错误维度、NaN、Inf、全零失败；cache key 对文本、模型和维度变化敏感。
- [ ] 运行 `pytest tests/embedding_generation/test_settings.py tests/embedding_generation/test_validation.py -q`，确认测试先失败。
- [ ] 实现最小配置、schema 和 validation。
- [ ] 重跑目标测试，预期全部通过。

### Task 2: OpenAI-compatible 客户端和重试分类

**Files:**
- Create: `scripts/embedding_generation/client.py`
- Create: `tests/embedding_generation/test_client.py`

**Interfaces:**
- Consumes: `EmbeddingSettings`、`validate_vector`。
- Produces: `EmbeddingClient` protocol、`OpenAIEmbeddingClient.embed(texts) -> ClientBatchResponse`。
- Produces: `EmbeddingRequestError(code, retryable, retry_after, message)`，所有消息必须脱敏。

- [ ] 编写 Fake SDK 测试：显式传入 model、dimensions、float encoding，按响应 index 排序，usage 转换正确。
- [ ] 编写错误测试：429/408/5xx/timeout/connection 为 retryable；认证和其他 4xx 为 permanent；响应数量/index 异常失败。
- [ ] 运行目标测试并确认失败。
- [ ] 实现客户端适配和错误分类，禁用 SDK 内部重试，由 runner 统一控制。
- [ ] 重跑目标测试，预期全部通过。

### Task 3: Chunk 输入验证和原子 Artifact

**Files:**
- Create: `scripts/embedding_generation/artifacts.py`
- Create: `tests/embedding_generation/test_artifacts.py`

**Interfaces:**
- Produces: `load_chunk_dataset(chunk_root) -> ChunkDataset`，验证 manifest SHA/记录数/ID/index。
- Produces: `ArtifactStore.open(output_root, dataset, profile) -> ArtifactStore`。
- Produces: `write_batch(outcome)`、`load_completed_batches()`、`publish_summary()`。

- [ ] 编写输入测试：正确 manifest 可读，文件 SHA、记录数、重复 ID、非连续 index 或空 embedding_text 时拒绝。
- [ ] 编写 profile 测试：同目录 source build/model/dim 不匹配时拒绝。
- [ ] 编写 shard 测试：临时文件 + fsync + replace；中断残留临时文件不算完成；batch identity 不匹配时拒绝恢复。
- [ ] 编写压实测试：稳定按 chunk_index 输出，生成文件 SHA、usage、failures，只有全覆盖零失败时为 ready。
- [ ] 运行目标测试并确认失败。
- [ ] 实现流式输入校验、原子 JSON/JSONL 写入、shard 读取与最终压实。
- [ ] 重跑目标测试，预期全部通过。

### Task 4: 批处理、并发、退避、缓存与断点续传

**Files:**
- Create: `scripts/embedding_generation/runner.py`
- Create: `tests/embedding_generation/test_runner.py`

**Interfaces:**
- Consumes: `EmbeddingClient`、`ChunkDataset`、`ArtifactStore`、`EmbeddingSettings`。
- Produces: `EmbeddingRunner.run(limit=None, retry_failures=False) -> RunSummary`。
- Produces: `RequestRateLimiter.acquire()`，支持关闭和测试注入 clock/sleep。

- [ ] 编写测试：10 条批处理、最大并发不超过配置、可选 RPS 限制。
- [ ] 编写测试：429/5xx/timeout 指数退避并最终成功；Retry-After 优先；超过最大次数时 batch 保持未完成。
- [ ] 编写测试：永久批次错误递归二分，只将坏输入标记失败，其余成功。
- [ ] 编写测试：重复 cache key 只请求一次并展开到多个 chunk；已有 shard 不重复请求；`--retry-failures` 重跑失败批次。
- [ ] 编写测试：单个无效向量失败，不污染同批其他向量；usage/request/retry/cache 统计正确。
- [ ] 运行目标测试并确认失败。
- [ ] 实现稳定 unique-job 分组、线程池、有界速率、重试退避、拆批和逐批持久化。
- [ ] 重跑目标测试，预期全部通过。

### Task 5: CLI 与安全输出

**Files:**
- Create: `scripts/generate_embeddings.py`
- Create: `tests/embedding_generation/test_cli.py`
- Modify: `.env.example`

**Interfaces:**
- CLI: `python scripts/generate_embeddings.py --env-file ... --chunk-root ... [--output-root ...] [--limit N] [--retry-failures]`。
- 输出：只打印 JSON 进度摘要和路径，不包含 secret 或完整请求文本。

- [ ] 编写 CLI 测试：直接执行 `--help`；缺少 key/输入返回非零；传入 Fake Runner 时参数映射正确；stderr/stdout 不泄露 key。
- [ ] 运行目标测试并确认失败。
- [ ] 实现参数解析、默认输出目录计算、OpenAI 客户端装配和非零失败退出码。
- [ ] 在 `.env.example` 增加无 secret 的 Embedding 配置说明，不添加数据库配置。
- [ ] 重跑目标测试，预期全部通过。

### Task 6: 全套质量验证和真实 API 冒烟

**Files:**
- Generated only: `/Users/kebofeier/Desktop/xueshen/embedding_artifacts/v1/embeddings/<profile>/...`

**Interfaces:**
- Consumes: 主工作区 `.env` 和正式 Chunk Artifact。
- Produces: 可恢复的部分或完整 1024 维 Embedding Artifact。

- [ ] 运行 `pytest tests/embedding_generation -q`。
- [ ] 运行 `ruff check scripts/embedding_generation scripts/generate_embeddings.py tests/embedding_generation`。
- [ ] 运行 `mypy scripts/embedding_generation scripts/generate_embeddings.py`。
- [ ] 运行现有 `pytest tests/embedding_chunks -q`，确认阶段一未回归。
- [ ] 使用显式 `--env-file /Users/kebofeier/Desktop/xueshen/.env --chunk-root /Users/kebofeier/Desktop/xueshen/embedding_artifacts/v1 --limit 20` 做真实 API 冒烟。
- [ ] 校验生成 manifest 仍为 `partial`、20 条向量长度全部为 1024、文件哈希正确且 Artifact 不含 API key。
- [ ] 在启动 15,000 条全量任务前，根据 smoke 的 token usage 和可配置单价报告预计费用；未获得明确费用参数时不伪造金额。

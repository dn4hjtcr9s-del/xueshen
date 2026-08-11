# 教材 Embedding 生成与 Artifact 发布设计

## 目标

读取已经通过质量门禁的 `embedding_artifacts/v1/chunks.jsonl`，调用 OpenAI-compatible Embedding API，为每个 Chunk 的 `embedding_text` 生成 `text-embedding-v4`、1024 维向量，并发布可校验、可恢复、可供阶段三独立导入 PostgreSQL + pgvector 的本地 Artifact。

阶段二不连接数据库，不创建 migration、表、容器或 volume，也不读取 `DATABASE_URL`/`RAG_DATABASE_URL`。数据库是阶段三消费者，不是阶段二的状态存储或唯一落点。

## 固定边界

- 输入 Chunk 参数保持 `size=800`、`overlap=100`，阶段二不重新切块。
- 模型固定为 `text-embedding-v4`，维度固定为 `1024`。
- API 配置通过显式 `--env-file`、进程环境变量或 CLI 参数注入；代码不硬编码主工作区绝对路径。
- API key 优先读取 `EMBEDDING_API_KEY`，兼容读取 `DASHSCOPE_API_KEY`，不复制、不打印、不写入 Artifact。
- 单元测试只使用 Fake Client；真实 API 只能由显式 CLI 命令触发。
- 阶段三只消费已达到 `ready` 状态且全部哈希校验通过的 Artifact。

## 方案选择

采用“同步兼容 API + 有界并发 + 每批原子 shard + 最终压实”的方案：

1. 启动时校验 Chunk manifest、`chunks.jsonl` SHA-256 和记录数。
2. 为每条输入计算 `embedding_input_hash` 和稳定 cache key。
3. 相同 `content_hash`、`embedding_input_hash`、模型和维度只请求一次，结果展开到全部对应 Chunk。
4. 唯一输入按稳定顺序分批；批次并发有上限。
5. 每个批次成功或完成永久失败隔离后，先原子写入 `parts/batch-XXXXXX.jsonl`。
6. 重新启动时扫描并严格校验已有 shard，仅处理未完成批次。
7. 本轮结束后从 shard 重建统计、失败清单、压实向量文件和 manifest。
8. 只有全部 Chunk 成功、无重复且每个向量通过校验时，manifest 才标记为 `ready`。

不采用直接流式写 PostgreSQL，因为这会把阶段二与阶段三耦合，也违反数据库不是 embedding 唯一落点的约束。不采用异步 Batch API 作为 v1 主路径，因为 15,000 条规模下任务提交、轮询和部分失败恢复增加的复杂度大于收益。

## 模块边界

```text
scripts/
├── embedding_generation/
│   ├── __init__.py       # 稳定公共接口
│   ├── schemas.py        # Chunk、请求、结果、usage 数据模型
│   ├── settings.py       # env/CLI 配置与 Secret 边界
│   ├── validation.py     # 输入哈希和 1024 维向量校验
│   ├── client.py         # OpenAI-compatible 客户端与错误分类
│   ├── artifacts.py      # Chunk 校验、原子 shard、压实和 manifest
│   └── runner.py         # 批处理、并发、重试、退避、拆批和恢复
└── generate_embeddings.py

tests/embedding_generation/
├── test_settings.py
├── test_validation.py
├── test_client.py
├── test_artifacts.py
├── test_runner.py
└── test_cli.py
```

这些模块位于 `scripts`，不放入 `backend/memory`，也不修改现有 Memory Alembic 或 Memory 表。

## 配置

默认配置：

```text
EMBEDDING_MODEL=text-embedding-v4
RAG_EMBEDDING_DIMENSIONS=1024
RAG_EMBEDDING_BATCH_SIZE=10
RAG_EMBEDDING_CONCURRENCY=4
RAG_EMBEDDING_TIMEOUT_SECONDS=60
RAG_EMBEDDING_MAX_ATTEMPTS=6
RAG_EMBEDDING_INITIAL_BACKOFF_SECONDS=1
RAG_EMBEDDING_MAX_BACKOFF_SECONDS=30
RAG_EMBEDDING_JITTER_SECONDS=0.5
RAG_EMBEDDING_REQUESTS_PER_SECOND=0
RAG_EMBEDDING_PRICE_PER_MILLION_TOKENS=
```

`RAG_EMBEDDING_REQUESTS_PER_SECOND=0` 表示仅使用并发上限；设置为正数时启用跨线程请求速率限制。费用必须通过可配置单价计算；代码不硬编码供应商价格。

配置优先级为 CLI > 进程环境变量 > `--env-file` > 默认值。配置对象的 `repr` 不得包含 API key。

## 输入一致性

运行前必须验证：

- `manifest.json` 的 `schema_version` 是 `embedding-chunks/v1`。
- `chunks.jsonl` 实际 SHA-256 等于 manifest 中记录的 SHA-256。
- 实际非空 JSONL 记录数等于 `chunk_count` 和文件记录数。
- `chunk_id` 唯一，`chunk_index` 连续且稳定。
- `embedding_text` 非空。
- 每条记录具备 `content_hash`。

Artifact profile 固定记录：

- Chunk `build_id`
- Chunk manifest SHA-256
- chunks 文件 SHA-256
- 模型、维度、输入字段和 profile hash

同一输出目录若 profile 不一致则拒绝运行，禁止把不同 Chunk build、模型或维度混入同一 Artifact。

## 缓存与批次稳定性

cache key 使用以下字段的规范 JSON SHA-256：

```text
content_hash
embedding_input_hash
model
1024
```

其中 `embedding_input_hash` 是 `embedding_text` UTF-8 字节的 SHA-256。`content_hash` 相同但 embedding 前缀不同不会误复用。

唯一 cache key 按首次出现的 `chunk_index` 排序，再以固定 batch size 分批。批次 identity 由批次序号和 cache key 列表计算；已有 shard 的 identity 不匹配时立即失败，而不是静默跳过。

## API 可靠性

- 429、408、连接错误、超时和 5xx 视为瞬时错误。
- 瞬时错误使用指数退避和 jitter，支持服务端 `Retry-After`。
- 认证失败、权限失败和其他确定性 4xx 不盲目重试。
- 确定性批次错误会二分输入，直到隔离到单条失败；成功条目仍写入 shard。
- 瞬时错误超过最大重试次数后不写完成 shard，命令返回非零；下次运行会自动恢复该批次。
- 每条永久失败写入 shard 和 `failures.jsonl`，但不会阻止其他批次继续。
- `--retry-failures` 可重新执行含永久失败记录的已有批次。

## 向量校验

每个 API 响应必须满足：

- 响应条数与请求条数一致，index 无重复、无缺失。
- 每个 vector 长度严格等于 1024。
- 每个元素可转换为 float 且为有限数。
- 不含 NaN 或 Inf。
- 向量不是全零。

验证失败不得进入成功 Artifact。返回条数或 index 异常按响应错误处理；单个无效向量隔离为该输入失败。

## Artifact 格式

默认输出目录包含 Chunk build ID，避免 Chunk v1 重建时覆盖旧向量：

```text
embedding_artifacts/v1/embeddings/
└── <build-id前12位>-text-embedding-v4-d1024/
    ├── profile.json
    ├── manifest.json
    ├── usage.json
    ├── failures.jsonl
    ├── embeddings.jsonl
    └── parts/
        ├── batch-000000.jsonl
        └── ...
```

每个 shard 是单文件 JSONL：第一行是批次元数据，后续行是该批覆盖的成功或失败 Chunk 记录。文件先写入同目录临时文件，flush、fsync 后使用 `os.replace` 原子发布。

`embeddings.jsonl` 不重复存储完整 Chunk 文本，只保存阶段三严格联接和校验所需字段：

- `chunk_id`
- `chunk_index`
- `content_hash`
- `embedding_input_hash`
- `cache_key`
- `profile_id`
- `model`
- `dimensions`
- `vector`
- 可选的 `cached_from_chunk_id`

`failures.jsonl` 保存 chunk identity、稳定错误代码、脱敏错误信息和 attempt 统计，不保存 API key。

## 统计与费用

每个 shard 记录请求数、重试数、API 输入数、Chunk 覆盖数、prompt/total token。`usage.json` 从已验证 shard 重建，避免进程中断导致计数漂移。

当配置每百万 token 单价时，用 API 返回的计费 token 计算估算费用；未配置时费用字段为 `null`。缓存展开的 Chunk 不重复计费。

## 发布状态与阶段三契约

manifest 状态：

- `partial`：仍有未执行批次、永久失败或本轮瞬时失败。
- `ready`：15000 条 Chunk 全覆盖、无失败、无重复、所有向量为 1024 维，所有文件哈希已记录。

阶段三导入前必须重新验证：

1. manifest 状态为 `ready`。
2. Chunk build ID 和 chunks SHA-256 与待导入 Chunk Artifact 一致。
3. profile 的模型为 `text-embedding-v4`、维度为 1024。
4. `embeddings.jsonl` 文件哈希和记录数正确。
5. 每个 `chunk_id` 恰好出现一次，`content_hash` 与 Chunk 一致。

这样阶段三可以独立选择数据库 schema 和索引，不要求阶段二了解 PostgreSQL。

## CLI

正式调用形式：

```bash
.venv/bin/python scripts/generate_embeddings.py \
  --env-file /Users/kebofeier/Desktop/xueshen/.env \
  --chunk-root /Users/kebofeier/Desktop/xueshen/embedding_artifacts/v1
```

小规模真实 API 冒烟：

```bash
.venv/bin/python scripts/generate_embeddings.py \
  --env-file /Users/kebofeier/Desktop/xueshen/.env \
  --chunk-root /Users/kebofeier/Desktop/xueshen/embedding_artifacts/v1 \
  --limit 20
```

`--limit` 只限制本轮新执行的 Chunk 范围，不改变完整输入的批次 identity，后续去掉限制即可在同一 Artifact 断点续传。

## 验收标准

- Fake Client 测试覆盖配置、批处理、并发上限、速率限制、429/5xx/timeout 重试、指数退避、Retry-After、拆批隔离、缓存命中、断点续传和失败重试。
- 校验测试覆盖错误维度、NaN、Inf、全零、响应 index 异常和 source/profile hash 不匹配。
- Artifact 测试证明 shard 与最终文件原子发布，恢复时不会重复请求已完成批次。
- CLI 不打印或持久化 API key，缺少配置时返回非零。
- 真实 API smoke test 生成 1024 维有效向量。
- 阶段二代码不导入数据库驱动，不读取数据库 URL，不修改 Memory 目录、Alembic 或 Docker 配置。

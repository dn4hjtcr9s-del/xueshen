# RAG 阶段三：独立 pgvector 入库与检索

## 目标

本阶段把阶段二生成的 chunk/embedding artifact 导入独立 PostgreSQL + pgvector，并验证：

- 精确 cosine 向量查询；
- HNSW 查询及 Recall@10/20/50；
- PostgreSQL simple FTS 中文术语检索；
- 数学公式精确匹配；
- 向量、关键词、公式的 RRF 融合；
- `answer_key` 的 `retrieval_weight = 0.65`；
- 书籍、学段、章节、内容类型过滤；
- 页码和 `source_refs` 引用回溯；
- 与现有 Memory 数据库的部署和 schema 隔离。

## 数据库隔离

阶段三不修改以下 Memory 资产：

- `/Users/kebofeier/Desktop/xueshen/docker-compose.yml`；
- `/Users/kebofeier/Desktop/xueshen/alembic/`；
- Memory 的 `DATABASE_URL`、数据库、表和 `postgres-data` volume。

RAG 使用独立资源：

| 资源 | RAG 配置 |
|---|---|
| Compose | `/Users/kebofeier/Desktop/xueshen/docker-compose.rag.yml` |
| 服务 | `rag-postgres` |
| 镜像 | `pgvector/pgvector:pg17` |
| 本地端口 | `55433` |
| 数据库 | `rag` |
| 用户 | `rag` |
| volume | `rag-postgres-data` |
| URL | `RAG_DATABASE_URL=postgresql+psycopg://rag:rag@127.0.0.1:55433/rag` |
| migration | `/Users/kebofeier/Desktop/xueshen/rag_migrations/` |
| version table | `rag.rag_alembic_version` |

## Schema 设计

### `rag.corpus_versions`

每次 chunk + embedding 组合是一个不可变 corpus 版本，使用 `loading -> ready -> active` 状态流转。表中保存：

- `chunk_build_id`、`embedding_artifact_id`、`embedding_profile_id`；
- 模型、维度、距离度量和词法管线版本；
- chunk 数量和 artifact SHA-256；
- 当前状态和激活时间。

`status = 'active'` 使用部分唯一索引保证同时只有一个 active corpus。重新导入失败时只会标记新版本 `failed`，不会覆盖旧 active 版本。

### `rag.books`

按 corpus 保存书籍级元数据和聚合信息：`book_id`、书名、学段、chunk 数量、最小/最大来源页码。

### `rag.chunks`

这是检索主表，包含：

- chunk 原文和 embedding 输入文本；
- `book_id`、`grade_level`、`section`、`chapter_path`、`content_role`；
- `retrieval_weight`；`answer_key` 必须为 0.65，其他内容为 1.0；
- `source_page_start`、`source_page_end`、完整 `source_refs`；
- `search_text`、`search_vector`、`formula_terms`；
- `embedding vector(1024)`。

索引包括：

- `HNSW (embedding vector_cosine_ops)`；
- `GIN (search_vector)`；
- `GIN (formula_terms)`；
- 书籍、页码、学段、内容类型、章节过滤索引。

### `rag.ingest_runs`

记录每次导入的 run、状态、预期/实际数量、artifact hash、错误详情和完成时间，便于失败审计与重试。

## 本地运行

```bash
cd /Users/kebofeier/Desktop/xueshen

# 1. 只启动独立 RAG PostgreSQL
 docker compose -f docker-compose.rag.yml up -d rag-postgres

# 2. 执行独立 migration；不会读取 DATABASE_URL
RAG_DATABASE_URL='postgresql+psycopg://rag:rag@127.0.0.1:55433/rag' \
  .venv/bin/alembic -c rag_alembic.ini upgrade head

# 3. 导入阶段二 artifact；路径必须显式传入
RAG_DATABASE_URL='postgresql+psycopg://rag:rag@127.0.0.1:55433/rag' \
  .venv/bin/python scripts/rag_import.py \
  --chunk-root /Users/kebofeier/Desktop/xueshen/embedding_artifacts/v1 \
  --embedding-root \
  /Users/kebofeier/Desktop/xueshen/embedding_artifacts/v1/embeddings/64f690dc095d-text-embedding-v4-d1024

# 4. 运行只读验收：Recall、FTS、公式、过滤、引用和隔离
RAG_DATABASE_URL='postgresql+psycopg://rag:rag@127.0.0.1:55433/rag' \
  .venv/bin/python scripts/rag_verify.py --sample-count 12
```

`rag_import.py` 会先验证 manifest、SHA-256、模型、维度、数量、chunk/embedding 的 `chunk_id + chunk_index + content_hash` 关联，再批量写入数据库。导入同一个 `(chunk_build_id, embedding_profile_id)` 会幂等返回，不会重复写入。

## 检索策略

- 精确向量：关闭 index scan，作为 ANN Recall 基线；
- HNSW：使用 `hnsw.ef_search`，默认 100；
- FTS：`simple` 配置，导入时把连续中文拆成二元组，并记录 `zh-bigram-formula/v1`；
- 公式：LaTeX 分隔符公式去空白规范化后存入 `formula_terms text[]`，通过 GIN overlap 精确匹配；
- RRF：向量 Top 50、FTS Top 50、公式 Top 20，默认 `rrf_k = 60`；融合后乘一次 `retrieval_weight`。

## 云部署

本地 embedding 和本地导入是允许且推荐的：artifact 已经是带 manifest/hash 的可迁移交付物。迁移到云服务器时有两种方式：

1. 将 `chunks.jsonl`、embedding `embeddings.jsonl` 和 manifest 安全传到云服务器，在云端独立 RAG PostgreSQL 上重复运行同一导入 CLI；
2. 对已经导入的 RAG 数据库做 PostgreSQL 备份/恢复，再在云端执行 migration 版本核对。

推荐第一种方式：artifact 可校验、可重试、可审计，不依赖本地 Docker volume 的物理迁移。云端只需配置独立的 `RAG_DATABASE_URL`、RAG migration 和 RAG volume；Memory 继续使用自己的 `DATABASE_URL` 与数据库。

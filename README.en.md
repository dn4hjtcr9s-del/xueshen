# xueshen-math

> 中文版：[README.md](./README.md)

**MemoryManagerGraph** — a long-term memory and knowledge-graph system for math learning. It asynchronously extracts explainable learning memories from conversations, exercises, and community activity; maintains per-user mastery states over a textbook knowledge graph; and provides an Agentic RAG math Q&A chat, a mistake-notebook review flow, and learning-community services.

## Features

- **Long-term memory**: conversation evidence is asynchronously extracted → reviewed → merged into Markdown memories, with correction, deletion, restoration, and graph tagging
- **Knowledge graph**: a fixed textbook knowledge graph plus per-user familiarity states (unfamiliar / familiar / mastered), each state explainable with supporting evidence
- **Smart chat**: Agentic RAG (multi-query retrieval + evidence loop), SSE streaming, answers with citations
- **Mistake notebook**: one-click saving of AI-chat problems, spaced-repetition review scheduling
- **Learning community**: discussion boards / study groups / check-ins (feature-flagged, off by default)
- **Auth**: embedded JWT auth service; login-free experience in development mode

## Tech Stack

- **Backend**: Python 3.13 · FastAPI (single-process, multi-domain) · SQLAlchemy 2 + Alembic (five independent migration chains) · LangGraph workers · PostgreSQL 17
- **Frontend**: React 19 · TypeScript · Vite · KaTeX (math rendering)
- **Tooling**: uv · Ruff · mypy · pytest · Vitest · Playwright · Docker Compose

## Quick Start

### Prerequisites

- Python 3.13 (managed with [uv](https://docs.astral.sh/uv/))
- Node.js 20+ / npm
- Docker (with the Compose plugin)

### Local development

```bash
# 1. Install dependencies (dev includes pytest/ruff/mypy; ocr includes pypdf)
uv sync --extra dev --extra ocr

# 2. Configure environment variables (.env is not committed)
cp .env.example .env

# 3. Start local PostgreSQL (note: port 55432, not the default 5432)
docker compose up -d --wait postgres

# 4. Run migrations for each domain chain, then sync the knowledge-graph registry
uv run alembic upgrade head                                  # memory chain
uv run alembic -c auth_alembic.ini upgrade head              # auth chain
uv run alembic -c conversation_alembic.ini upgrade head      # conversation chain
uv run alembic -c community_alembic.ini upgrade head         # community chain
uv run python -m backend.memory.cli sync-knowledge-graph --apply

# 5. Start the memory-api (single entry point; mounts conversation/community routes by config)
uv run uvicorn backend.app:app --port 8000

# 6. In other terminals, start the background processes
uv run python -m backend.memory.worker.main           # memory worker (LangGraph)
uv run python -m backend.memory.worker.scheduler      # maintenance scheduler
uv run python -m backend.memory.worker.outbox_consumer
uv run python -m backend.conversation.worker.main     # conversation worker
uv run python -m backend.conversation.publisher.main  # conversation outbox publisher

# 7. Start the frontend
cd frontend
npm install
npm run dev
```

### Verify

| Entry point | URL |
| --- | --- |
| Frontend | http://localhost:5173 |
| API docs (OpenAPI) | http://localhost:8000/docs |
| Readiness check | http://localhost:8000/health/ready |

Dev Auth is on by default in development (`DEV_AUTH_ENABLED=true`): the Vite proxy injects `MEMORY_DEV_USER_ID` (from `frontend/.env`) into the `X-Dev-User-Id` header so visitors can browse without logging in. You can also pass it manually:

```bash
curl -H "X-Dev-User-Id: 00000000-0000-4000-8000-000000000001" \
  http://localhost:8000/health/ready
```

### Docker

```bash
docker compose up --build            # postgres + memory-api (127.0.0.1:8001) + workers
docker compose --profile frontend up # adds a frontend preview (127.0.0.1:4173)
```

### RAG (optional)

RAG uses a fully isolated PostgreSQL database (port 55433):

```bash
docker compose -f docker-compose.rag.yml up -d --wait rag-postgres
uv run alembic -c rag_alembic.ini upgrade head
```

## Project Structure

```
xueshen-math/
├── backend/
│   ├── app.py                  # Single FastAPI entry point (mounts domain routers by config)
│   ├── memory/                 # Core long-term memory domain: API → operation queue → LangGraph worker
│   │                           #   → transactional commit + outbox; Markdown bodies, PostgreSQL index/events
│   ├── auth_service/           # Embedded JWT issuer (same process as memory-api)
│   ├── auth/                   # JWT verifier
│   ├── conversation/           # Agentic RAG chat domain (own DB + worker/publisher, SSE streaming)
│   ├── community/              # Community domain (own DB; routes not mounted when unconfigured)
│   ├── rag/                    # RAG import/retrieval (own DB, reused by conversation retrieval)
│   └── settings.py             # Centralized environment-variable settings
├── frontend/                   # React 19 + Vite + TypeScript
├── tests/                      # unit / integration / contract / conversation / community / rag / failure_recovery
├── scripts/                    # ci-local.sh, backup/restore, OCR/embedding tools, auth key generation, etc.
├── docs/                       # Implementation plans and ops manuals (ops/startup, failure-runbook, backup-restore)
├── knowledge_graph/            # Authoritative knowledge-graph data (mounted read-only at runtime)
├── alembic.ini / *_alembic.ini / *_migrations/  # Five independent Alembic migration chains
├── docker-compose.yml          # postgres + memory-api + workers; frontend profile
├── docker-compose.rag.yml      # Dedicated RAG PostgreSQL (55433)
└── memory-manager-execution-spec*.md  # Execution specs (build baseline)
```

## Architecture Overview

The chat path is a bounded Agentic RAG workflow orchestrated by LangGraph, not a single “retrieve then answer” call. The vector store itself is not a graph node: `retrieve_subquery` calls `RetrievalService` through `AsyncRetrieverAdapter`, which then accesses the isolated RAG PostgreSQL + pgvector database.

```mermaid
flowchart TD
    FE["React frontend"]
    API["Conversation API<br/>transaction intake + SSE"]
    CDB[("Conversation PostgreSQL")]
    WORKER["conversation-worker<br/>claim / lease / fencing"]

    subgraph GRAPH["Conversation Agentic RAG · LangGraph"]
        START(["START: user message persisted"])
        LOAD["load_conversation_context<br/>load summary and recent messages"]
        MEMORY["recall_memory<br/>single long-term-memory read per turn"]
        SNAPSHOT["build_turn_snapshot<br/>immutable TurnContextSnapshot"]
        REWRITE["rewrite_and_plan<br/>standalone question + N subqueries"]
        ROUTE{"need_retrieval?"}
        EMBED["embed_subqueries<br/>batch query embeddings"]
        FANOUT["dispatch_retrieval_workers<br/>LangGraph Send × N"]
        RETRIEVE["retrieve_subquery<br/>one worker per subquery"]
        AGG["aggregate_results<br/>deterministic Map Reducer"]
        RERANK["deduplicate_and_rerank<br/>dedupe, neighbor merge, evidence budget"]
        EVAL["evaluate_evidence<br/>structured sufficiency check"]
        ENOUGH{"evidence sufficient or budget exhausted?"}
        ANSWER["generate_answer<br/>streamed structured answer"]
        VALIDATE["validate_answer_and_citations<br/>server-side citation validation"]
        FINALIZE["persist_turn<br/>messages, events, and Memory Outbox"]
        ACK["explicit_remember_ack<br/>explicit-memory acknowledgement"]
        END(["END"])

        START --> LOAD --> MEMORY --> SNAPSHOT --> REWRITE --> ROUTE
        ROUTE -->|"no"| ANSWER
        ROUTE -->|"yes"| EMBED --> FANOUT
        FANOUT -. "Send subqueries 1..N" .-> RETRIEVE
        RETRIEVE -. "WorkerResult Map Reducer" .-> AGG
        AGG --> RERANK --> EVAL --> ENOUGH
        ENOUGH -->|"missing evidence and budget remains"| REWRITE
        ENOUGH -->|"sufficient / insufficient with exhausted budget"| ANSWER
        ANSWER --> VALIDATE --> FINALIZE
        FINALIZE -->|"normal turn"| END
        FINALIZE -->|"explicit_remember"| ACK --> END
    end

    OAI["OpenAI SDK Gateway<br/>Responses API + Structured Outputs"]
    EMBGW["QueryEmbeddingGateway<br/>OpenAI-compatible Embeddings"]
    RETGW["AsyncRetrieverAdapter<br/>bounded concurrency + timeout"]
    RAGSVC["RetrievalService<br/>HNSW + FTS + formula search → RRF"]
    RAGDB[("isolated RAG PostgreSQL<br/>pgvector + FTS")]
    MEMAPI["Memory API / MemoryClient"]
    MEMGRAPH["MemoryManagerGraph<br/>extract, review, merge, graph projection"]
    MEMDB[("Memory PostgreSQL + Markdown")]
    OUTBOX["conversation-outbox-publisher"]

    FE -->|"send message"| API
    API -->|"atomically persist message, Turn, accepted event"| CDB
    CDB -->|"poll and claim"| WORKER
    WORKER --> START
    CDB -->|"persisted Turn Events"| API
    API -->|"SSE"| FE

    MEMORY -. "MemoryGateway" .-> MEMAPI
    REWRITE -.-> OAI
    EVAL -.-> OAI
    ANSWER -.-> OAI
    EMBED -.-> EMBGW
    RETRIEVE -.-> RETGW
    RETGW --> RAGSVC --> RAGDB
    FINALIZE -. "transactional commit" .-> CDB
    CDB --> OUTBOX -->|"conversation evidence / source deletion"| MEMAPI
    MEMAPI --> MEMGRAPH --> MEMDB
```

In brief:

- **LangGraph controls the workflow**: it loads conversation and long-term memory into one immutable snapshot, then uses the OpenAI SDK to produce a structured retrieval plan. Questions that do not need textbook evidence go directly to answering; the rest enter the parallel multi-query path.
- **RAG is a service behind graph nodes**: queries are embedded in one batch and fanned out with `Send × N` to independent `retrieve_subquery` workers. `RetrievalService` fuses HNSW vector, Chinese FTS, and formula-search rankings with RRF.
- **The evidence loop is strictly bounded**: results are deduplicated across subqueries, adjacent chunks are merged, ranking is deterministic, and the evidence set is trimmed to a token budget. The graph returns to `rewrite_and_plan` only when evidence is still missing and retrieval budget remains.
- **Answers and citations are traceable**: the OpenAI SDK streams one structured answer; citation content and IDs are generated and validated from the server-owned final evidence set. Persisted Turn Events are then delivered over SSE.
- **Memory and RAG remain separate domains**: long-term memory is read once per turn. After the answer is committed, the Conversation Outbox reliably submits conversation evidence to MemoryManagerGraph; Conversation, RAG, and Memory use isolated databases.

Key concepts:

- **MemoryManagerGraph**: an internal asynchronous memory-processing workflow (extract / review / merge / graph projection). It is never exposed to browsers; external systems interact through Gateway / MemoryClient
- **Domain isolation**: memory, auth, conversation, community, and rag each have their own database and least-privilege accounts
- **Error envelope**: every domain uses a unified `PublicError` (`code` / `message` / `retryable` / `trace_id`); 422 distinguishes `REQUEST_EXTRA_FIELD` / `INVALID_PAYLOAD`
- **Feature flags off by default**: the three community pipelines, conversation memory_read/submit, etc. — "implementation does not mean approval; enabling requires approval"

## Common Tasks

### Local CI

```bash
scripts/ci-local.sh                     # all stages
scripts/ci-local.sh backend-lint        # Ruff + mypy
scripts/ci-local.sh backend-unit        # unit tests (no database needed)
scripts/ci-local.sh backend-integration # creates *_test databases and migrates them (needs Docker)
scripts/ci-local.sh frontend            # frontend lint + vitest + build
scripts/ci-local.sh contracts           # contract tests
```

### Testing

```bash
# Backend unit tests (no database needed)
uv run pytest tests/unit tests/test_mineru_ocr_*.py

# Integration tests (conftest rejects any database that is not *_test; the ci-local entry is easiest)
scripts/ci-local.sh backend-integration

# Contract tests; update the OpenAPI snapshot after route/schema changes
uv run pytest tests/contract
UPDATE_OPENAPI_SNAPSHOT=1 .venv/bin/python -m pytest tests/contract -q

# Frontend
cd frontend && npm run lint && npm run test && npm run build
```

### Backup & Restore

```bash
scripts/backup.sh    # age-encrypted backup (see docs/ops/backup-restore.md)
scripts/restore.sh
```

## Configuration

All environment variables are centralized in `backend/settings.py`; see `.env.example` for a full sample. Frequently used ones:

| Variable | Purpose | Default |
| --- | --- | --- |
| `DATABASE_URL` | memory database DSN | `postgresql+psycopg://memory:memory@127.0.0.1:55432/memory` |
| `DEV_AUTH_ENABLED` | dev identity simulation (development only) | `true` |
| `AUTH_ISSUER` / `AUTH_AUDIENCE` | JWT issuer and audience | `gewu-auth` / `memory-api` |
| `CONVERSATION_DATABASE_URL` | conversation database DSN | `…@127.0.0.1:55432/conversation` |
| `COMMUNITY_DATABASE_URL` | community database DSN; routes are not mounted and readiness does not fail when unset | — |
| `RAG_DATABASE_URL` | isolated RAG database DSN | `…@127.0.0.1:55433/rag` |
| `OPENAI_API_KEY` / `OPENAI_BASE_URL` | LLM credentials and endpoint | — |

In production (`APP_ENV=production`) `Settings` construction enforces hard requirements: an RSA-2048 private key with exactly `0600` permissions, a matching public key, and an explicit `AUTH_DATABASE_URL` — startup fails immediately without them. For local development, use `scripts/generate_auth_keys.sh` to generate a key pair.

## Troubleshooting

| Symptom | Fix |
| --- | --- |
| `/health/ready` reports `knowledge_graph_registry_not_loaded` | `uv run python -m backend.memory.cli sync-knowledge-graph --apply` |
| Integration tests refuse the database | Only `*_test` databases are accepted; use `scripts/ci-local.sh backend-integration`, which handles this automatically |
| Cannot connect on local 5432 | This project uses non-default port **55432** (RAG: 55433); check `docker compose ps` |
| Production mode crashes at startup | Missing hard-required auth config (`AUTH_PRIVATE_KEY_FILE` RSA2048 with 0600, etc.); see the auth section comments in `.env.example` |
| Frontend requests on 5173 fail to proxy | Ensure the backend is running on 8000; check `MEMORY_DEV_API_TARGET` / `MEMORY_DEV_USER_ID` in `frontend/.env` |
| Chat does not respond | Ensure the conversation worker/publisher processes are running and the `OPENAI_*` model-role settings are configured |

For more operational details see `docs/ops/` (startup.md / failure-runbook.md / backup-restore.md).

## Documentation Index

- **Execution specs (build baseline)**: `memory-manager-execution-spec-v1.1.md`; architecture principles: `memorymangergraph.md`; gap rulings: `memory-manager-execution-spec-gap-analysis.md`
- **Implementation plans**: `docs/auth-service-implementation-plan.md`, `docs/conversation-agentic-rag-implementation-plan.md`, `docs/community-implementation-plan.md`, `docs/rag-phase3.md`
- **Operations**: `docs/ops/startup.md`, `docs/ops/failure-runbook.md`, `docs/ops/backup-restore.md`
- **Developer / AI conventions**: `AGENTS.md`

In-code references like `规格 §X` / `方案 §X` point at the documents above; read the relevant section before changing behavior.

## Development Conventions

- Comments, docstrings, and commit messages are written in Simplified Chinese; commit style: `feat|fix|chore(域): 中文描述`
- Ruff line length is 100; `backend/**/api/` ignores B008 (FastAPI `Depends` factory pattern)
- `scripts/` (OCR/embedding tools) is outside the lint gate
- Do not enable feature flags without approval

## License

Private project; no open-source license is provided.

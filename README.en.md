# xueshen-math

> 中文版：[README.md](./README.md)
>
> **Documentation baseline**: `ff01255` (August 16, 2026), which merges and closes review for Study Orchestration Phase 0–4. This file documents committed functionality only; uncommitted worktree changes are intentionally excluded.

**MemoryManagerGraph** is a long-term memory, knowledge-graph, and study-orchestration system for math learning. The project separates conversations, learning evidence, study plans, textbook retrieval, and community activity into isolated domains: Memory stores explainable learning facts, Conversation provides Agentic RAG math tutoring, Study turns goals into executable plans, RAG retrieves textbook evidence, and Community supports peer learning.

## Features and Status

| Feature | Committed capabilities | Default status |
| --- | --- | --- |
| **MemoryManagerGraph** | Async evidence extraction, candidate review, Markdown version commits, correction/deletion/restoration, and per-user knowledge-graph overlay | Core domain |
| **Knowledge graph** | Fixed textbook graph plus auditable user mastery state, evidence, and recommendation signals | Core domain |
| **Smart chat** | LangGraph Agentic RAG, parallel multi-query retrieval, bounded evidence loop, structured answers, citation validation, and SSE events | Agentic RAG on; Memory transport is flag-controlled |
| **Study Orchestration** | Structured/conversational intake, AI plan generation, deterministic scheduling, tasks and Sessions, Daily Feed, recommendations, automatic replan, and Memory writeback Outbox | **Entire Study domain off by default** |
| **Textbook RAG** | Isolated pgvector database, HNSW vector search, Chinese FTS, formula search, RRF fusion, metadata filters, and page citations | Optional isolated service |
| **Learning community** | Boards, posts, replies, likes, notifications, Community Outbox, and Activity Publisher | Routes depend on DB config; publishing and Memory delivery off by default |
| **Auth** | Embedded JWT issuer, isolated Auth database, production RSA validation, and Dev Auth | Login-free local development |
| **Frontend** | React/Vite SPA for chat, plans, knowledge map, notebook, community, memory profile, and unified notifications | Connects through the Vite proxy |

> Study backend Phase 0–4 is committed, but “implemented” does not mean “approved to enable”: `STUDY_DOMAIN_ENABLED`, Memory read, Daily Feed, auto-replan, Memory writeback, and notification flags all default to `false`. The current frontend `Plan` page still provides the compatibility Memory-learner display and onboarding; the real Study API flow needs to be wired after enablement approval.

## Tech Stack

- **Backend**: Python 3.13 · FastAPI (single-process, multi-domain) · SQLAlchemy 2 · Alembic · LangGraph · PostgreSQL 17
- **Domain databases**: five local PostgreSQL databases (memory, auth, conversation, community, study) plus an independently deployed rag database; six migration chains in total
- **Frontend**: React 19 · TypeScript · Vite · KaTeX
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

# 4. Run migrations for configured domain databases, then sync the graph registry
uv run alembic upgrade head                                  # memory chain
uv run alembic -c auth_alembic.ini upgrade head              # auth chain
uv run alembic -c conversation_alembic.ini upgrade head      # conversation chain
uv run alembic -c community_alembic.ini upgrade head         # community chain
uv run python -m backend.memory.cli sync-knowledge-graph --apply

# 5. Optional Study chain: configure STUDY_DATABASE_URL before enabling Study
STUDY_DATABASE_URL='postgresql+psycopg://study:study@127.0.0.1:55432/study' \
  uv run alembic -c study_alembic.ini upgrade head

# 6. Start memory-api (the single entry point; routers mount by configuration)
uv run uvicorn backend.app:app --port 8000

# 7. In other terminals, start background processes
uv run python -m backend.memory.worker.main
uv run python -m backend.memory.worker.scheduler
uv run python -m backend.memory.worker.outbox_consumer
uv run python -m backend.conversation.worker.main
uv run python -m backend.conversation.publisher.main

# Start these only after enabling Study
uv run python -m backend.study.worker.main
uv run python -m backend.study.scheduler.main
uv run python -m backend.study.publisher.main          # only for approved Memory writeback

# 8. Start the frontend
cd frontend
npm install
npm run dev
```

Study routes mount under `/api/v1/study` only when `STUDY_DOMAIN_ENABLED=true` and `STUDY_DATABASE_URL` is configured. The Study Publisher exits cleanly when its Memory writeback flag or internal Memory credentials are missing; this is fail-closed behavior, not a startup bug.

### Verify

| Entry point | URL |
| --- | --- |
| Frontend | http://localhost:5173 |
| API docs (OpenAPI) | http://localhost:8000/docs |
| Readiness check | http://localhost:8000/health/ready |
| Study API (when enabled) | http://localhost:8000/api/v1/study |

Dev Auth is on by default in development (`DEV_AUTH_ENABLED=true`): the Vite proxy injects `MEMORY_DEV_USER_ID` (from `frontend/.env`) into the `X-Dev-User-Id` header so visitors can browse without logging in. You can also pass it manually:

```bash
curl -H "X-Dev-User-Id: 00000000-0000-4000-8000-000000000001" \
  http://localhost:8000/health/ready
```

### Docker

```bash
docker compose up --build            # postgres + memory-api (127.0.0.1:8001) + Memory/Conversation workers
docker compose --profile frontend up # adds a frontend preview (127.0.0.1:4173)
```

The main Compose file does not enable Study or start its Worker/Scheduler/Publisher by default. When Study is enabled, start those roles from the host commands above and use the independent `study` database. Community database initialization is available through Compose, but Community Publisher, Memory evidence delivery, and source deletion remain feature-flagged.

### RAG (optional)

RAG uses a fully isolated PostgreSQL + pgvector database on port 55433 and never reads or modifies the Memory database:

```bash
docker compose -f docker-compose.rag.yml up -d --wait rag-postgres
RAG_DATABASE_URL='postgresql+psycopg://rag:rag@127.0.0.1:55433/rag' \
  uv run alembic -c rag_alembic.ini upgrade head
```

Import chunk and embedding artifacts with their manifest and hashes:

```bash
RAG_DATABASE_URL='postgresql+psycopg://rag:rag@127.0.0.1:55433/rag' \
  uv run python scripts/rag_import.py \
  --chunk-root <chunk-artifact-root> \
  --embedding-root <embedding-artifact-root>
```

## Project Structure

```text
xueshen-math/
├── backend/
│   ├── app.py                  # Single FastAPI entry point; routers mount by config
│   ├── memory/                 # MemoryManagerGraph: operations, LangGraph, Markdown, graph overlay
│   ├── auth_service/           # Embedded JWT issuer; only this process holds the signing key
│   ├── auth/                   # JWT verification, user_id/scope context, and permissions
│   ├── conversation/           # Agentic RAG: API, LangGraph worker, SSE Turn Events, Outbox
│   ├── community/              # Community API, isolated DB, Activity Publisher, internal Reader
│   ├── study/                  # Plans, tasks, Sessions, Feed, Graphs, Scheduler, Publisher
│   ├── rag/                    # RAG import/retrieval service; connects to the isolated rag DB
│   ├── integrations/           # Cross-domain Reader adapters
│   └── settings.py             # Environment variables, flags, and production validation
├── frontend/                   # React 19 + Vite + TypeScript SPA
├── knowledge_graph/            # Authoritative textbook graph data, mounted read-only
├── tests/                      # unit / integration / contract / conversation / community / study / rag
├── scripts/                    # CI, backup/restore, OCR, embeddings, RAG import, and auth keys
├── docs/                       # Domain plans, gap analysis, service tokens, and operations manuals
├── alembic.ini                 # memory migration chain
├── auth_alembic.ini            # auth migration chain
├── conversation_alembic.ini    # conversation migration chain
├── community_alembic.ini       # community migration chain
├── study_alembic.ini           # study migration chain
├── rag_alembic.ini             # rag migration chain
├── *_migrations/               # Independent migration directories; RAG uses rag_migrations/
├── docker-compose.yml          # Shared PostgreSQL, API, and Memory/Conversation workers
├── docker-compose.rag.yml      # Independent RAG PostgreSQL (55433)
└── memory-manager-execution-spec*.md  # Memory execution specs and architecture baseline
```

## System Architecture: One API, Isolated Domains

The browser talks to the FastAPI entry point and public APIs. Domains share process-level authentication and HTTP infrastructure, but keep their databases, migration chains, workers, and cross-domain write boundaries separate. Cross-database writes use an Outbox and eventual consistency instead of distributed transactions.

```mermaid
flowchart LR
    FE["React frontend<br/>Chat / Plan / Map / Notebook / Community"] --> API["Single FastAPI entry point<br/>auth, routers, errors, readiness"]

    subgraph DOMAINS["Business domains"]
        MEM["MemoryManagerGraph<br/>long-term memory + graph"]
        CHAT["Conversation<br/>Agentic RAG + SSE"]
        STUDY["Study<br/>plans, tasks, Feed, Replan"]
        COMM["Community<br/>posts, replies, notifications"]
        AUTH["Auth Service<br/>JWT issuer/verifier"]
    end

    API --> MEM
    API --> CHAT
    API --> STUDY
    API --> COMM
    API --> AUTH

    MEM --> MEMDB[("Memory DB<br/>+ Markdown")]
    CHAT --> CHATDB[("Conversation DB")]
    STUDY --> STUDYDB[("Study DB")]
    COMM --> COMMDB[("Community DB")]
    AUTH --> AUTHDB[("Auth DB")]

    CHAT -. "retrieval only" .-> RAG["RetrievalService"]
    RAG --> RAGDB[("Independent RAG DB<br/>pgvector + FTS")]
    CHAT -. "evidence / explicit memory" .-> MEM
    STUDY -. "Memory context / Outbox" .-> MEM
    COMM -. "activity evidence / source deletion" .-> MEM

    MEM -. "LangGraph worker / scheduler / outbox" .-> MEMDB
    CHAT -. "worker / publisher" .-> CHATDB
    STUDY -. "worker / scheduler / publisher" .-> STUDYDB
    COMM -. "lifespan publisher" .-> COMMDB
```

### Architecture Principles

- **Separate facts of record**: Study DB owns plans, tasks, Sessions, and Daily Feed; Conversation owns threads/messages/Turn Events; RAG owns textbook corpora; Memory owns long-term memory and graph state.
- **Models do not own side effects**: OpenAI returns structured understanding, plan blueprints, or language; deterministic code owns dates, budgets, prerequisites, state transitions, conflicts, and progress.
- **Recoverable async work**: long-running or cross-domain writes first persist to a domain operation/outbox, then run behind leases, fencing, idempotency keys, and checkpoints.
- **No hidden read-side effects**: Study `GET /home` only reads persisted results; Daily Feed creation is explicit through `ensure-today` or the Scheduler.
- **Implementation is not approval**: Community Memory delivery, Conversation Memory read/write, and all Study flags are off by default.

## Feature Architecture

### 1. MemoryManagerGraph: Long-Term Memory and Knowledge Graph

MemoryManagerGraph is an internal asynchronous workflow. It is not exposed to browsers and does not directly manipulate files. Other domains enter through Memory Gateway/MemoryClient and an operation queue; all persistent writes go through MemoryService.

```mermaid
flowchart LR
    EVIDENCE["Conversation / Community / Study<br/>learning evidence"] --> GATEWAY["Memory Gateway<br/>auth, schema, idempotency, user_id"]
    COMMAND["User commands<br/>correct / delete / restore / graph state"] --> GATEWAY
    GATEWAY --> OPS[("memory_operations<br/>PostgreSQL")]
    OPS --> WORKER["Memory Worker<br/>claim / lease / checkpoint"]

    subgraph GRAPH["MemoryManagerGraph"]
        NORMALIZE["normalize_input<br/>invariants"] --> ROUTE{"route"}
        ROUTE --> SUMMARY["SummaryMemoryGraph<br/>extract, value, MutationPlanDraft"]
        ROUTE --> KG["KnowledgeGraphStateGraph<br/>deterministic mastery/recommendations"]
        ROUTE --> CMD["Deterministic commands<br/>correct, delete, restore"]
        ROUTE --> MAINT["Maintenance<br/>rebuild, cleanup, recovery"]
    end

    WORKER --> NORMALIZE
    SUMMARY --> SERVICE["MemoryService<br/>only persistence write entry"]
    KG --> SERVICE
    CMD --> SERVICE
    MAINT --> SERVICE
    SERVICE --> MD["Markdown storage<br/>immutable versions + atomic active version"]
    SERVICE --> INDEX[("Memory PostgreSQL<br/>index, commit, audit, outbox")]
    SERVICE --> OUTBOX["memory_outbox<br/>notifications / cross-domain events"]

    CLIENT["MemoryClient / LearningContextService"] --> READ["read-only context assembly"]
    READ --> MD
    READ --> INDEX
    READ --> KGREAD["fixed textbook graph<br/>+ user overlay"]
```

**Boundaries:**

- SummaryMemoryGraph may call OpenAI for structured candidate extraction, but the model only produces a schema-validated `MutationPlanDraft`; the application adds stable IDs, `expected_version`, paths, and the final commit plan.
- KnowledgeGraphStateGraph validates fixed-graph `node_id` values, updates the user overlay, derives recommendation signals, and writes audit records without letting a model decide database state transitions.
- Markdown is the auditable version source for memory bodies; PostgreSQL stores indexes, operations, commits, audits, and Outbox events. Deletion and restoration follow version and tombstone protocols.
- `MemoryClient` is the only access boundary for Conversation, Study, Community, and other domains; business code must not call graph nodes, databases, or Markdown files directly.

### 2. Conversation: Agentic RAG Math Chat

Conversation is a bounded, recoverable LangGraph workflow rather than a single “retrieve then answer” call. The API atomically accepts messages and serves SSE; the Worker runs orchestration; the Publisher reliably submits evidence to Memory.

```mermaid
flowchart TD
    FE["React Chat"] --> API["Conversation API<br/>transaction intake + SSE"]
    API --> CDB[("Conversation DB<br/>threads / messages / turns / events")]
    CDB --> CLAIM["conversation-worker<br/>claim / lease / fencing"]

    subgraph GRAPH["Conversation Agentic RAG · LangGraph"]
        START(["START: message persisted"]) --> CONTEXT["load summary + recent messages"]
        CONTEXT --> MEMORY["optional Memory context<br/>at most once per turn"]
        MEMORY --> SNAPSHOT["immutable TurnContextSnapshot"]
        SNAPSHOT --> PLAN["rewrite_and_plan<br/>standalone question + subqueries"]
        PLAN --> NEED{"Need textbook evidence?"}
        NEED -->|"no"| ANSWER["generate_answer<br/>structured streaming answer"]
        NEED -->|"yes"| EMBED["batch query embeddings"]
        EMBED --> FANOUT["LangGraph Send × N"]
        FANOUT --> RETRIEVE["independent retrieve_subquery workers"]
        RETRIEVE --> AGG["Map Reducer aggregation"]
        AGG --> RERANK["dedupe, neighbor merge, deterministic rerank"]
        RERANK --> EVAL["evaluate_evidence<br/>sufficiency + budget"]
        EVAL --> LOOP{"Evidence missing and budget remains?"}
        LOOP -->|"yes"| PLAN
        LOOP -->|"no"| ANSWER
        ANSWER --> CITATION["server-generated and validated citations"]
        CITATION --> FINALIZE["persist Turn Events + Memory Outbox"]
    end

    CLAIM --> START
    RETRIEVE -. "AsyncRetrieverAdapter" .-> RETRIEVER["RetrievalService"]
    RETRIEVER --> RAGDB[("Independent RAG DB<br/>HNSW + Chinese FTS + formula")]
    PLAN -. "structured RewritePlan" .-> LLM["OpenAI SDK Gateway"]
    EVAL -. "structured EvidenceAssessment" .-> LLM
    ANSWER -. "Structured Outputs" .-> LLM
    FINALIZE --> CDB
    CDB -->|"Turn Events"| API
    API -->|"SSE"| FE
    CDB --> OUTBOX["conversation-outbox-publisher"]
    OUTBOX -. "conversation evidence / source deletion" .-> MEM["Memory API / MemoryManagerGraph"]
```

**Boundaries:**

- `rewrite_and_plan` creates independent subqueries, embeddings are batched, and `Send × N` runs retrieval in parallel. A deterministic reducer, RRF/reranking, and token budget produce the final evidence set.
- The graph loops only when evidence is missing and budget remains. The answer uses the final server-owned evidence set; citation IDs are generated and validated server-side.
- Conversation reads long-term memory at most once per turn. After commit, evidence goes through the Conversation Outbox; Conversation, RAG, and Memory never write directly across databases.
- SSE exposes persisted Turn Events rather than treating an LLM stream as the source of truth. Worker lease/checkpoint recovery handles restarts.

### 3. Study: Plan and Proactive Learning Orchestration

Study is the newest independent backend domain in the baseline. It turns a goal, deadline, weekly availability, and daily minute budget into plans, revisions, tasks, Sessions, Daily Feed, and seven-day activity statistics. Study DB is the source of truth for execution data; Memory only provides long-term context and receives asynchronous writeback.

```mermaid
flowchart LR
    PLAN_UI["Plan UI / conversational intake"] --> API["Study API<br/>/api/v1/study"]
    HOME_UI["Today home"] --> API
    API --> DB[("Study DB<br/>plans / revisions / tasks / sessions / feed / stats")]

    API -. "synchronous single-turn intake" .-> INTAKE["Plan Intake Graph<br/>extract → validate → clarify → preview"]
    INTAKE -. "structured model call" .-> LLM["OpenAI SDK<br/>structured output only"]
    API --> OPS[("Study Operations<br/>idempotency + lease")]
    ENSURE["POST /home/ensure-today<br/>explicit Feed operation"] --> OPS
    SCHED["Study Scheduler<br/>user IANA timezone / 300s scan"] --> OPS

    OPS --> WORKER["Study Worker<br/>serialize per user, parallelize users"]
    subgraph GRAPHS["Study LangGraph async graphs"]
        PLAN["Plan Generation Graph<br/>Memory context → blueprint"]
        FEED["Daily Feed Graph<br/>formal tasks + up to two recommendations"]
        REPLAN["Replan Graph<br/>rollover / weekly review / revision"]
    end
    WORKER --> PLAN
    WORKER --> FEED
    WORKER --> REPLAN

    PLAN --> RULES["Deterministic scheduler<br/>dates, timezone, budget, prerequisites, conflicts, progress"]
    FEED --> RULES
    REPLAN --> RULES
    RULES --> DB
    LLM -. "goal understanding / task copy / reasons" .-> PLAN
    LLM -. "Feed copy" .-> FEED

    PLAN -. "optional read" .-> MEMREAD["Memory Gateway<br/>learner / mastery / graph"]
    FEED -. "optional read" .-> MEMREAD
    REPLAN -. "optional read" .-> MEMREAD
    DB --> OUTBOX["Study Outbox Publisher"]
    OUTBOX -. "plan summary / activity evidence" .-> MEMWRITE["Memory API<br/>async writeback"]
```

**Boundaries:**

- Intake synchronously extracts and clarifies a structured `PlanIntent`; only confirmation creates a plan-generation operation. Textual AI output cannot become an active plan directly.
- OpenAI creates a blueprint constrained to backend-approved topics. Deterministic code owns final dates, timezone/DST, daily budget, prerequisites, rest days, task splitting, collision checks, and progress semantics.
- `GET /home` has no side effect. The frontend calls `ensure-today` when `generation_status=pending`, while the Scheduler retries by `(user_id, plan_id, local_date)`. Formal tasks and adaptive recommendations are separate; accepting a recommendation is the only path that creates a formal task.
- Replan changes only future, incomplete, unlocked tasks. High-impact changes create a proposed revision; the user accepts/rejects it with `expected_version` CAS protection.
- Manual actions are the only formal completion source in v1. Session heartbeats record real active minutes and cannot be replaced by a task-complete call. Study flags are off by default.

### 4. RAG: Textbook Ingestion and Evidence Retrieval

RAG is an independent data and retrieval domain. It keeps embeddings, chunks, and textbook data out of Memory; Conversation reads an active corpus through `RetrievalService`.

```mermaid
flowchart LR
    ARTIFACT["OCR / chunk / embedding artifacts<br/>manifest + SHA-256"] --> IMPORT["rag_import.py<br/>validate links, dimensions, counts, hashes"]
    IMPORT --> VERSION["immutable corpus_version<br/>loading → ready → active"]
    VERSION --> RAGDB[("RAG PostgreSQL 55433<br/>pgvector + FTS + formula_terms")]

    QUERY["Conversation subquery"] --> EMBED["QueryEmbeddingGateway"]
    EMBED --> RETRIEVAL["RetrievalService"]
    RETRIEVAL --> VECTOR["HNSW cosine<br/>vector Top-K"]
    RETRIEVAL --> FTS["Chinese simple FTS<br/>terms / bigrams"]
    RETRIEVAL --> FORMULA["normalized formula + GIN<br/>exact match"]
    VECTOR --> RRF["RRF fusion + retrieval_weight<br/>filters and citation budget"]
    FTS --> RRF
    FORMULA --> RRF
    RRF --> EVIDENCE["Evidence blocks<br/>book / chapter / page / source_refs"]
    RAGDB --> RETRIEVAL
    EVIDENCE --> QUERY
```

Each import run records status, counts, artifact hash, and errors; the same `(chunk_build_id, embedding_profile_id)` is idempotent. Retrieval combines HNSW, Chinese FTS, and formula matching before applying book, grade, chapter, content-role, and page filters. A failed new import does not replace the previous active corpus.

### 5. Community: Learning Community and Activity Evidence

Community uses an isolated database for high-frequency public content. It does not create cross-database Auth foreign keys and stores only `user_id`. The Publisher runs in the FastAPI lifespan rather than as a separate port, converting community activity into controlled Activity Evidence or source-deletion requests.

```mermaid
flowchart LR
    UI["Community UI"] --> API["Community API<br/>boards / posts / replies / likes / notifications"]
    API --> AUTH["Auth Context<br/>user_id + scope"]
    API --> DB[("Community DB<br/>posts / replies / outbox / notifications")]
    DB --> PUB["ActivityPublisher<br/>claim + lease + retry + dead-letter"]
    PUB -. "COMMUNITY_MEMORY_SUBMIT_ENABLED" .-> READER["Community Source Reader"]
    READER -. "ActivityEvidence" .-> MEM["Memory API / MemoryManagerGraph"]
    PUB -. "COMMUNITY_SOURCE_DELETION_ENABLED" .-> DELETE["Memory source deletion"]
```

Posts, replies, likes, and notifications commit within Community DB. Cross-domain delivery is eventually consistent through the Outbox. Publisher retry/dead-letter behavior is controlled by feature flags, source state, error classification, and fencing. `COMMUNITY_PUBLISHER_ENABLED`, `COMMUNITY_MEMORY_SUBMIT_ENABLED`, and `COMMUNITY_SOURCE_DELETION_ENABLED` default to `false`.

### 6. Auth: Identity and Domain Authorization

Auth Service runs in the same process as memory-api and signs JWTs; verification and permission dependencies live in `backend/auth/`. Domain APIs use `user_id` from the authenticated context and never trust a client-provided identity in the request body.

```mermaid
flowchart LR
    USER["Browser / client"] --> LOGIN["/api/v1/auth<br/>register / login / refresh"]
    LOGIN --> SERVICE["Embedded Auth Service<br/>JWT issuer"]
    SERVICE --> AUTHDB[("Auth DB<br/>users, refresh tokens, events")]
    KEYS["RSA private/public keys<br/>strict production checks"] --> SERVICE
    SERVICE --> TOKEN["JWT access token"]
    TOKEN --> REQUEST["Memory / Conversation / Study / Community request"]
    REQUEST --> VERIFY["Auth verifier<br/>issuer / audience / scope"]
    VERIFY --> ROUTES["Domain router<br/>user isolation + permission checks"]
    DEV["Dev Auth: X-Dev-User-Id"] -. "development only" .-> VERIFY
```

Production requires an RSA-2048 private key with exactly `0600` permissions, a matching public key, and an explicit `AUTH_DATABASE_URL`. Local development can use `DEV_AUTH_ENABLED=true` and `X-Dev-User-Id`. Internal Publishers use separate service tokens rather than user JWTs.

## Domain and Data Isolation

| Domain | Source of truth | Runtime roles | Cross-domain relationship |
| --- | --- | --- | --- |
| Memory | Memory PostgreSQL + Markdown | API, Worker, Scheduler, Outbox Consumer | Receives Conversation/Community/Study evidence; serves Memory context |
| Auth | Auth PostgreSQL | Embedded issuer + verifier dependencies | Provides identity, tokens, and scopes; no business-table foreign keys |
| Conversation | Conversation PostgreSQL | API, Agentic RAG Worker, Outbox Publisher | Reads RAG; optionally reads/submits Memory |
| Community | Community PostgreSQL | API + FastAPI lifespan Activity Publisher | Submits Activity Evidence/source deletion through Outbox |
| Study | Study PostgreSQL | API, Worker, Scheduler, Outbox Publisher | Reads Memory context through a Gateway; writes back asynchronously |
| RAG | Independent RAG PostgreSQL (55433) | Import CLI + RetrievalService | Provides textbook evidence to Conversation only |

All domains use a shared `PublicError` envelope (`code` / `message` / `retryable` / `trace_id`). Write APIs use idempotency keys and version/CAS protection. Cross-database writes follow “commit in source domain → Outbox → idempotent consumer.”

## Common Tasks

### Local CI

```bash
scripts/ci-local.sh                     # all stages
scripts/ci-local.sh backend-lint        # Ruff + mypy
scripts/ci-local.sh backend-unit        # unit tests (no database needed)
scripts/ci-local.sh backend-integration # creates/migrates memory/auth/conversation/community/study_test
scripts/ci-local.sh frontend            # frontend lint + vitest + build
scripts/ci-local.sh contracts           # contract tests
```

### Testing

```bash
# Backend unit tests (no database needed)
uv run pytest tests/unit tests/test_mineru_ocr_*.py

# Study-specific unit/integration coverage
uv run pytest tests/unit/test_study_*.py
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

## Configuration and Feature Flags

All environment variables are centralized in `backend/settings.py`; see `.env.example` for a complete sample.

| Variable | Purpose | Default |
| --- | --- | --- |
| `DATABASE_URL` | memory database DSN | `postgresql+psycopg://memory:memory@127.0.0.1:55432/memory` |
| `AUTH_DATABASE_URL` | auth database DSN | `…@127.0.0.1:55432/auth` |
| `CONVERSATION_DATABASE_URL` | conversation database DSN | `…@127.0.0.1:55432/conversation` |
| `COMMUNITY_DATABASE_URL` | community database; routes are not mounted when unset | — |
| `STUDY_DATABASE_URL` | study database; routes are not mounted when the domain flag is off or unset | — |
| `RAG_DATABASE_URL` | isolated RAG database | `…@127.0.0.1:55433/rag` |
| `DEV_AUTH_ENABLED` | development identity simulation | `true` |
| `OPENAI_API_KEY` / `OPENAI_BASE_URL` | LLM credentials and endpoint | — |

Key flags that are off by default or require explicit approval:

```text
# Study
STUDY_DOMAIN_ENABLED=false
STUDY_MEMORY_READ_ENABLED=false
STUDY_DAILY_FEED_ENABLED=false
STUDY_AUTO_REPLAN_ENABLED=false
STUDY_MEMORY_WRITEBACK_ENABLED=false
STUDY_NOTIFICATION_ENABLED=false

# Conversation cross-domain Memory transport
CONVERSATION_MEMORY_READ_ENABLED=false
CONVERSATION_MEMORY_SUBMIT_ENABLED=false

# Community Publisher / Memory evidence / source deletion
COMMUNITY_PUBLISHER_ENABLED=false
COMMUNITY_MEMORY_SUBMIT_ENABLED=false
COMMUNITY_SOURCE_DELETION_ENABLED=false
```

`CONVERSATION_AGENTIC_RAG_ENABLED`, `CONVERSATION_MULTI_QUERY_ENABLED`, `CONVERSATION_EVIDENCE_LOOP_ENABLED`, and `CONVERSATION_STREAMING_ENABLED` default to `true`; that does not grant Conversation permission to read or write Memory.

In production (`APP_ENV=production`), `Settings` enforces an RSA-2048 private key with exact `0600` permissions, a matching public key, and an explicit `AUTH_DATABASE_URL`; startup fails without them. For local development, use `scripts/generate_auth_keys.sh` to generate the key pair.

## Troubleshooting

| Symptom | Fix |
| --- | --- |
| `/health/ready` reports `knowledge_graph_registry_not_loaded` | Run `uv run python -m backend.memory.cli sync-knowledge-graph --apply` |
| `/health/ready` reports `study_database_not_configured` | Check `STUDY_DOMAIN_ENABLED` and `STUDY_DATABASE_URL`; keep the domain flag `false` when Study is not enabled |
| Study routes are missing from OpenAPI | Both the domain flag and database URL must be set; restart the API after changing `.env` |
| Study Worker does not generate a Feed | Set `STUDY_DAILY_FEED_ENABLED=true`, start Study Scheduler/Worker, and call `POST /home/ensure-today` after `GET /home` reports `generation_status=pending` |
| Integration tests reject the database | Only `*_test` databases are accepted; use `scripts/ci-local.sh backend-integration` |
| Local PostgreSQL 5432 is unavailable | This project uses **55432** (RAG: 55433); check `docker compose ps` |
| Production crashes at startup | Missing hard-required auth configuration (`AUTH_PRIVATE_KEY_FILE` RSA2048 with 0600, etc.); see `.env.example` |
| Frontend requests on 5173 fail to proxy | Ensure the backend runs on 8000; check `MEMORY_DEV_API_TARGET` / `MEMORY_DEV_USER_ID` in `frontend/.env` |
| Chat does not respond | Ensure conversation worker/publisher processes are running, `OPENAI_*` role settings are present, and the RAG DB is available when textbook evidence is needed |
| Study Publisher exits immediately | This is fail-closed by default: configure `MEMORY_API_BASE_URL`, `MEMORY_AGENT_TOKEN`, and explicitly enable `STUDY_MEMORY_WRITEBACK_ENABLED` |

For operational details see `docs/ops/` (startup.md / failure-runbook.md / backup-restore.md).

## Documentation Index

- **Memory architecture and execution specs**: [`memorymangergraph.md`](./memorymangergraph.md), [`memory-manager-execution-spec-v1.1.md`](./memory-manager-execution-spec-v1.1.md), [`memory-manager-execution-spec-gap-analysis.md`](./memory-manager-execution-spec-gap-analysis.md)
- **Conversation / Agentic RAG**: [`docs/conversation-decision-items.md`](./docs/conversation-decision-items.md), [`docs/conversation-gap-analysis.md`](./docs/conversation-gap-analysis.md)
- **Study Orchestration**: [`docs/study-plan-push-implementation-plan.md`](./docs/study-plan-push-implementation-plan.md)
- **Community**: [`docs/community-implementation-plan.md`](./docs/community-implementation-plan.md), [`docs/community-service-tokens.md`](./docs/community-service-tokens.md)
- **RAG**: [`docs/rag-phase3.md`](./docs/rag-phase3.md)
- **Operations**: [`docs/ops/startup.md`](./docs/ops/startup.md), [`docs/ops/failure-runbook.md`](./docs/ops/failure-runbook.md), [`docs/ops/backup-restore.md`](./docs/ops/backup-restore.md)
- **Developer / AI conventions**: [`AGENTS.md`](./AGENTS.md)

In-code references like `规格 §X` / `方案 §X` point at the documents above; read the relevant section before changing behavior.

## Development Conventions

- Comments, docstrings, and commit messages are written in Simplified Chinese; commit style: `feat|fix|chore(域): 中文描述`
- Ruff line length is 100; `backend/**/api/` ignores B008 (FastAPI `Depends` factory pattern)
- `scripts/` (OCR/embedding tools) is outside the lint gate
- Do not enable feature flags without approval

## License

Private project; no open-source license is provided.

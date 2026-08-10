# MemoryManagerGraph 第一版执行规格 v1.1

> **文档状态：1.1-approved / 正式批准，可施工**  
> **版本：1.1**  
> **更新时间：2026-08-10**  
> **合并来源：** `memory-manager-execution-spec.md` v1.0-draft、`memory-manager-execution-spec-gap-analysis.md`、v1.1 审阅裁决  
> **效力声明：** 本文件是 MemoryManagerGraph 第一版唯一施工规格。原 v1.0、缺口索引和上位架构中的示例如与本文件冲突，以本文件为准。

本文将 v1.0 已确定内容与 v1.1 补充裁决合并为一份正式批准的可执行规格。批准后允许立即开始完整本地实现，不限于工程骨架；未在本文中出现的外部 Secret、生产地址、网站统一认证参数和云服务器信息不阻塞本地施工，相关能力使用正式适配器接口、配置占位和开发适配器完成。


### v1.1 审阅裁决补充（2026-08-10）

本次将开发审阅中仍未闭合的 23 项问题写入正文，以下规则与各章节中的详细定义具有同等效力：

1. 增加可重建的 `memory_graph_links` 弱连接表；图谱投影和推荐从活动版本 link 读取。
2. `MemoryDocumentView` 改用 `memory_type` 作为 Pydantic discriminator。
3. `page_view/bookmark/check_in` 进入父图的确定性 activity exposure 分支。
4. 增加 `source_deletions` 事实表；本期只记录删除并抑制 Reader 返回。
5. 图谱状态写接口统一返回 `MemoryOperationResult`，clear 使用 DELETE query `expected_version`。
6. 明确 index.md 的 dirty、异步重建、版本 0 和 stale 语义。
7. 候选内容改为受控结构化联合，补齐审核跨字段校验。
8. 备份由开发者/主机 cron 执行，采用 age 加密；Scheduler 只检查并告警。
9. 账号删除逐表执行物理删除，长期只保留最小隐私审计和 manifest。
10. 日志 HMAC 与长期隐私 HMAC 分离，第一版不支持在线轮换。
11. 明确本地 Git 的 `.gitignore`、大数据产物边界和 commit 身份策略。
12. 补充 `MemoryGraphRunner` Protocol 与 `LocalLangGraphRunner` 实现边界。
13. 明确知识图谱 alias 的来源、同步和前端可见性。
14. `--allow-remove` 同步删除节点时同时处理 activity、Overlay、link 并写归档审计。
15. 增加身份映射维护 CLI，并提供 dry-run/冲突覆盖控制。
16. 取消结果增加 `cancelled_at`，补齐取消和身份/资源错误码。
17. 所有 cursor 使用独立 HMAC 签名并绑定路由、主体、筛选器和过期时间。
18. 指标使用 `prometheus-client`，禁止用户级高基数标签和原始错误文本。
19. 固定数据库 statement/lock timeout 配置。
20. 前端本地通过 Vite proxy 注入 Dev Auth，不把测试身份放进生产构建。
21. `SourceBundle.total_utf8_bytes` 按去重后的内容真实计算，超限返回 `SOURCE_TOO_LARGE`。
22. 固定知识图谱以只读挂载映射到 `/app/knowledge_graph`。
23. 对 evidence JSON 数组增加应用层和数据库条数上限。

---

## 1. 第一版范围与交付标准

### 1.1 当前项目基线

当前仓库具备：

- React/Vite 前端原型，数据仍为 Mock。
- 固定知识图谱文件：`knowledge_graph/教材目录.md` 与 `knowledge_graph/数学知识科技树关系图.md`。
- OCR 脚本和 pytest 测试。
- Python 3.13.12 虚拟环境。

当前仓库不具备：

- `backend/` 后端项目。
- 根目录 `pyproject.toml`、`uv.lock`。
- FastAPI、数据库迁移、PostgreSQL 和认证实现。
- 对话 Agent、用户动态 Agent 及其业务数据库。
- Memory Worker、Scheduler、Outbox Consumer。
- 项目根目录本地 Git 仓库和本地 CI 统一入口；本期不配置 remote。

### 1.2 本期实现范围

第一版必须实现：

1. `backend/memory/` 完整模块。
2. FastAPI Memory Gateway 和 OpenAPI 契约。
3. PostgreSQL DDL、Alembic migration 和知识图谱只读注册表同步命令。
4. Markdown 不可变版本存储、本地持久化卷实现和原子物化。
5. `MemoryService`、`KnowledgeGraphStateService`、`LearningContextService`。
6. `MemoryManagerGraph`、`SummaryMemoryGraph`、`KnowledgeGraphStateGraph` 和 Maintenance 分支。
7. PostgreSQL Worker、Scheduler 和 Outbox Consumer。
8. `ConversationReader`、`ActivityReader` 接口及测试实现；不实现对话 Agent 和用户动态 Agent。
9. Profile 与 KnowledgeMap 页面的真实 API 接入。
10. Docker Compose 单机本地开发基线，并为未来云端部署保留同构配置。
11. 单元、Graph、持久化、失败恢复、API 契约和前端必要测试。
12. 本地 CI 命令入口、开发文档和运维文档；本期不创建 GitHub Actions workflow。

本期不实现：

- 对话 Agent。
- 用户动态 Agent、论坛后端、错题上传服务和行为采集系统。
- LangGraph Agent Server。
- Redis、RabbitMQ、Kafka、向量数据库。
- 多服务器部署、对象存储和分布式文件锁。
- Memory API 自建注册、登录或密码管理。
- 远端生产部署和真实网站认证联调。

### 1.3 验收标准

交付必须同时满足：

- 本地执行 Docker Compose 后可完成数据库迁移、知识图谱同步、API、Worker、Scheduler 和 Consumer 启动。
- 总结记忆可以创建、合并、纠正、删除、恢复、检索和组装上下文。
- 图谱用户状态只显示“无状态、学习中、熟练、精通”。
- 用户不能手动设置“精通”，尝试时前后端均给出明确提示。
- 所有写操作幂等，不出现重复 commit。
- 多文档更新不出现部分活动版本。
- 固定知识图谱节点和边不能被业务 API 修改。
- 任务最终进入 `succeeded`、`needs_review`、`dead_letter` 或 `cancelled`。
- API 不暴露 LangGraph 内部线程、Checkpoint 和节点状态。
- 本地检查、测试、前端构建和本地联调全部通过；实施阶段按第 23.7 节初始化本地 Git 且不配置 remote；本期不要求 GitHub Actions。

---

## 2. 已裁决产品与架构规则

### 2.1 本地优先、云端后置的部署和认证

- 当前施工阶段只做本地开发和本地验收，不部署云服务器，不依赖域名，也不进行真实网站认证联调。
- 本地运行形态为：前端 Vite + 本地 Memory API + Docker Compose PostgreSQL/Worker/Scheduler/Outbox Consumer。
- 只有本地后端、前端、API 契约、失败恢复和端到端验收全部通过后，才进入云端部署阶段。
- 未来生产形态仍为单台云服务器 + Docker Compose；云端配置必须与本地 Compose 服务边界保持一致。
- API、Worker、Scheduler、Outbox Consumer 和 PostgreSQL 分进程/容器部署。
- Markdown 位于只授予 Memory 服务写权限的持久化卷。
- Memory API 不实现登录；生产身份来自网站统一认证。
- 浏览器不得持有内部服务凭证，也不得直接提交可信 `user_id`。
- 本地开发启用仅限 development 环境的测试身份适配器。
- 生产 `AUTH_ISSUER`、`AUTH_AUDIENCE`、`AUTH_JWKS_URL` 或 `AUTH_PUBLIC_KEY` 通过部署配置注入；缺失时生产 readiness 失败，但本地开发不受影响。
- 域名只在云端公开访问、HTTPS、Cookie 和正式认证阶段配置，不是本地前端开发前置条件。

### 2.2 Markdown 管理

- 生产环境禁止人工直接修改 Markdown。
- `MemoryService` 是唯一写入口。
- PostgreSQL 的活动版本指针是读取依据。
- Markdown 历史版本不可变；活动路径是便于查看和备份的物化副本。
- `index.md` 是可重建派生文档，不阻塞核心记忆提交。

### 2.3 删除与恢复

- 单条记忆删除后立即对用户和 Agent 不可见。
- 删除内容进入隔离区 30 天；tombstone 本身不保存正文。
- 30 天内允许用户主动恢复。
- tombstone 期间，旧证据不得重新创建同一记忆；明确的新证据可以触发恢复/新版本。
- 30 天后物理清理隔离正文和历史内容，只保留不可还原正文的最小审计字段。
- 账号注销事件到达后 24 小时内物理删除 Markdown、总结记忆、图谱 Overlay、Checkpoint 和未投递用户 Outbox。
- 备份中的账号数据随最长 30 天备份周期自然淘汰。

### 2.4 管理员和审核

- 管理员默认不能读取用户记忆正文、对话证据和模型输出。
- 故障或申诉使用限用户、限时间、限原因并完整审计的 `break-glass` 权限。
- 低置信候选不写入活动 Markdown，而是进入 `needs_review` 候选区。
- 用户可以确认、纠正或拒绝候选；拒绝后创建候选 tombstone，避免旧证据重复生成。

### 2.5 知识图谱状态

对外状态只允许四种显示语义：

```text
null / 无状态
learning / 学习中
proficient / 熟练
expert / 精通
```

内部可以保留 evidence、reason code、source 和版本，但 UI 只显示上述四种状态，不显示百分比掌握度。

规则：

- “无状态”表示没有足够信息，数据库中删除活动 Overlay 行。
- 用户点击“不熟悉”立即设置为 `learning`。
- 用户点击“熟悉”立即设置为 `proficient`。
- 用户清除状态后删除活动 Overlay 行，并写审计。
- 用户不能手动设置 `expert`；前端提示，后端返回 `GRAPH_STATUS_NOT_USER_SETTABLE`。
- `expert` 只能由长期、多次、高质量证据推导。
- 用户点击立即生效，但不永久锁定；后续足够强的新证据可以调整状态，并必须保留依据和通知用户。
- 总结记忆可以派生图谱状态更新，但二者独立提交、独立重试，不强制关联。

### 2.6 OpenAI 调用取向

- 成本和延迟优先。
- 默认模型：`gpt-5.6-luna`，通过 `OPENAI_MEMORY_MODEL` 覆盖。
- 使用 OpenAI Python SDK Responses API 与 Structured Outputs。
- 普通 operation 最多两次模型调用：候选提取一次、MutationPlanDraft 生成一次。
- 不使用流式输出；不为低置信结果自动升级昂贵模型。
- 模型无文件、数据库和任意工具写权限。
- CI 不依赖真实 OpenAI；所有 Graph 测试使用 Fake Client。真实模型验收为手动步骤。

---

## 3. 技术栈、版本和工程基线

### 3.1 Python 和后端依赖

`pyproject.toml` 使用兼容范围，`uv.lock` 锁定精确解析版本。第一版基线如下：

| 组件 | 第一版约束 |
|---|---|
| Python | `>=3.13,<3.14`，当前开发基线 3.13.12 |
| 包管理 | `uv` + `pyproject.toml` + `uv.lock` |
| FastAPI | `>=0.116,<1.0` |
| Uvicorn | `>=0.47,<0.48` |
| Pydantic | `>=2.13,<3.0` |
| SQLAlchemy | `>=2.0.49,<2.1` |
| Alembic | `>=1.16,<2.0` |
| PostgreSQL 驱动 | `psycopg[binary,pool]>=3.2,<4.0` |
| LangGraph | `==1.2.1` |
| LangGraph Checkpoint | `==4.1.0` |
| LangGraph PostgreSQL Checkpoint | `==3.0.4` |
| OpenAI Python SDK | `>=2.38,<3`，由 `uv.lock` 锁定实际验收版本 |
| Prometheus client | `prometheus-client>=0.22,<1` |
| pytest | `>=9.0,<10.0` |
| pytest-asyncio | `>=1.1,<2.0` |
| Ruff | `>=0.12,<1.0` |
| mypy | `>=1.17,<2.0` |

选择 `psycopg` 同时服务 SQLAlchemy async 与 LangGraph PostgreSQL Checkpointer，第一版不再额外引入 `asyncpg`。

`pypdf` 不进入 Memory 核心运行依赖，放入 OCR/test 可选 extra：

```toml
[project.optional-dependencies]
ocr = ["pypdf>=6,<7"]
```

本地完整 CI 使用 `uv sync --extra dev --extra ocr`，确保新工程不会破坏现有 OCR 测试。

### 3.2 前端工程基线

现有前端使用 React 19、Vite、TypeScript。第一版允许补充以下开发依赖：

- Vitest
- Testing Library
- MSW
- ESLint
- jsdom
- React Hooks ESLint plugin
- React Refresh ESLint plugin

同时增加运行依赖 `@dagrejs/dagre`，用于 KnowledgeMap 的确定性层次布局。

`frontend/package.json` 必须新增：

```json
{
  "scripts": {
    "dev": "vite",
    "build": "tsc --noEmit && vite build",
    "preview": "vite preview",
    "lint": "eslint .",
    "test": "vitest run"
  }
}
```

### 3.3 基础设施

| 组件 | 第一版选择 |
|---|---|
| PostgreSQL | 17，Docker 镜像固定到 major：`postgres:17` |
| 扩展 | `pg_trgm`、`pgcrypto` |
| Markdown | 单机持久化卷 |
| 本地环境 | Docker Compose |
| 版本控制 | 项目根目录本地 Git，不配置 remote |
| CI | 本地 `scripts/ci-local.sh` 和等价命令，不创建 GitHub Actions workflow |
| 日志 | JSON stdout/stderr |
| 时区 | 数据库存 UTC；产品日程使用 `Asia/Shanghai` |

### 3.4 推荐模块布局

```text
backend/
├── __init__.py
├── app.py
├── settings.py
├── auth/
│   ├── context.py
│   └── verifier.py
└── memory/
    ├── __init__.py
    ├── api/
    │   ├── dependencies.py
    │   ├── operations.py
    │   ├── memories.py
    │   ├── graph_states.py
    │   ├── notifications.py
    │   ├── internal.py
    │   └── reviews.py
    ├── contracts/
    │   ├── common.py
    │   ├── operations.py
    │   ├── evidence.py
    │   ├── commands.py
    │   ├── results.py
    │   ├── context.py
    │   └── graph_state.py
    ├── graph/
    │   ├── manager.py
    │   ├── state.py
    │   ├── summary.py
    │   ├── graph_state.py
    │   ├── maintenance.py
    │   └── policies.py
    ├── prompts/
    │   ├── extract_candidates_v1.md
    │   └── build_mutation_plan_v1.md
    ├── readers/
    │   ├── base.py
    │   ├── conversation.py
    │   ├── activity.py
    │   └── testing.py
    ├── services/
    │   ├── memory_service.py
    │   ├── graph_state_service.py
    │   ├── context_service.py
    │   ├── review_service.py
    │   ├── notification_service.py
    │   ├── purge_service.py
    │   └── projection_service.py
    ├── persistence/
    │   ├── database.py
    │   ├── operations.py
    │   ├── documents.py
    │   ├── commits.py
    │   ├── graph_states.py
    │   ├── notifications.py
    │   ├── outbox.py
    │   └── checkpoints.py
    ├── storage/
    │   ├── base.py
    │   └── local_markdown.py
    ├── knowledge_graph/
    │   ├── parser.py
    │   ├── registry.py
    │   └── resolver.py
    ├── worker/
    │   ├── main.py
    │   ├── scheduler.py
    │   ├── outbox_consumer.py
    │   └── retry.py
    ├── runner.py
    ├── client.py
    └── cli.py
alembic/
tests/
├── unit/
├── graph/
├── integration/
├── contract/
└── failure_recovery/
```

---

## 4. 标识符、主题和时间规则

### 4.1 ID 规则

| ID | 格式与生成规则 | 稳定性 |
|---|---|---|
| `operation_id` | UUID4，由 Gateway 生成 | 全局唯一、永不复用 |
| `mutation_id` | UUID4，由应用代码在确定性校验通过、准备提交时生成 | 全局唯一、永不复用 |
| `commit_id` | UUID4，由 MemoryService 生成 | 全局唯一、永不复用 |
| `candidate_id` | UUID4，由 CandidateStore/MemoryService 持久化候选时生成 | 全局唯一、永不复用 |
| `trace_id` | 继承 W3C Trace Context；不存在时生成 32 位十六进制 ID | 一条调用链稳定 |
| `graph_thread_id` | `memory-op:{operation_id}` | 仅内部使用 |
| `memory_id` | `learner`、`index`、`mastery:{topic_key}` | 用户生命周期内稳定 |

`operation_id`、`mutation_id`、`commit_id` 和 `candidate_id` 在数据库中使用 `uuid` 类型；API JSON 使用标准 UUID 字符串，不添加自定义前缀。模型输出不得生成其中任何稳定 ID。`mutation_id` 只为实际提交的 mutation 生成；`no_change` 不生成 mutation，也不创建 commit。

全局 maintenance 使用固定系统 UUID：

```text
00000000-0000-0000-0000-000000000000
```

该 UUID 禁止作为真实用户 ID 使用。

`expected_version` 不是模型决策字段：用户命令可携带它作为并发令牌；总结 Graph 在读取目标后由应用代码把当前版本注入内部计划。模型只提出“操作意图”和受限 patch。

### 4.2 `memory_id`

- `learner.md` 固定为 `learner`。
- `index.md` 固定为 `index`，但属于派生文档。
- 掌握档案固定为 `mastery:{topic_key}`。
- `memory_id` 可以通过 API 对用户显示，用于纠正、删除和恢复。
- 删除后逻辑 ID 不分配给其他主题。
- 同一主题恢复或重新形成记忆时沿用同一 `memory_id`，版本号继续递增。

### 4.3 `topic_key`

规范化算法：

1. 对主题名做 Unicode NFKC 规范化。
2. 去除首尾空白；连续空白和标点转换成一个 `-`。
3. 拉丁字母统一小写；保留 Unicode 字母和数字。
4. 只允许 Unicode 字母、数字和 ASCII `-`。
5. 禁止 `/`、`\\`、`.` 路径段、控制字符和隐藏字符。
6. 去除连续和首尾 `-`。
7. 最长 80 个 Unicode code point，最短 1 个。
8. 若不同规范主题产生同一 key，追加 `-` 和规范主题 SHA-256 的前 8 位。

模型只能输出 `topic_title`；最终 `topic_key` 必须由代码生成。

### 4.4 时间

- API 时间均为带时区 RFC 3339。
- 数据库使用 `timestamptz`，统一存储 UTC。
- `occurred_at` 表示业务发生时间；`created_at` 表示系统接收时间。
- 未提供业务时间的用户命令，由 Gateway 使用当前时间。
- 允许业务时间最多比当前时间晚 5 分钟；超过则拒绝。

### 4.5 幂等 payload hash

所有写请求在创建 operation 前计算 `idempotency_payload_hash`：

- 使用 Pydantic 校验后的公开请求 payload。
- 序列化为 RFC 8785 JSON Canonicalization Scheme。
- 计算 SHA-256，小写十六进制 64 位。
- 同一 `(user_id, actor_type, idempotency_key)` 但 hash 不同，返回 `IDEMPOTENCY_KEY_REUSED_WITH_DIFFERENT_PAYLOAD`。

`Idempotency-Key` 允许 ASCII 可见字符，长度 1～200，不允许控制字符。

---

## 5. `MemoryOperation` 契约

### 5.1 枚举

```python
ActorType = Literal[
    "user",
    "conversation_agent",
    "activity_agent",
    "knowledge_graph_ui",
    "summary_projection",
    "system",
    "admin",
]

InputKind = Literal["evidence", "command", "projection", "maintenance"]

OperationType = Literal[
    "conversation_evidence",
    "activity_evidence",
    "correct_memory",
    "forget_memory",
    "restore_memory",
    "override_learner_profile",
    "review_candidate",
    "set_graph_state",
    "project_summary_to_graph",
    "rebuild_index",
    "verify_checksums",
    "purge_tombstones",
    "cleanup_orphan_versions",
    "cleanup_checkpoints",
    "purge_account_memory",
]

OperationStatus = Literal[
    "queued",
    "running",
    "retry_wait",
    "succeeded",
    "needs_review",
    "dead_letter",
    "cancelled",
]
```

### 5.2 优先级

```text
P0 = 100：纠正、删除、恢复、候选审核
P1 = 80：用户知识图谱标记
P2 = 50：对话总结、总结到图谱的派生更新
P3 = 20：用户动态证据
P4 = 0：维护任务
```

Worker 使用：

```sql
ORDER BY priority DESC, created_at ASC
```

### 5.3 Pydantic Schema

```python
from datetime import datetime
from typing import Annotated, Literal, Protocol
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator


class MemoryOperation(BaseModel):
    """MemoryManagerGraph 的稳定输入信封。"""

    model_config = ConfigDict(extra="forbid")

    operation_id: UUID
    idempotency_key: str = Field(min_length=1, max_length=200)
    user_id: UUID
    actor_type: ActorType
    input_kind: InputKind
    operation_type: OperationType
    priority: int = Field(ge=0, le=100)
    occurred_at: datetime
    payload: "MemoryPayload"
    trace_id: str = Field(min_length=32, max_length=64)
    graph_thread_id: str
    schema_version: Literal[1] = 1
```

规则：

- 公共 API 请求不接受 `operation_id`、`user_id`、`priority` 和 `graph_thread_id`。
- Gateway 从认证上下文注入 `user_id` 和 `actor_type`，根据操作类型设置优先级。
- `Idempotency-Key` 是所有写请求的必填 HTTP Header。
- 唯一约束为 `(user_id, actor_type, idempotency_key)`。
- 重复请求返回原 operation，不创建新任务。
- `payload` 使用 `payload.kind` 判别联合；未知字段一律拒绝。
- `operation_type`、`input_kind` 和优先级由代码从 `payload.kind` 推导。

---

## 6. Payload 完整定义

### 6.1 对话和行为 Reader 引用

```python
class ConversationEvidence(BaseModel):
    model_config = ConfigDict(extra="forbid")

    kind: Literal["conversation_evidence"] = "conversation_evidence"
    thread_id: str = Field(min_length=1, max_length=200)
    checkpoint_id: str | None = Field(default=None, max_length=200)
    message_ids: list[str] = Field(min_length=1, max_length=200)
    trigger: Literal[
        "explicit_remember",
        "turn_boundary",
        "topic_switch",
        "exercise_completed",
        "conversation_end",
    ]
    topic_hints: list[str] = Field(default_factory=list, max_length=20)
    graph_node_hints: list[str] = Field(default_factory=list, max_length=20)


class ActivityEvidence(BaseModel):
    model_config = ConfigDict(extra="forbid")

    kind: Literal["activity_evidence"] = "activity_evidence"
    activity_type: Literal[
        "forum_post",
        "forum_reply",
        "wrong_question_upload",
        "exercise_attempt",
        "review_result",
        "page_view",
        "bookmark",
        "check_in",
    ]
    activity_ids: list[str] = Field(min_length=1, max_length=200)
    content_ref: str | None = Field(default=None, max_length=500)
    aggregated_count: int = Field(default=1, ge=1, le=10_000)
    window_started_at: datetime | None = None
    window_ended_at: datetime | None = None
    topic_hints: list[str] = Field(default_factory=list, max_length=20)
    graph_node_hints: list[str] = Field(default_factory=list, max_length=20)
```

Reader 契约：

```python
class ConversationReader(Protocol):
    async def read(
        self,
        *,
        user_id: UUID,
        thread_id: str,
        checkpoint_id: str | None,
        message_ids: list[str],
    ) -> SourceBundle: ...


class ActivityReader(Protocol):
    async def read(
        self,
        *,
        user_id: UUID,
        activity_type: str,
        activity_ids: list[str],
        content_ref: str | None,
    ) -> SourceBundle: ...
```

本期只提供可注入测试 Reader；Memory 模块不得读取外部系统的内部数据库表。

### 6.2 用户记忆命令

```python
class LearnerReplacement(BaseModel):
    model_config = ConfigDict(extra="forbid")

    replacement_type: Literal["learner"] = "learner"
    preferences: list[str] = Field(default_factory=list, max_length=50)
    goals: list[str] = Field(default_factory=list, max_length=50)
    plans: list[str] = Field(default_factory=list, max_length=50)


class MasteryReplacement(BaseModel):
    model_config = ConfigDict(extra="forbid")

    replacement_type: Literal["mastery"] = "mastery"
    topic_title: str = Field(min_length=1, max_length=120)
    overview: str = Field(default="", max_length=1200)
    understood: list[str] = Field(default_factory=list, max_length=50)
    difficulties: list[str] = Field(default_factory=list, max_length=50)
    review_advice: list[str] = Field(default_factory=list, max_length=30)
    evidence_refs: list[str] = Field(default_factory=list, max_length=100)


MemoryReplacement = Annotated[
    LearnerReplacement | MasteryReplacement,
    Field(discriminator="replacement_type"),
]


class CorrectMemoryCommand(BaseModel):
    model_config = ConfigDict(extra="forbid")

    kind: Literal["correct_memory"] = "correct_memory"
    memory_id: str = Field(min_length=1, max_length=160)
    expected_version: int = Field(ge=1)
    replacement: MemoryReplacement
    reason: str | None = Field(default=None, max_length=500)


class ForgetMemoryCommand(BaseModel):
    model_config = ConfigDict(extra="forbid")

    kind: Literal["forget_memory"] = "forget_memory"
    memory_id: str = Field(min_length=1, max_length=160)
    expected_version: int = Field(ge=1)
    reason: str | None = Field(default=None, max_length=500)


class RestoreMemoryCommand(BaseModel):
    model_config = ConfigDict(extra="forbid")

    kind: Literal["restore_memory"] = "restore_memory"
    memory_id: str = Field(min_length=1, max_length=160)
    deleted_version: int = Field(ge=1)


class OverrideLearnerProfileCommand(BaseModel):
    model_config = ConfigDict(extra="forbid")

    kind: Literal["override_learner_profile"] = "override_learner_profile"
    expected_version: int | None = Field(default=None, ge=1)
    preferences: list[str] | None = Field(default=None, max_length=50)
    goals: list[str] | None = Field(default=None, max_length=50)
    plans: list[str] | None = Field(default=None, max_length=50)
    reason: str | None = Field(default=None, max_length=500)
```

服务端必须校验 replacement 类型与目标 `memory_id` 一致。任意 Markdown、JSON Patch、绝对路径、SQL 和文件删除命令都不是公开契约。

### 6.3 候选审核命令

```python
class CandidateContentView(BaseModel):
    model_config = ConfigDict(extra="forbid")

    memory_type: Literal["learner", "mastery"]
    topic_key: str | None = Field(default=None, max_length=160)
    topic_title: str | None = Field(default=None, max_length=240)
    overview: str | None = Field(default=None, max_length=1200)
    preferences: list[str] = Field(default_factory=list, max_length=50)
    goals: list[str] = Field(default_factory=list, max_length=50)
    plans: list[str] = Field(default_factory=list, max_length=50)
    understood: list[str] = Field(default_factory=list, max_length=50)
    difficulties: list[str] = Field(default_factory=list, max_length=50)
    review_advice: list[str] = Field(default_factory=list, max_length=30)


class ReviewCandidateCommand(BaseModel):
    model_config = ConfigDict(extra="forbid")

    kind: Literal["review_candidate"] = "review_candidate"
    candidate_id: UUID
    decision: Literal["accept", "correct", "reject"]
    resolution_target: Literal["merge_existing", "create_new_topic"] | None = None
    target_memory_id: str | None = Field(default=None, max_length=160)
    corrected_content: MemoryReplacement | None = None
    reason: str | None = Field(default=None, max_length=500)

    @model_validator(mode="after")
    def validate_review_fields(self):
        if self.decision == "correct" and self.corrected_content is None:
            raise ValueError("corrected_content is required for correct")
        if self.decision in {"accept", "reject"} and self.corrected_content is not None:
            raise ValueError("corrected_content is only allowed for correct")
        if self.resolution_target == "merge_existing" and not self.target_memory_id:
            raise ValueError("target_memory_id is required for merge_existing")
        if self.resolution_target == "create_new_topic" and self.target_memory_id is not None:
            raise ValueError("target_memory_id is forbidden for create_new_topic")
        if self.resolution_target is None and self.target_memory_id is not None:
            raise ValueError("target_memory_id requires resolution_target=merge_existing")
        return self
```

- `accept` 不直接复用旧 mutation，而是读取候选 `base_memory_id/base_version/topic_key` 和当前活动版本后生成新的 commit plan。
- `correct` 必须提供受限结构化内容；`accept/reject` 禁止 `corrected_content`。
- 只有 `topic_conflict` 候选允许 `resolution_target`；`merge_existing` 必须同时提供 `target_memory_id`，`create_new_topic` 必须禁止 `target_memory_id`。后端不得替用户猜测“合并还是新主题”。
- 已有 mastery 允许纠正显示用 `topic_title`，但不得改变稳定 `topic_key`、`memory_id` 或固定图谱节点 ID。
- `topic_conflict` 的 `accept/correct` 必须提供 `resolution_target`，明确“合并到已有 memory_id”或“创建新主题”；不能由后端猜测。
- 当前版本变化时按确定性策略重放 patch；无法安全重放则生成新的 `version_conflict` 候选，不提交旧计划。
- `reject` 创建 30 天候选 tombstone。

### 6.4 图谱命令和派生更新

```python
class GraphStateCommand(BaseModel):
    model_config = ConfigDict(extra="forbid")

    kind: Literal["set_graph_state"] = "set_graph_state"
    node_id: str = Field(pattern=r"^n\d{3,}$")
    action: Literal["mark_unfamiliar", "mark_familiar", "clear"]
    expected_version: int | None = Field(default=None, ge=1)


class GraphStatePutRequest(BaseModel):
    """图谱状态 PUT 的公开请求体；路径参数由 Gateway 注入内部命令。"""

    model_config = ConfigDict(extra="forbid")

    action: Literal["mark_unfamiliar", "mark_familiar"]
    expected_version: int | None = Field(default=None, ge=1)
```


公开接口与内部命令分离：

- `PUT /api/v1/knowledge-graph/me/nodes/{node_id}/state` 的 JSON **只接受** `action` 和 `expected_version`；`kind`、`node_id` 由服务端分别固定为 `set_graph_state` 和 URL 路径中的 `node_id` 后构造内部 `GraphStateCommand`。
- `GraphStatePutRequest` 使用 `extra="forbid"`。客户端额外传入 `kind` 或 `node_id`（即使 `node_id` 与路径一致）均返回 422 `REQUEST_EXTRA_FIELD`，避免同一字段出现两个权威来源；本期不提供兼容性放宽。
- `DELETE /api/v1/knowledge-graph/me/nodes/{node_id}/state?expected_version=<n>` 不接收 JSON body；Gateway 直接构造 `action="clear"` 的内部 `GraphStateCommand`。
- `GraphStateCommand` 是 operation/Graph 内部 payload，不能直接作为 PUT 的公开请求 schema。

```python

class GraphProjectionEvidence(BaseModel):
    evidence_ref: str
    direction: Literal["learning", "positive", "strong_positive", "conflict"]
    strength: float = Field(ge=0, le=1)
    occurred_at: datetime


class ProjectSummaryToGraphCommand(BaseModel):
    model_config = ConfigDict(extra="forbid")

    kind: Literal["project_summary_to_graph"] = "project_summary_to_graph"
    trigger_event_type: Literal[
        "memory.changed",
        "memory.deleted",
        "memory.restored",
    ]
    projection_action: Literal[
        "apply_active_version",
        "recompute_without_deleted_version",
    ]
    source_memory_id: str
    source_version: int = Field(ge=1)
    node_id: str = Field(pattern=r"^n\d{3,}$")
    mapping_method: Literal["explicit_hint", "exact_alias", "model_candidate"] | None = None
    mapping_confidence: float | None = Field(default=None, ge=0, le=1)
    evidence: list[GraphProjectionEvidence] = Field(default_factory=list, max_length=50)
```

`ProjectSummaryToGraphCommand` 只能由 `summary_projection` actor 创建，外部 API 不接受该 payload。应用层必须做跨字段校验：

- `memory.changed/memory.restored` 必须使用 `apply_active_version`，且 mapping、confidence 和至少一条 evidence 必填；`source_version` 必须仍是活动版本。
- `memory.deleted` 必须使用 `recompute_without_deleted_version`；`source_version` 等于 tombstone 的 `deleted_version`，不得按活动版本读取。该模式忽略被删除版本的 evidence，并从该节点仍有效的其他活动总结记忆重新聚合。

### 6.5 Maintenance Payload

```python
class MaintenanceCommand(BaseModel):
    model_config = ConfigDict(extra="forbid")

    kind: Literal[
        "rebuild_index",
        "verify_checksums",
        "purge_tombstones",
        "cleanup_orphan_versions",
        "cleanup_checkpoints",
        "purge_account_memory",
    ]
    target_user_id: UUID | None = None
    dry_run: bool = False
    cursor: str | None = None
    batch_size: int = Field(default=100, ge=1, le=1000)
```

### 6.6 判别联合

```python
MemoryPayload = Annotated[
    ConversationEvidence
    | ActivityEvidence
    | CorrectMemoryCommand
    | ForgetMemoryCommand
    | RestoreMemoryCommand
    | OverrideLearnerProfileCommand
    | ReviewCandidateCommand
    | GraphStateCommand
    | ProjectSummaryToGraphCommand
    | MaintenanceCommand,
    Field(discriminator="kind"),
]
```

`operation_type` 必须与内部 operation `payload.kind` 一一对应；Gateway 接收公开 HTTP 请求时先使用各路由的公开 request schema（例如 `GraphStatePutRequest`），再由路径参数和认证上下文构造内部 `payload`。因此，公开 PUT 不要求客户端发送 `kind`，但内部 operation 始终必须有 `payload.kind`；未知字段一律拒绝。

---

## 7. 返回契约和错误模型

### 7.1 Operation 结果

```python
class MutationResult(BaseModel):
    mutation_id: UUID
    memory_id: str
    action: Literal[
        "create",
        "merge",
        "replace",
        "append_evidence",
        "forget",
        "restore",
    ]
    before_version: int | None
    after_version: int | None


class GraphStateChangeView(BaseModel):
    node_id: str = Field(pattern=r"^n\d{3,}$")
    before_status: Literal["learning", "proficient", "expert"] | None
    after_status: Literal["learning", "proficient", "expert"] | None
    before_version: int | None
    after_version: int | None
    source_type: Literal["user", "summary_memory", "system_recompute"]
    reason_codes: list[str] = Field(default_factory=list, max_length=20)
    changed_at: datetime


class MemoryOperationResult(BaseModel):
    operation_id: UUID
    status: OperationStatus
    operation_type: OperationType
    created_at: datetime
    updated_at: datetime
    completed_at: datetime | None = None
    cancelled_at: datetime | None = None
    mutations: list[MutationResult] = Field(default_factory=list)
    review_candidate_ids: list[UUID] = Field(default_factory=list)
    graph_state_changes: list[GraphStateChangeView] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    error: "PublicError | None" = None
```

规则：

- 活动文档提交不允许部分成功。
- `mutations` 只返回目标、动作和版本，不返回模型 reasoning、原始 Prompt 或内部存储路径。
- 低置信候选产生 `succeeded + review_candidate_ids`；只有阻塞性冲突才使 operation 进入 `needs_review`。
- `no_change` 是 Graph/策略层结果，不创建 `mutation_id`、`memory_commits` 或引用不存在目标文档的 commit。
- 重复幂等请求返回同一 `operation_id` 的当前结果。

### 7.2 HTTP 返回

| 场景 | HTTP |
|---|---:|
| 新异步任务已入队 | 202 |
| P0/P1 快速执行在等待窗口内完成 | 200 |
| 幂等重复且原任务未完成 | 202 |
| 幂等重复且原任务已完成 | 200 |
| 参数/状态转换错误 | 422 |
| 未认证 | 401 |
| 无权限 | 403 |
| 目标不存在 | 404 |
| expected_version 冲突 | 409 |
| 限流 | 429 |
| 系统不可用 | 503 |

### 7.3 错误结构

```python
class PublicError(BaseModel):
    code: str
    message: str
    retryable: bool
    field: str | None = None
    trace_id: str
```

第一版错误码：

```text
AUTH_REQUIRED
AUTH_FORBIDDEN
INVALID_PAYLOAD
INVALID_IDEMPOTENCY_KEY
IDEMPOTENCY_KEY_REUSED_WITH_DIFFERENT_PAYLOAD
MEMORY_NOT_FOUND
MEMORY_VERSION_CONFLICT
MEMORY_DELETED
MEMORY_RESTORE_EXPIRED
CANDIDATE_NOT_FOUND
CANDIDATE_ALREADY_REVIEWED
GRAPH_NODE_NOT_FOUND
GRAPH_STATUS_NOT_USER_SETTABLE
GRAPH_STATE_VERSION_CONFLICT
GRAPH_STATE_VERSION_REQUIRED
OPERATION_CANCEL_NOT_ALLOWED
IDENTITY_MAPPING_NOT_FOUND
ACCOUNT_PURGE_ALREADY_RUNNING
SOURCE_TOO_LARGE
CURSOR_INVALID
CURSOR_EXPIRED
SOURCE_NOT_FOUND
SOURCE_ACCESS_DENIED
SOURCE_DELETED
OPENAI_TIMEOUT
OPENAI_RATE_LIMITED
OPENAI_SCHEMA_INVALID
STORAGE_UNAVAILABLE
DATABASE_UNAVAILABLE
OPERATION_NEEDS_REVIEW
OPERATION_DEAD_LETTER
RATE_LIMITED
INTERNAL_ERROR
```

---

## 8. Markdown Schema、路径和版本协议

### 8.1 逻辑文档

```text
index
learner
mastery:{topic_key}
```

Front matter 必须包含：

```yaml
kind: mastery-profile
schema_version: 1
user_id: <uuid>
memory_id: mastery:一致收敛
topic_key: 一致收敛
topic_title: 一致收敛
version: 4
updated_at: 2026-08-10T08:00:00Z
evidence_count: 3
confidence: 0.86
```

`learner.md` 使用 `kind: learner-profile`，不包含 `topic_key/topic_title`；`index.md` 使用 `kind: memory-index`。

### 8.2 正文模板

#### `learner.md`

```markdown
---
kind: learner-profile
schema_version: 1
user_id: <uuid>
memory_id: learner
version: <n>
updated_at: <rfc3339>
evidence_count: <n>
confidence: <0..1|null>
---

# 学习者档案

## 学习偏好

## 学习目标

## 当前计划

## 证据引用
```

#### `mastery/{topic_key}.md`

```markdown
---
kind: mastery-profile
schema_version: 1
user_id: <uuid>
memory_id: mastery:<topic_key>
topic_key: <topic_key>
topic_title: <topic_title>
version: <n>
updated_at: <rfc3339>
evidence_count: <n>
confidence: <0..1|null>
---

# <topic_title>

## 当前掌握概况

## 已掌握

## 仍有困难

## 建议复习

## 证据引用
```

#### `index.md`

```markdown
---
kind: memory-index
schema_version: 1
user_id: <uuid>
memory_id: index
version: <n>
updated_at: <rfc3339>
---

# 长期记忆目录

## 学习者档案

## 掌握档案

## 主题路由
```

空章节保留标题，不写入空列表项。`learner.md` 和每个 `mastery/*.md` 的活动版本最多物化 100 条去重后的 evidence refs，超出的旧引用只保留在 commit metadata 中，不保留原始证据正文。`index.md` 只索引当前未删除的活动版本。`confidence` 只写 front matter，不作为正文掌握百分比展示。Markdown 渲染必须可解析 round-trip；解析失败的活动版本触发 checksum/一致性维护告警。

### 8.3 存储布局

默认根目录：`MEMORY_STORAGE_ROOT=/data/memory`。

```text
/data/memory/
└── users/
    └── {user_id前2位}/
        └── {user_id}/
            ├── current/
            │   ├── index.md
            │   ├── learner.md
            │   └── mastery/
            │       └── {topic_key}.md
            ├── versions/
            │   ├── index/
            │   │   └── v00000001-{checksum12}.md
            │   ├── learner/
            │   │   └── v00000007-{checksum12}.md
            │   └── mastery/
            │       └── {topic_key}/
            │           └── v00000004-{checksum12}.md
            └── quarantine/
                └── {memory_id_hash}/
                    └── delete-{deleted_at_epoch}/
```

- `versions/` 文件不可变。
- `current/` 是活动版本的物化副本，不是读取事实源。
- `quarantine/` 仅用于 30 天恢复窗口，对普通查询和 Agent 不可见。
- 文件路径只能由 `MemoryService` 生成。

### 8.4 生产对象存储 Key 预留

第一版不实现对象存储，但 Store 抽象使用以下稳定 Key 规则：

```text
users/{shard}/{user_id}/versions/{memory_type}/{topic_key-or-fixed}/v{version:08d}-{checksum12}.md
```

### 8.5 Checksum 和压缩

- Checksum：SHA-256，小写十六进制 64 位。
- 文件名使用 checksum 前 12 位，数据库保存完整值。
- 第一版不压缩 Markdown 历史版本。
- 孤立版本在创建 24 小时后清理。
- 未删除记忆的历史版本保留到账号删除。

### 8.6 多文档原子提交

- 一个 operation 可以包含最多 8 个 `CommitMutationPlan`。
- 一个 `CommitMutationPlan` 只能修改一个逻辑文档。
- 所有新版本先写入不可变 `versions/`，此时不会成为活动版本。
- 数据库事务取得用户级写锁，并按 `memory_id` 字典序锁定目标文档。
- 同一事务中校验全部 `expected_version`、插入 commit、更新活动版本、更新检索索引并写 Outbox。
- 任一校验失败则整个数据库事务回滚；已写文件成为孤立版本，24 小时后清理。
- 数据库提交后再原子物化 `current/`；物化失败不影响活动版本，维护任务根据数据库指针修复。
- 核心读取根据数据库 `active_version` 读取 `versions/`，不读取可能暂时滞后的 `current/`。
- `index.md` 永远异步派生，不参与业务文档原子提交。learner/mastery commit 在同一数据库事务中把用户的 index 文档 `index_dirty_at` 设为最早待重建时间；不再额外发送重复的 `index.rebuild_requested` Outbox。重建成功后仅在没有更新 commit 发生的前提下清除 `index_dirty_at`。重建审计与 `IndexRebuildPlan` 规则见第 8.6.1 节。

### 8.6.1 `index.md` 生命周期


- 首次 learner/mastery commit 自动 upsert 用户的 index 文档，仅设置 `index_dirty_at`；不调用 OpenAI。
- Scheduler 每 5 分钟将 dirty index 创建/复用 `rebuild_index` maintenance operation；重建使用独立确定性 `IndexRebuildPlan`，不加入模型生成的 `CommitMutationPlan`。
- 为了审计，重建仍写 `memory_commits.action='rebuild_index'`，但不产生 `memory.changed`/`learner.updated` 业务事件，也不进入用户可见 mutations。
- 重建只索引当前 active learner/mastery 版本；commit 并发发生时旧重建结果不得清除新的 dirty 标记。
- 未构建过的 index 返回 `version=0, entries=[], updated_at=null, stale=true`；dirty 时返回旧版本并 `stale=true`，不返回 404。

```python
class IndexRebuildPlan(BaseModel):
    user_id: UUID
    expected_dirty_at: datetime
    source_versions: dict[str, int]
    action: Literal["rebuild_index"] = "rebuild_index"
```

### 8.7 删除和恢复版本协议

删除单条记忆：

1. 校验 `expected_version` 等于当前活动版本。
2. 写入 `forget` commit：`before_version=当前版本`，`after_version=NULL`。
3. 将删除前版本号写入 `memory_documents.deleted_version`，并把 `active_version/active_storage_key/active_checksum` 全部置为 `NULL`；同时设置 `deleted_at` 和 `tombstone_until`。tombstone 只保存版本指针和审计元数据，不把已删除正文继续视为活动版本。
4. 普通读取、搜索和上下文组装排除该文档。
5. 删除检索索引活动条目。
6. 可恢复正文仍以不可变版本保存，并在数据库提交后由维护流程移动或标记到 `quarantine/`；即使物化移动失败，也可按 `memory_id + deleted_version` 从不可变版本区恢复。
7. 写入 `memory.deleted` Outbox，事件的 `aggregate_version` 使用 `deleted_version`。

恢复单条记忆：

1. 校验文档处于删除状态且未到 `tombstone_until`。
2. 命令和内部计划中的 `deleted_version` 必须等于 tombstone 版本；此时 `active_version` 必须为 `NULL`。
3. 读取隔离区或不可变版本区中的该版本正文并校验 checksum。
4. 新版本号为该文档历史最大版本号加 1；首次恢复示例为 `deleted_version + 1`，重复删除/恢复仍保持单调递增。
5. 写入 `restore` commit：`before_version=NULL`、`after_version=新版本`；恢复不是把旧版本重新设为活动版本，而是创建内容等价的新版本。
6. 清除 `deleted_at/tombstone_until/deleted_version`，写入新的 `active_version/active_storage_key/active_checksum`。
7. 重建检索索引并写入 `memory.restored` Outbox，事件的 `aggregate_version` 使用恢复后的新版本。

防旧证据复活：

- 对已删除文档记录 `(user_id, memory_id, evidence_ref_hash)` 级抑制关系，只保存 HMAC/SHA-256 引用摘要，不保存原始证据正文。
- 旧 evidence ref 再次出现时不得自动创建同一记忆；该抑制不自动到期，保留到账号删除。
- 明确的新 evidence ref 可以创建新版本。

### 8.8 候选 tombstone

拒绝候选后记录匹配键：

```text
candidate_type + normalized_topic_or_category + normalized_summary_hash
```

30 天内相同匹配键不得重复生成候选；用户明确纠正或新证据强度显著提升时允许生成新候选。

---

## 9. OpenAI 调用规格

### 9.1 Client 和参数

使用 `AsyncOpenAI`，通过 Runtime Context 注入，不进入 Graph State。OpenAI SDK 的实际安装版本由 `uv.lock` 固定；第一版允许 `>=2.38,<3` 范围内兼容升级，不在架构文档中把补丁版本永久固定为 `2.38.0`。

```text
model               = env OPENAI_MEMORY_MODEL，默认 gpt-5.6-luna
reasoning.effort     = env OPENAI_REASONING_EFFORT，默认 none
temperature          = 不设置，使用模型/API默认行为
stream               = false
timeout              = 45 秒
max_output_tokens    = 3000（候选提取）/ 4000（MutationPlanDraft）
正常最大调用次数      = 2 次/operation attempt
任务生命周期调用上限  = 4 次
```

自动化测试、`scripts/ci-local.sh` 和契约测试全部使用 Fake OpenAI Client，不要求 `OPENAI_API_KEY`，真实联网调用不构成本地 CI 阻塞条件。实现必须提供手动 smoke test：

```bash
uv run python -m backend.memory.cli validate-openai
```

该命令使用当前 `OPENAI_MEMORY_MODEL`、Responses API、Structured Outputs 和 `OPENAI_REASONING_EFFORT=none` 发起最小请求，明确报告模型不存在、账号无权限、参数不支持或 Schema 不兼容。目标账号的真实可用性必须在部署前通过该命令验证；验证失败不允许进入生产部署，但不阻塞 Fake Client 自动测试。

超过生命周期上限后，不再调用模型：有可用候选则进入 `needs_review`，否则进入 `dead_letter` 并记录可重试性。

### 9.2 Structured Outputs Schema

#### 候选提取

```python
class ExtractedEvidence(BaseModel):
    evidence_ref: str
    evidence_type: Literal[
        "explicit_user_statement",
        "user_solution",
        "exercise_result",
        "repeated_error",
        "learning_activity",
        "preference_statement",
        "goal_statement",
        "plan_statement",
    ]
    summary: str = Field(max_length=500)
    strength: float = Field(ge=0, le=1)


class CandidateMemory(BaseModel):
    memory_type: Literal["learner", "mastery"]
    topic_title: str | None = Field(default=None, max_length=120)
    category: Literal[
        "preference",
        "goal",
        "plan",
        "understanding",
        "difficulty",
        "misconception",
        "review_advice",
    ]
    summary: str = Field(max_length=1000)
    long_term_value: Literal["save", "review", "ignore"]
    confidence: float = Field(ge=0, le=1)
    evidence: list[ExtractedEvidence] = Field(min_length=1, max_length=20)
    graph_node_candidates: list[str] = Field(default_factory=list, max_length=5)


class CandidateExtractionResult(BaseModel):
    candidates: list[CandidateMemory] = Field(max_length=20)
    ignored_reason_codes: list[str] = Field(default_factory=list)
```

#### MutationPlanDraft

```python
class LearnerPatch(BaseModel):
    preferences_to_add: list[str] = Field(default_factory=list, max_length=20)
    preferences_to_remove: list[str] = Field(default_factory=list, max_length=20)
    goals_to_add: list[str] = Field(default_factory=list, max_length=20)
    goals_to_remove: list[str] = Field(default_factory=list, max_length=20)
    plans_to_add: list[str] = Field(default_factory=list, max_length=20)
    plans_to_remove: list[str] = Field(default_factory=list, max_length=20)


class MasteryPatch(BaseModel):
    overview: str | None = Field(default=None, max_length=1200)
    understood_to_add: list[str] = Field(default_factory=list, max_length=30)
    difficulties_to_add: list[str] = Field(default_factory=list, max_length=30)
    difficulties_to_resolve: list[str] = Field(default_factory=list, max_length=30)
    review_advice_to_add: list[str] = Field(default_factory=list, max_length=20)
    evidence_refs_to_add: list[str] = Field(default_factory=list, max_length=50)


class MutationPlanDraft(BaseModel):
    target_memory_type: Literal["learner", "mastery"]
    topic_title: str | None = Field(default=None, max_length=120)
    action: Literal["create", "merge", "replace", "append_evidence", "no_change"]
    learner_patch: LearnerPatch | None = None
    mastery_patch: MasteryPatch | None = None
    candidate_indexes: list[int] = Field(default_factory=list, max_length=20)
    reasoning_summary: str = Field(max_length=500)


class MutationPlanResult(BaseModel):
    plans: list[MutationPlanDraft] = Field(max_length=8)
```

模型输出的 `candidate_indexes` 只引用本次 `CandidateExtractionResult.candidates` 的数组位置，不是跨请求稳定标识。

应用代码转换后的确定计划：

```python
class CommitMutationPlan(BaseModel):
    model_config = ConfigDict(extra="forbid")

    mutation_id: UUID
    memory_id: str = Field(min_length=1, max_length=160)
    target_memory_type: Literal["learner", "mastery"]
    topic_title: str | None = Field(default=None, max_length=120)
    action: Literal[
        "create",
        "merge",
        "replace",
        "append_evidence",
        "forget",
        "restore",
    ]
    expected_version: int | None = Field(default=None, ge=1)
    deleted_version: int | None = Field(default=None, ge=1)
    learner_patch: LearnerPatch | None = None
    mastery_patch: MasteryPatch | None = None
    candidate_indexes: list[int] = Field(default_factory=list, max_length=20)
    replacement: MemoryReplacement | None = None
    reason: str | None = Field(default=None, max_length=500)
```

转换规则：

1. 重新校验目标主题、权限、策略和证据；
2. 读取目标文档当前状态并由代码注入并发令牌：`create` 的 `expected_version/deleted_version` 均为 `None`；`restore` 的 `expected_version=None` 且 `deleted_version` 必须等于 tombstone 版本；其余非 create 动作的 `expected_version` 必须等于当前活动版本，`deleted_version=None`；
3. 只为实际提交的计划生成 `mutation_id`；`no_change` 草稿不转换；
4. 仅为需要持久化审核的候选生成 `candidate_id`；
5. `target_memory_type`、patch 类型和 `memory_id` 必须一致；
6. 用户命令只转换为确定性计划，不经过模型重新解释。

模型不生成 `user_id`、最终 `topic_key`、绝对路径、SQL、稳定 ID、`expected_version`、删除命令或可执行工具调用。

### 9.3 确定性阈值

| 判断 | 规则 |
|---|---|
| 自动写入长期记忆 | `confidence >= 0.80` 且 `long_term_value=save` |
| 进入候选审核 | `0.55 <= confidence < 0.80` 或 `long_term_value=review` |
| 丢弃 | `confidence < 0.55` 或 `long_term_value=ignore` |
| 自动语义合并 | 模型建议 merge，且已有主题原始 trigram similarity `>=0.72` |
| 主题冲突 | 原始 trigram similarity `0.55–0.72` 或多个主题接近，进入 `needs_review` |
| 图谱模型候选映射 | 代码计算的 `mapping_confidence >=0.92`，且第一、第二候选差值 `>=0.15` |

掌握类事实附加限制：

- 单次页面浏览、收藏、打卡不能形成掌握结论。
- 单次普通计算错误不能独立形成稳定误解。
- “熟练”至少需要两条独立正向证据，或用户明确点击“熟悉”。
- “精通”至少需要三条高质量正向证据，来自至少两个事件/会话，并包含一次用户自主解答、推导、迁移应用或讲解证据；不得存在未解决的强冲突证据。
- 从“熟练/精通”降为“学习中”至少需要两条独立强冲突证据，或者一条清楚揭示核心概念误解的证据。

这些阈值必须放在配置/策略模块，写入指标，后续通过评测调整，不能散落在 Prompt 中。

同一记忆聚合多个已接受候选时，`memory_index_entries.confidence` 取最近 5 条已接受候选的证据强度加权平均：`weight_i = 候选 evidence strength 的平均值`，`confidence = Σ(candidate_confidence_i × weight_i) / Σ(weight_i)`；无有效权重时为 `NULL`。该值用于检索和质量指标，不得转换成用户掌握百分比。

### 9.4 Prompt 规则

- Prompt 由实现方依据本规格编写，需求方通过评测验收。
- Prompt 固定使用中文指令，Markdown 总结内容默认输出中文；数学符号和专有名词保留原文。
- 必须区分“教材/助手陈述”与“用户真实表现”，不得把助手解释保存成用户掌握事实。
- 必须过滤与数学学习无关的个人信息。
- 禁止保存密码、认证令牌、联系方式、精确住址、身份证件、财务信息和医疗信息。
- Prompt 文件独立版本管理，名称包含版本号；每次 LLM 调用记录 `prompt_version`。
- 日志不记录完整 Prompt、原始对话和完整模型输出。

---

## 10. Graph 实现规格

### 10.1 Graph State

```python
class MemoryManagerState(TypedDict, total=False):
    operation: dict
    route: str
    source_bundle: dict
    candidates: list[dict]
    candidate_graph_nodes: dict[str, list[dict]]
    existing_memories: list[dict]
    mutation_plan_drafts: list[dict]
    commit_mutation_plans: list[dict]
    commit_result: dict
    graph_state_result: dict
    review_candidates: list[dict]
    warnings: list[str]
    errors: list[dict]
    llm_call_count: int
    replan_count: int
```

Graph State 禁止保存 OpenAI Client、数据库连接、文件句柄、Reader/Service 实例、密钥、JWT 和大型原始对话全文。`source_bundle` 在进入 Checkpoint 前必须裁剪到最多 80 KB。

### 10.2 Runtime Context

```python
@dataclass(frozen=True)
class MemoryRuntimeContext:
    settings: MemorySettings
    memory_service: MemoryService
    graph_state_service: KnowledgeGraphStateService
    context_service: LearningContextService
    conversation_reader: ConversationReader
    activity_reader: ActivityReader
    graph_registry: KnowledgeGraphRegistry
    openai_client: AsyncOpenAI
    session_factory: async_sessionmaker[AsyncSession]
    clock: Clock
    id_generator: IdGenerator
    logger: BoundLogger
```

依赖通过 LangGraph runtime context/context schema 注入。Graph 节点不得自行创建全局 Client，也不得把数据库连接写入 State。

执行器接口：

```python
class MemoryGraphRunner(Protocol):
    async def run(
        self,
        operation: MemoryOperation,
    ) -> MemoryOperationResult:
        """执行已经成功领取的记忆操作并返回结构化结果。"""
        ...


class LocalLangGraphRunner(MemoryGraphRunner):
    ...
```

`LocalLangGraphRunner` 是第一版唯一实现。Runner 只接收 Gateway/Worker 已经通过公共 `claim_operation` 函数领取的 operation；Lease、heartbeat、soft/hard timeout 和 HTTP 断开后的继续执行由执行层负责，不由 Graph 节点自行管理。

### 10.3 父图节点

| 节点 | 输入 | 输出 | DB | OpenAI | 副作用 |
|---|---|---|---:|---:|---:|
| `normalize_input` | operation | 标准化 operation | 否 | 否 | 否 |
| `authorize_actor` | operation | 权限结论 | 可读 | 否 | 否 |
| `idempotency_guard` | operation | 已有结果或继续 | 只读 | 否 | 否 |
| `validate_invariants` | operation | 验证结果 | 只读 | 否 | 否 |
| `route_operation` | operation | route | 否 | 否 | 否 |
| `run_summary` | state | summary 结果 | 见子图 | 见子图 | 见子图 |
| `run_activity_exposure` | state | activity exposure 结果 | 是 | 否 | 更新 activity |
| `run_memory_command` | state | command 结果 | 是 | 否 | 是 |
| `run_graph_state` | state | graph 结果 | 是 | 否 | 是 |
| `run_projection` | state | projection 结果 | 是 | 否 | 是 |
| `run_maintenance` | state | maintenance 结果 | 是 | 否 | 是 |
| `normalize_result` | 各分支结果 | 稳定公开结果 | 否 | 否 | 否 |

### 10.3.1 Activity exposure 分支

`route_operation` 对 `ActivityEvidence` 做确定性分流：

```text
activity_evidence
├── page_view/bookmark/check_in → run_activity_exposure
└── forum_post/forum_reply/wrong_question_upload/exercise_attempt/review_result → run_summary
```

`run_activity_exposure` 的节点顺序为：

```text
validate_activity_hints
→ upsert_graph_node_activity
→ return_no_change
```

- 仅接受存在于只读注册表中的可靠 `graph_node_hints`；没有可靠 hint 时不猜节点，直接 `no_change`。
- 对每个去重后的 `(user_id, node_id, activity_type, activity_id)` 幂等处理；`event_count` 按 `aggregated_count` 累加，不能因重试重复增加。
- `page_view` 更新 `last_viewed_at`，`bookmark` 更新 `last_bookmarked_at`，`check_in` 更新 `last_check_in_at`。
- 不调用 OpenAI、不创建总结记忆、不更新 `graph_user_states.status`，返回 `no_change`。activity 只参与推荐排序。

### 10.4 SummaryMemoryGraph 节点

```text
load_source_refs
→ sanitize_and_bound_source
→ extract_candidates
→ apply_scope_and_value_policy
→ route_candidates
   ├─ 无候选 → no_change
   ├─ 低置信 → persist_review_candidates
   └─ 可写入 → resolve_existing_memories
→ resolve_graph_candidates
→ build_mutation_plan_drafts
→ prepare_commit_mutation_plans
→ commit_summary_memories
→ finalize_summary_result
```

| 节点 | DB/外部读取 | OpenAI | 副作用 |
|---|---:|---:|---:|
| `load_source_refs` | Reader | 否 | 否 |
| `sanitize_and_bound_source` | 否 | 否 | 否 |
| `extract_candidates` | 否 | 是，第 1 次 | 否 |
| `apply_scope_and_value_policy` | 否 | 否 | 否 |
| `persist_review_candidates` | DB | 否 | 写候选 |
| `resolve_existing_memories` | DB + Markdown | 否 | 否 |
| `resolve_graph_candidates` | 图谱注册表 | 否 | 否 |
| `build_mutation_plan_drafts` | 否 | 是，第 2 次 | 否 |
| `prepare_commit_mutation_plans` | DB 只读 | 否 | 否 |
| `commit_summary_memories` | DB + Markdown | 否 | 唯一总结写入节点 |
| `finalize_summary_result` | 否 | 否 | 否 |

`prepare_commit_mutation_plans` 生成 `mutation_id`、解析最终 `memory_id` 并读取当前 `expected_version`。确定计划必须先进入可序列化 Graph State，并由 LangGraph 在进入 `commit_summary_memories` 前完成 Checkpoint；副作用节点重放时必须复用同一 `mutation_id`。

`commit_summary_memories` 必须先按 `mutation_id` 查询已存在 commit；已存在则直接返回原结果。不存在时才校验 `expected_version` 并提交。其数据库事务同时写 `memory.changed` 或 `learner.updated` Outbox。总结到图谱的更新由 Consumer 创建独立 projection operation。

### 10.5 KnowledgeGraphStateGraph 节点

```text
validate_node
→ load_overlay
→ resolve_user_transition
→ commit_overlay
→ emit_graph_state_changed
```

用户命令映射：

```text
mark_unfamiliar → learning
mark_familiar   → proficient
clear           → 删除活动 Overlay 行
```

`expert` 不在用户命令枚举中。

### 10.6 Summary Projection 分支

```text
load_projection_trigger
→ validate_node_mapping
→ load_effective_active_evidence
→ evaluate_evidence
→ resolve_projected_status
→ compare_current_state
→ commit_overlay_if_changed
→ emit_graph_state_changed
```

约束：

- `memory.changed/memory.restored` 的 `apply_active_version` 必须确认 `source_version` 仍为活动版本；不是活动版本时按过期投递幂等成功结束，不应用旧事实。
- `memory.deleted` 的 `recompute_without_deleted_version` 必须确认文档 tombstone 与事件版本相符，并排除该删除版本，从其候选节点仍有效的全部活动总结记忆重新计算；不得把删除事件当作普通活动版本投影，也不执行简单的相反 delta。
- 总结记忆纠正由新 `memory.changed` 版本触发；聚合时只统计当前活动版本，因此旧版本证据自然失效。
- projection 只读取 `memory_graph_links.active=true` 且 `memory_version` 等于对应 `memory_documents.active_version` 的 link；link 是可重建映射，不是总结记忆与图谱的强绑定。
- 无候选节点、无可靠节点映射或无足够有效证据时以 `no_change` 成功结束；若重算后节点不再有足够证据，按确定性策略删除 Overlay 行或保留用户 grace 内的手动状态。
- 系统降级必须满足强证据规则并产生用户可见通知事件。

### 10.7 Maintenance 分支

```text
validate_maintenance_command
→ acquire_scheduler_lock
→ execute_bounded_batch
→ persist_cursor_or_finish
→ emit_maintenance_metric
```

维护操作一次只处理有限 batch，可通过 cursor 继续，不允许单次扫描全部用户并长时间持锁。

---

## 11. 重试、恢复和 Checkpoint

### 11.1 节点级重试

| 错误 | 节点内重试 |
|---|---|
| OpenAI 429、连接错误、5xx | 最多 2 次，退避 1s/3s，受生命周期调用上限约束 |
| Reader 临时网络错误 | 最多 2 次，退避 0.5s/2s |
| 数据库 serialization/deadlock | Service 事务最多 3 次，带 jitter |
| Markdown 临时 I/O 错误 | 最多 2 次 |
| Pydantic/Structured Output Schema 错误 | 记录一次失败后交任务级策略 |
| 权限、目标不存在、非法状态转换 | 不重试 |

### 11.2 任务级重试

| 优先级 | `max_attempts` |
|---|---:|
| P0/P1 | 6 |
| P2/P3 | 4 |
| P4 | 3 |

退避：

```text
min(5 × 2^(attempt-1), 900) 秒 + 0～20% jitter
```

永久错误直接 `dead_letter`；可由用户处理的版本/语义冲突进入 `needs_review`。

### 11.3 版本冲突

- `expected_version` 冲突后重新读取目标文档并重新规划，最多 2 轮；未提交的旧 `CommitMutationPlan` 必须废弃，新计划使用新的 `mutation_id`。
- 如果同一 `mutation_id` 已存在 commit，说明上次副作用已成功，直接返回原提交结果。
- 用户明确命令不由模型重新解释；返回 HTTP 409，要求前端刷新。
- 总结 operation 两轮仍冲突则进入 `needs_review`。

### 11.4 Checkpoint

- Graph thread 固定为 `memory-op:{operation_id}`。
- Checkpoint 保存节点恢复数据，不保存长期记忆事实。
- terminal operation 的 Checkpoint 保留 7 天。
- `needs_review` 的 Checkpoint 保留 30 天。
- `dead_letter` 的 Checkpoint 保留 30 天。
- 账号删除时 24 小时内清理相关 Checkpoint。
- 清理优先使用 Checkpointer 官方删除 API；如果当前依赖版本只能直接 SQL，必须封装为独立 `CheckpointCleanupAdapter`，根据实际安装版本确认表名、prefix 和 namespace，并用集成测试固定兼容行为。
- 清理任务不得删除仍为 `running`、Lease 未过期或正由 Gateway/Worker 执行的 operation Checkpoint。

### 11.5 Lease 和超时

```text
lease_duration         = 120 秒
heartbeat_interval     = 30 秒
operation_soft_timeout = 150 秒
operation_hard_timeout = 180 秒
```

- soft timeout：记录告警，停止非关键工作和新的可选模型调用，但继续续约 Lease，允许当前关键提交路径安全结束。
- hard timeout：取消任务协程并停止续约；尚未提交的数据库事务由连接/上下文管理器回滚，已完成 commit 不回滚。
- 数据库连接必须设置受控 `statement_timeout` 和 `lock_timeout`，单条 SQL 不得无限超过 operation hard timeout。
- Graph 已调用 `MemoryService` 后即使 Worker 被杀死，也只依赖 `mutation_id`、operation 幂等和 Lease 回收恢复，不尝试补偿已完成 commit。
- Worker 被终止后，Scheduler 回收过期 Lease；幂等 mutation 保证恢复执行不会重复提交。

### 11.6 Operation 取消

- `queued/retry_wait`：在持有 operation 行锁后立即标记 `cancelled`，清除 Lease，并写取消结果。
- `running` 且未进入 commit 副作用：设置 `cancel_requested_at`；Runner 在每个 Graph 节点入口、外部调用返回后和任何 commit 前检查，命中后协作结束为 `cancelled`。
- 已进入 commit 副作用：不允许取消，API 返回 409；已经开始的事务必须完成提交或正常回滚，不能留下半提交。
- `needs_review`：允许取消 operation，但已生成候选继续保留，候选只能通过审核或到期规则处理。
- `succeeded/dead_letter/cancelled`：不允许再次取消。
- 取消必须写审计和 operation result，至少包含 `cancelled_at`、请求 actor、原状态和是否在运行中协作取消。
- 同一幂等键重发仍返回原 `cancelled` operation；用户确需重新执行时必须提交新的幂等键。

---

## 12. 检索和上下文组装

### 12.1 第一版搜索方案

第一版使用 PostgreSQL `pg_trgm` 混合确定性检索，不引入向量库。

索引内容：

- `topic_key`、`topic_title`。
- Markdown 标题、当前掌握概况、已掌握、仍有困难、建议复习。
- learner 的偏好、目标和计划。
- front matter 中的 `memory_type`、版本和受控关键词。
- 不索引 Markdown 语法符号、用户 ID、内部 storage key 和完整审计信息。

中文策略：

- 保留原始中文规范文本。
- 对空白、全半角和标点做 NFKC 规范化。
- `search_text` 建立 `GIN (... gin_trgm_ops)`。
- 精确 `topic_key/topic_title` 匹配优先，其次 trigram similarity，再按更新时间排序。
- 第一版不安装中文分词扩展，不使用 PostgreSQL 默认英文分词冒充中文检索。

### 12.2 `search_summary` 排序

```text
exact topic_key             +100
exact normalized title      +90
prefix title match          +70
trigram similarity × 60
明确 topic filter           +40
最近 30 天更新              +0～10
已删除/隔离                 排除
```

阈值：`similarity >= 0.20` 才进入候选；默认返回 10 条，最大 50 条。

自动 merge 判断只使用原始 trigram similarity 和规范标题匹配，不使用上述综合排序分。

### 12.3 返回结构

```python
class MemorySearchHit(BaseModel):
    memory_id: str
    memory_type: Literal["learner", "mastery"]
    topic_key: str | None
    title: str
    summary: str
    matched_excerpt: str | None
    evidence_refs: list[str] = Field(max_length=100)
    version: int
    updated_at: datetime
    confidence: float | None
    score: float
```

默认不返回完整 Markdown；`GET /memories/{memory_id}` 才返回结构化完整内容。对 Agent 的返回必须包含版本和有限证据引用。

### 12.4 LearningContextService

默认 `token_budget=3000`，允许范围 500～8000。

优先级：

1. 与当前 query/topic 精确相关的 mastery。
2. 用户明确目标、计划和学习偏好。
3. 与请求明确相关的图谱状态和推荐原因。
4. 其他弱相关总结记忆。

超预算裁剪：

1. 先删除低排序文档。
2. 再移除旧 evidence 详情，只保留 evidence ref。
3. 再压缩建议复习和历史描述。
4. 不截断单条事实到语义不完整。
5. learner 中与当前任务无关的字段不注入。

总结记忆和图谱 Overlay 只在上下文组装阶段弱融合，不合并存储。

### 12.5 `LearningContext` 返回结构

```python
class LearningContextLearner(BaseModel):
    preferences: list[str]
    goals: list[str]
    plans: list[str]
    version: int
    updated_at: datetime
    evidence_refs: list[str] = Field(max_length=100)


class LearningContextMastery(BaseModel):
    memory_id: str
    topic_key: str
    title: str
    overview: str
    understood: list[str]
    difficulties: list[str]
    review_advice: list[str]
    version: int
    updated_at: datetime
    evidence_refs: list[str] = Field(max_length=100)


class LearningContextGraphState(BaseModel):
    node_id: str
    title: str
    status: Literal["learning", "proficient", "expert"] | None
    reason_codes: list[str]


class LearningContextTokenUsage(BaseModel):
    budget: int = Field(ge=0)
    estimated: int = Field(ge=0)
    remaining: int = Field(ge=0)


class LearningContext(BaseModel):
    user_id: UUID
    query: str
    learner: LearningContextLearner | None
    mastery: list[LearningContextMastery]
    graph_states: list[LearningContextGraphState]
    recommendations: list[GraphRecommendation]
    token_usage: LearningContextTokenUsage
    truncated: bool
```

---

## 13. PostgreSQL DDL

以下 DDL 是字段和约束基线；Alembic migration 可以使用 SQLAlchemy 类型表达，但不能改变语义。

### 13.1 扩展和身份映射

```sql
CREATE EXTENSION IF NOT EXISTS pgcrypto;
CREATE EXTENSION IF NOT EXISTS pg_trgm;

CREATE TABLE account_identity_mappings (
    internal_user_id uuid NOT NULL,
    issuer varchar(300) NOT NULL,
    external_subject varchar(300) NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (internal_user_id, issuer),
    UNIQUE (issuer, external_subject)
);

CREATE INDEX ix_identity_mapping_internal_user
    ON account_identity_mappings (internal_user_id);
```

PostgreSQL 内部用户 ID 固定为 UUID；网站 JWT 的 `sub` 可以是任意稳定字符串，由认证适配器通过 `(issuer, external_subject)` 映射到 `internal_user_id`。映射由网站账户服务或受控维护 CLI 创建，不接受浏览器直接写入。

身份映射维护 CLI：

```bash
uv run python -m backend.memory.cli create-identity-mapping \
  --issuer <issuer> \
  --external-subject <subject> \
  --internal-user-id <uuid> \
  [--dry-run] [--replace-existing]
```

默认冲突即拒绝；`--dry-run` 只校验并显示将要变更的摘要；`--replace-existing` 只允许受控维护身份使用，并写入审计。生产最终由网站账户服务创建映射，浏览器不得直接调用该 CLI 或对应数据库。

### 13.2 `memory_operations`

```sql
CREATE TABLE memory_operations (
    operation_id uuid PRIMARY KEY,
    user_id uuid NOT NULL,
    actor_type text NOT NULL CHECK (actor_type IN (
        'user', 'conversation_agent', 'activity_agent',
        'knowledge_graph_ui', 'summary_projection', 'system', 'admin'
    )),
    input_kind text NOT NULL CHECK (input_kind IN (
        'evidence', 'command', 'projection', 'maintenance'
    )),
    operation_type text NOT NULL CHECK (operation_type IN (
        'conversation_evidence', 'activity_evidence',
        'correct_memory', 'forget_memory', 'restore_memory',
        'override_learner_profile', 'review_candidate',
        'set_graph_state', 'project_summary_to_graph',
        'rebuild_index', 'verify_checksums', 'purge_tombstones',
        'cleanup_orphan_versions', 'cleanup_checkpoints',
        'purge_account_memory'
    )),
    idempotency_key varchar(200) NOT NULL,
    idempotency_payload_hash char(64) NOT NULL,
    priority smallint NOT NULL CHECK (priority BETWEEN 0 AND 100),
    status text NOT NULL CHECK (status IN (
        'queued', 'running', 'retry_wait', 'succeeded',
        'needs_review', 'dead_letter', 'cancelled'
    )),
    payload jsonb NOT NULL,
    result jsonb,
    public_error jsonb,
    trace_id varchar(64) NOT NULL,
    graph_thread_id varchar(128) NOT NULL,
    occurred_at timestamptz NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    started_at timestamptz,
    completed_at timestamptz,
    next_run_at timestamptz NOT NULL DEFAULT now(),
    attempt_count integer NOT NULL DEFAULT 0 CHECK (attempt_count >= 0),
    max_attempts integer NOT NULL CHECK (max_attempts BETWEEN 1 AND 20),
    locked_by varchar(128),
    lease_expires_at timestamptz,
    last_heartbeat_at timestamptz,
    llm_call_count integer NOT NULL DEFAULT 0 CHECK (llm_call_count >= 0),
    cancel_requested_at timestamptz,
    CONSTRAINT uq_memory_operation_idempotency
        UNIQUE (user_id, actor_type, idempotency_key)
);

CREATE INDEX ix_memory_operations_claim
    ON memory_operations (priority DESC, created_at ASC)
    WHERE status IN ('queued', 'retry_wait');

CREATE INDEX ix_memory_operations_user_created
    ON memory_operations (user_id, created_at DESC);

CREATE INDEX ix_memory_operations_lease
    ON memory_operations (lease_expires_at)
    WHERE status = 'running';
```

Payload 使用 JSONB 保存经过 Pydantic 校验的引用型数据，不保存完整对话和论坛正文。

### 13.3 `memory_documents`

```sql
CREATE TABLE memory_documents (
    user_id uuid NOT NULL,
    memory_id varchar(160) NOT NULL,
    memory_type text NOT NULL CHECK (memory_type IN ('index', 'learner', 'mastery')),
    topic_key varchar(160),
    topic_title varchar(240),
    logical_path varchar(500) NOT NULL,
    active_version bigint,
    active_storage_key varchar(1000),
    active_checksum char(64),
    deleted_version bigint,
    index_dirty_at timestamptz,
    deleted_at timestamptz,
    tombstone_until timestamptz,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (user_id, memory_id),
    CONSTRAINT uq_memory_document_path UNIQUE (user_id, logical_path),
    CONSTRAINT ck_memory_document_topic CHECK (
        (memory_type = 'mastery' AND topic_key IS NOT NULL AND topic_title IS NOT NULL)
        OR
        (memory_type IN ('index', 'learner') AND topic_key IS NULL)
    ),
    CONSTRAINT ck_memory_document_deleted_state CHECK (
        deleted_at IS NULL
        OR (
            active_version IS NULL
            AND active_storage_key IS NULL
            AND active_checksum IS NULL
            AND deleted_version IS NOT NULL
            AND tombstone_until IS NOT NULL
        )
    )
);

CREATE UNIQUE INDEX uq_memory_mastery_topic
    ON memory_documents (user_id, topic_key)
    WHERE memory_type = 'mastery';

CREATE INDEX ix_memory_documents_active
    ON memory_documents (user_id, updated_at DESC)
    WHERE deleted_at IS NULL;

CREATE INDEX ix_memory_documents_tombstone
    ON memory_documents (tombstone_until)
    WHERE deleted_at IS NOT NULL;

CREATE INDEX ix_memory_documents_index_dirty
    ON memory_documents (index_dirty_at)
    WHERE memory_type = 'index' AND index_dirty_at IS NOT NULL;
```

`logical_path` 是相对于用户 memory 根目录的逻辑路径，例如 `mastery/一致收敛.md`，不得存绝对路径。

### 13.4 `memory_commits`

```sql
CREATE TABLE memory_commits (
    commit_id uuid PRIMARY KEY,
    mutation_id uuid NOT NULL UNIQUE,
    operation_id uuid NOT NULL REFERENCES memory_operations(operation_id),
    user_id uuid NOT NULL,
    memory_id varchar(160) NOT NULL,
    action text NOT NULL CHECK (action IN (
        'create', 'merge', 'replace', 'append_evidence',
        'forget', 'restore', 'rebuild_index'
    )),
    before_version bigint,
    after_version bigint,
    storage_key varchar(1000),
    checksum char(64),
    actor_type text NOT NULL,
    evidence_refs jsonb NOT NULL DEFAULT '[]'::jsonb CHECK (jsonb_typeof(evidence_refs) = 'array' AND jsonb_array_length(evidence_refs) <= 100),
    commit_payload jsonb NOT NULL DEFAULT '{}'::jsonb,
    prompt_version varchar(100),
    model_name varchar(100),
    created_at timestamptz NOT NULL DEFAULT now(),
    FOREIGN KEY (user_id, memory_id)
        REFERENCES memory_documents(user_id, memory_id)
);

CREATE INDEX ix_memory_commits_document
    ON memory_commits (user_id, memory_id, created_at DESC);

CREATE INDEX ix_memory_commits_operation
    ON memory_commits (operation_id);
```

`commit_payload` 保存结构化 patch、原因码和必要审计，不保存完整 Prompt、原始对话、模型隐藏 reasoning 或认证信息。版本语义固定为：普通 create/update 使用实际前后版本；`forget` 使用 `before_version=删除前活动版本、after_version=NULL`；`restore` 使用 `before_version=NULL、after_version=新活动版本`。`storage_key/checksum` 对 forget 可以为 `NULL`，restore 必须指向新不可变版本。

### 13.5 `memory_index_entries`

```sql
CREATE TABLE memory_index_entries (
    user_id uuid NOT NULL,
    memory_id varchar(160) NOT NULL,
    source_version bigint NOT NULL,
    memory_type text NOT NULL CHECK (memory_type IN ('learner', 'mastery')),
    topic_key varchar(160),
    title varchar(240) NOT NULL,
    summary text NOT NULL,
    keywords text[] NOT NULL DEFAULT '{}',
    search_text text NOT NULL,
    evidence_refs jsonb NOT NULL DEFAULT '[]'::jsonb CHECK (jsonb_typeof(evidence_refs) = 'array' AND jsonb_array_length(evidence_refs) <= 100),
    confidence real,
    updated_at timestamptz NOT NULL,
    PRIMARY KEY (user_id, memory_id),
    FOREIGN KEY (user_id, memory_id)
        REFERENCES memory_documents(user_id, memory_id)
        ON DELETE CASCADE
);

CREATE INDEX ix_memory_index_search_trgm
    ON memory_index_entries USING gin (search_text gin_trgm_ops);

CREATE INDEX ix_memory_index_title_trgm
    ON memory_index_entries USING gin (title gin_trgm_ops);

CREATE INDEX ix_memory_index_keywords
    ON memory_index_entries USING gin (keywords);
```

### 13.6 `memory_review_candidates`

```sql
CREATE TABLE memory_review_candidates (
    candidate_id uuid PRIMARY KEY,
    operation_id uuid NOT NULL REFERENCES memory_operations(operation_id),
    user_id uuid NOT NULL,
    candidate_type text NOT NULL CHECK (candidate_type IN (
        'learner', 'mastery', 'topic_conflict', 'version_conflict'
    )),
    base_memory_id varchar(160),
    base_version bigint,
    topic_key varchar(160),
    normalized_match_key varchar(300) NOT NULL,
    resolution_target text CHECK (resolution_target IN (
        'merge_existing', 'create_new_topic'
    )),
    target_memory_id varchar(160),
    resolved_operation_id uuid REFERENCES memory_operations(operation_id),
    candidate_payload jsonb NOT NULL,
    evidence_refs jsonb NOT NULL DEFAULT '[]'::jsonb CHECK (jsonb_typeof(evidence_refs) = 'array' AND jsonb_array_length(evidence_refs) <= 100),
    confidence real NOT NULL CHECK (confidence BETWEEN 0 AND 1),
    status text NOT NULL CHECK (status IN (
        'pending', 'accepted', 'corrected', 'rejected', 'expired'
    )),
    reviewed_by uuid,
    reviewed_at timestamptz,
    tombstone_until timestamptz,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX ix_memory_review_user_pending
    ON memory_review_candidates (user_id, created_at DESC)
    WHERE status = 'pending';

CREATE INDEX ix_memory_review_match_key
    ON memory_review_candidates (user_id, normalized_match_key, created_at DESC);
```

候选 accept/correct 必须读取 `base_memory_id/base_version` 与当前活动版本：

- 当前未变化：直接生成新 commit plan。
- 当前已变化：按确定性规则重放 patch；无法安全重放时生成 `version_conflict` 候选。
- `topic_conflict` 的 accept/correct 将用户裁决写入 `resolution_target/target_memory_id`；`merge_existing` 必须给出当前用户下存在且类型兼容的 `target_memory_id`，`create_new_topic` 不得复用已有主题 ID。
- 所有 accept/correct/reject/expire 都写 `resolved_operation_id`、`reviewed_at` 和最终 status；它们与生成候选的原 `operation_id` 分开，形成完整审计链。
- reject 使用 `normalized_match_key` 阻止 30 天内重复候选。

### 13.7 记忆删除抑制

```sql
CREATE TABLE memory_deleted_evidence_suppressions (
    user_id uuid NOT NULL,
    memory_id varchar(160) NOT NULL,
    evidence_ref_hash char(64) NOT NULL,
    hash_key_version varchar(32) NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (user_id, memory_id, evidence_ref_hash)
);
```

### 13.8 知识图谱只读注册表

```sql
CREATE TABLE knowledge_graph_nodes (
    node_id varchar(16) PRIMARY KEY CHECK (node_id ~ '^n[0-9]{3,}$'),
    title varchar(300) NOT NULL,
    group_key varchar(100),
    source_file varchar(300) NOT NULL,
    source_checksum char(64) NOT NULL,
    metadata jsonb NOT NULL DEFAULT '{}'::jsonb,
    synced_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE knowledge_graph_edges (
    from_node_id varchar(16) NOT NULL REFERENCES knowledge_graph_nodes(node_id),
    to_node_id varchar(16) NOT NULL REFERENCES knowledge_graph_nodes(node_id),
    relation_type text NOT NULL DEFAULT 'prerequisite',
    source_checksum char(64) NOT NULL,
    PRIMARY KEY (from_node_id, to_node_id, relation_type)
);

CREATE TABLE knowledge_graph_sync_runs (
    run_id uuid PRIMARY KEY,
    graph_file varchar(300) NOT NULL,
    graph_checksum char(64) NOT NULL,
    catalog_file varchar(300) NOT NULL,
    catalog_checksum char(64) NOT NULL,
    manifest_checksum char(64) NOT NULL,
    applied boolean NOT NULL DEFAULT false,
    result jsonb,
    created_at timestamptz NOT NULL DEFAULT now(),
    applied_at timestamptz
);

CREATE TABLE knowledge_graph_node_aliases (
    node_id varchar(16) NOT NULL REFERENCES knowledge_graph_nodes(node_id) ON DELETE CASCADE,
    alias varchar(300) NOT NULL,
    normalized_alias varchar(300) NOT NULL,
    alias_source text NOT NULL CHECK (alias_source IN (
        'repository', 'manual_curated', 'derived'
    )),
    created_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (node_id, normalized_alias)
);

CREATE INDEX ix_knowledge_graph_alias_lookup
    ON knowledge_graph_node_aliases (normalized_alias);
```



```sql
CREATE TABLE knowledge_graph_node_removal_audit (
    removal_audit_id uuid PRIMARY KEY,
    sync_run_id uuid NOT NULL REFERENCES knowledge_graph_sync_runs(run_id),
    node_id varchar(16) NOT NULL,
    record_type text NOT NULL CHECK (record_type IN (
        'graph_user_states', 'graph_user_node_activity', 'memory_graph_links'
    )),
    user_hash char(64),
    original_record_checksum char(64) NOT NULL,
    affected_count integer NOT NULL CHECK (affected_count >= 0),
    created_at timestamptz NOT NULL DEFAULT now()
);
```
业务 API 对节点和边表只读。只有部署/维护命令可以根据仓库文件同步；同步必须校验完整文件 checksum，并在一个事务中替换注册表。

如果同步会删除已有 Overlay、activity 或 `memory_graph_links` 引用的节点，默认失败；只有显式 `--allow-remove` 时允许删除。该选项必须先写 `graph_state_audit(reason_code=graph_node_removed)`、`knowledge_graph_node_removal_audit`（按记录类型、用户 HMAC、原记录 checksum 和 sync run 归档），再删除受影响的 `graph_user_states`、`graph_user_node_activity`、`memory_graph_links`，最后删除节点和边；禁止无审计级联删除。

### 13.8.1 `memory_graph_links`：总结记忆到图谱的可重建弱连接

```sql
CREATE TABLE memory_graph_links (
    user_id uuid NOT NULL,
    memory_id varchar(160) NOT NULL,
    node_id varchar(16) NOT NULL REFERENCES knowledge_graph_nodes(node_id),
    memory_version bigint NOT NULL CHECK (memory_version >= 1),
    mapping_method text NOT NULL CHECK (mapping_method IN (
        'explicit_hint', 'exact_alias', 'model_candidate'
    )),
    mapping_confidence real NOT NULL CHECK (mapping_confidence BETWEEN 0 AND 1),
    active boolean NOT NULL DEFAULT true,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (user_id, memory_id, node_id),
    FOREIGN KEY (user_id, memory_id)
        REFERENCES memory_documents(user_id, memory_id)
);

CREATE INDEX ix_memory_graph_links_active_node
    ON memory_graph_links (user_id, node_id, memory_version)
    WHERE active = true;

CREATE INDEX ix_memory_graph_links_active_memory
    ON memory_graph_links (user_id, memory_id, memory_version)
    WHERE active = true;
```

该表是可重建的派生映射，不是总结记忆与知识图谱的强绑定。mastery 活动版本提交后 upsert link 并更新 `memory_version`；映射失效或记忆删除时将 `active=false`；恢复以新活动版本重新激活或创建。projection 只读取 `active=true` 且版本等于 `memory_documents.active_version` 的 link。`graph_user_states.source_memory_id` 只保留主要来源指针，`related_memory_ids` 必须从本表读取。

### 13.9 `graph_user_states` 和 activity 聚合

```sql
CREATE TABLE graph_user_states (
    user_id uuid NOT NULL,
    node_id varchar(16) NOT NULL REFERENCES knowledge_graph_nodes(node_id),
    status text NOT NULL CHECK (status IN ('learning', 'proficient', 'expert')),
    version bigint NOT NULL DEFAULT 1 CHECK (version >= 1),
    status_source text NOT NULL CHECK (status_source IN (
        'user', 'summary_memory', 'system_recompute'
    )),
    source_memory_id varchar(160),
    source_memory_version bigint,
    evidence_snapshot jsonb NOT NULL DEFAULT '[]'::jsonb CHECK (jsonb_typeof(evidence_snapshot) = 'array' AND jsonb_array_length(evidence_snapshot) <= 50),
    evidence_count integer NOT NULL DEFAULT 0 CHECK (evidence_count >= 0),
    last_user_action_at timestamptz,
    last_evidence_at timestamptz,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (user_id, node_id)
);

CREATE INDEX ix_graph_user_states_status
    ON graph_user_states (user_id, status, updated_at DESC);

CREATE TABLE graph_user_node_activity (
    user_id uuid NOT NULL,
    node_id varchar(16) NOT NULL REFERENCES knowledge_graph_nodes(node_id),
    last_viewed_at timestamptz,
    last_bookmarked_at timestamptz,
    last_check_in_at timestamptz,
    event_count integer NOT NULL DEFAULT 0 CHECK (event_count >= 0),
    updated_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (user_id, node_id)
);

CREATE INDEX ix_graph_user_node_activity_recommendation
    ON graph_user_node_activity (user_id, updated_at DESC);
```

无状态表示不存在 `graph_user_states` 活动行；activity 记录不等同于状态。用户 `clear` 删除活动 Overlay 行并写 `graph_state_audit`，不删除 `graph_user_node_activity`。

### 13.10 `graph_state_audit`

```sql
CREATE TABLE graph_state_audit (
    audit_id uuid PRIMARY KEY,
    operation_id uuid REFERENCES memory_operations(operation_id),
    user_id uuid NOT NULL,
    node_id varchar(16) NOT NULL,
    before_status text,
    after_status text,
    before_version bigint,
    after_version bigint,
    actor_type text NOT NULL,
    reason_codes text[] NOT NULL DEFAULT '{}',
    evidence_refs jsonb NOT NULL DEFAULT '[]'::jsonb CHECK (jsonb_typeof(evidence_refs) = 'array' AND jsonb_array_length(evidence_refs) <= 50),
    explanation_summary varchar(500),
    created_at timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX ix_graph_state_audit_user_node
    ON graph_state_audit (user_id, node_id, created_at DESC);
```

### 13.10.1 `source_deletions`

```sql
CREATE TABLE source_deletions (
    source_deletion_id uuid PRIMARY KEY,
    user_id uuid NOT NULL,
    source_system text NOT NULL CHECK (source_system IN ('conversation', 'activity')),
    source_ref varchar(500) NOT NULL,
    source_version varchar(200),
    deleted_at timestamptz NOT NULL,
    idempotency_hash char(64) NOT NULL UNIQUE,
    created_at timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX ix_source_deletions_lookup
    ON source_deletions (user_id, source_system, source_ref, source_version);
```

`idempotency_hash` 的输入必须包含 `user_id`、`source_system`、`source_ref`、`source_version` 和规范化删除事件标识。Reader 查询时必须过滤 `source_deletions`；`source_version=NULL` 表示该 `source_ref` 的全部版本已删除。第一版只记录删除事实并抑制 Reader 返回，不实现跨证据全量重算；账号删除时物理删除这些记录。

### 13.11 `memory_outbox`

```sql
CREATE TABLE memory_outbox (
    outbox_id uuid PRIMARY KEY,
    operation_id uuid REFERENCES memory_operations(operation_id),
    user_id uuid NOT NULL,
    event_type text NOT NULL CHECK (event_type IN (
        'memory.changed',
        'memory.deleted',
        'memory.restored',
        'learner.updated',
        'review_candidate.created',
        'review_candidate.resolved',
        'graph_state.changed',
        'graph_state.explanation_available',
        'account_memory.purge_requested'
    )),
    aggregate_type text NOT NULL,
    aggregate_id varchar(200) NOT NULL,
    aggregate_version bigint NOT NULL DEFAULT 0 CHECK (aggregate_version >= 0),
    payload jsonb NOT NULL,
    status text NOT NULL DEFAULT 'pending' CHECK (status IN (
        'pending', 'publishing', 'published', 'retry_wait', 'dead_letter'
    )),
    attempt_count integer NOT NULL DEFAULT 0,
    max_attempts integer NOT NULL DEFAULT 10,
    next_run_at timestamptz NOT NULL DEFAULT now(),
    locked_by varchar(128),
    lease_expires_at timestamptz,
    created_at timestamptz NOT NULL DEFAULT now(),
    published_at timestamptz,
    last_error jsonb,
    CONSTRAINT uq_memory_outbox_event
        UNIQUE (event_type, aggregate_type, aggregate_id, aggregate_version)
);

CREATE INDEX ix_memory_outbox_claim
    ON memory_outbox (created_at ASC)
    WHERE status IN ('pending', 'retry_wait');
```

### 13.12 Outbox delivery / inbox

```sql
CREATE TABLE memory_outbox_deliveries (
    delivery_id uuid PRIMARY KEY,
    outbox_id uuid NOT NULL REFERENCES memory_outbox(outbox_id) ON DELETE CASCADE,
    target text NOT NULL CHECK (target IN (
        'summary_projection',
        'user_notification',
        'internal_event_log'
    )),
    status text NOT NULL DEFAULT 'pending' CHECK (status IN (
        'pending', 'succeeded', 'retry_wait', 'dead_letter'
    )),
    idempotency_key varchar(300) NOT NULL,
    attempt_count integer NOT NULL DEFAULT 0,
    last_error jsonb,
    created_at timestamptz NOT NULL DEFAULT now(),
    completed_at timestamptz,
    UNIQUE (outbox_id, target),
    UNIQUE (target, idempotency_key)
);

CREATE TABLE memory_internal_event_log (
    event_log_id uuid PRIMARY KEY,
    outbox_id uuid NOT NULL REFERENCES memory_outbox(outbox_id) ON DELETE CASCADE,
    event_type varchar(100) NOT NULL,
    idempotency_key varchar(300) NOT NULL UNIQUE,
    user_id uuid NOT NULL,
    payload jsonb NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now()
);
```

每个启用 target 都有一行 delivery；Consumer 以 `(event_type, target)` 路由并在目标侧使用 inbox/唯一幂等键。某个 target 失败只将该 delivery 置为 `retry_wait` 或 `dead_letter`，不影响其他 target 重试；Outbox 主行只有在所有第一版启用 target 成功后才能标记 `published`。如果任一启用 target 进入 `dead_letter`，主行保持 `dead_letter` 并产生运维告警。

### 13.13 用户通知

```sql
CREATE TABLE memory_user_notifications (
    notification_id uuid PRIMARY KEY,
    user_id uuid NOT NULL,
    event_type text NOT NULL,
    title varchar(200) NOT NULL,
    body varchar(1000) NOT NULL,
    aggregate_type varchar(100) NOT NULL,
    aggregate_id varchar(200) NOT NULL,
    source_outbox_id uuid NOT NULL REFERENCES memory_outbox(outbox_id),
    read_at timestamptz,
    created_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (source_outbox_id)
);

CREATE INDEX ix_memory_notifications_user
    ON memory_user_notifications (user_id, created_at DESC);
```

通知为用户可见派生记录，默认保留 90 天；Scheduler 按 `created_at` 分批清理已读和未读记录，清理不删除对应的最小审计元数据或 Outbox 投递记录。

### 13.14 备份、维护和模型指标表

```sql
CREATE TABLE backup_runs (
    batch_id uuid PRIMARY KEY,
    status text NOT NULL CHECK (status IN ('running', 'succeeded', 'failed')),
    backup_root varchar(1000) NOT NULL,
    postgres_artifact varchar(1000) NOT NULL,
    markdown_artifact varchar(1000) NOT NULL,
    manifest_artifact varchar(1000) NOT NULL,
    postgres_checksum char(64),
    markdown_checksum char(64),
    manifest_checksum char(64),
    restore_verification_status text NOT NULL DEFAULT 'pending' CHECK (
        restore_verification_status IN ('pending', 'succeeded', 'failed')
    ),
    restore_verified_at timestamptz,
    restore_verification_error varchar(1000),
    started_at timestamptz NOT NULL DEFAULT now(),
    completed_at timestamptz,
    error_summary varchar(1000)
);

CREATE INDEX ix_backup_runs_status_started
    ON backup_runs (status, started_at DESC);

CREATE TABLE memory_maintenance_runs (
    run_id uuid PRIMARY KEY,
    operation_id uuid REFERENCES memory_operations(operation_id),
    maintenance_type text NOT NULL,
    idempotency_key varchar(200) NOT NULL UNIQUE,
    status text NOT NULL CHECK (status IN (
        'queued', 'running', 'succeeded', 'failed'
    )),
    cursor varchar(500),
    result jsonb,
    started_at timestamptz,
    completed_at timestamptz,
    created_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE memory_llm_call_metrics (
    call_id uuid PRIMARY KEY,
    operation_id uuid NOT NULL REFERENCES memory_operations(operation_id),
    user_hash char(64) NOT NULL,
    model_name varchar(100) NOT NULL,
    prompt_version varchar(100) NOT NULL,
    schema_name varchar(100) NOT NULL,
    status text NOT NULL,
    input_tokens integer,
    output_tokens integer,
    latency_ms integer,
    error_code varchar(100),
    created_at timestamptz NOT NULL DEFAULT now()
);
```

不创建保存原始 Prompt/模型输出的 `llm_call_logs` 表。

### 13.15 Break-glass

```sql
CREATE TABLE memory_break_glass_grants (
    grant_id uuid PRIMARY KEY,
    admin_user_id uuid NOT NULL,
    target_user_id uuid NOT NULL,
    reason varchar(500) NOT NULL,
    scopes text[] NOT NULL,
    approved_by uuid,
    expires_at timestamptz NOT NULL,
    revoked_at timestamptz,
    created_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE memory_break_glass_audit (
    audit_id uuid PRIMARY KEY,
    grant_id uuid NOT NULL REFERENCES memory_break_glass_grants(grant_id),
    admin_user_id uuid NOT NULL,
    target_user_id uuid NOT NULL,
    action varchar(100) NOT NULL,
    resource_type varchar(100) NOT NULL,
    resource_id varchar(300),
    trace_id varchar(64) NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now()
);
```

没有未过期 grant 时，admin 只能访问元数据。第一版通过本地 CLI 创建 grant，不做管理网页；grant 必须绑定唯一目标用户、必填 reason 和 scopes，最长有效期 60 分钟，生产环境申请者与批准者必须不同。所有申请、批准、使用、撤销和过期检查，以及所有正文读取和修改，必须写入 `memory_break_glass_audit`。

### 13.16 账号删除 manifest

```sql
CREATE TABLE account_deletion_manifest (
    account_deletion_id uuid PRIMARY KEY,
    user_hash char(64) NOT NULL UNIQUE,
    user_hash_key_version varchar(32) NOT NULL,
    status text NOT NULL CHECK (status IN ('requested', 'running', 'completed', 'failed')),
    requested_at timestamptz NOT NULL,
    purge_completed_at timestamptz,
    backup_retention_until timestamptz NOT NULL,
    completion_proof_checksum char(64),
    created_at timestamptz NOT NULL DEFAULT now()
);
```



```sql
CREATE TABLE memory_privacy_audit_records (
    privacy_audit_id uuid PRIMARY KEY,
    user_hash char(64) NOT NULL,
    user_hash_key_version varchar(32) NOT NULL,
    action varchar(100) NOT NULL,
    actor_hash char(64),
    occurred_at timestamptz NOT NULL,
    proof_checksum char(64) NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX ix_memory_privacy_audit_user_time
    ON memory_privacy_audit_records (user_hash, occurred_at DESC);
```
恢复旧备份时必须读取该 manifest 并重新应用账号删除；manifest 本身不得包含可还原的用户正文。



账号删除逐表规则：

- 物理删除：`account_identity_mappings`、`memory_operations`、`memory_documents`、`memory_commits`、`memory_index_entries`、`memory_review_candidates`、`memory_deleted_evidence_suppressions`、`source_deletions`、`graph_user_states`、`graph_user_node_activity`、`graph_state_audit` 中的用户原始记录、`memory_graph_links`、`memory_user_notifications`、`memory_internal_event_log`、用户的 `memory_outbox`/`memory_outbox_deliveries`、`memory_llm_call_metrics`、用户 maintenance/operation 数据、Markdown 当前/历史/quarantine/孤立版本以及 Checkpoint。
- `memory_break_glass_grants` 和 `memory_break_glass_audit` 先压缩目标用户相关记录：只保留 `user_hash`、动作、时间、admin/actor 标识摘要和 `proof_checksum`，写入 `memory_privacy_audit_records`；随后删除原始 `target_user_id`、resource ID 和正文关联。
- 长期保留：`account_deletion_manifest`、上述最小隐私审计、完成证明 checksum 和不含用户标识的聚合指标。恢复旧备份时必须先重放 manifest，再允许任何用户级数据恢复。

### 13.17 Tombstone

不单独创建 `memory_tombstones` 表。文档 tombstone 使用 `memory_documents.deleted_at/tombstone_until`；候选 tombstone 使用 `memory_review_candidates.tombstone_until`。物理清理后只保留正文不可还原的 checksum、时间和动作审计。

### 13.18 事务和锁

第一版隔离级别使用 `READ COMMITTED` + 显式锁；数据库连接必须为 operation 级别设置受控 `statement_timeout` 和 `lock_timeout`，具体值固定为 `DATABASE_STATEMENT_TIMEOUT_MS=150000`、`DATABASE_LOCK_TIMEOUT_MS=10000`，由 settings 注入；该值为 180 秒 hard timeout 留出清理余量：

1. 锁 operation 行。
2. 获取用户级 PostgreSQL transaction advisory lock。
3. 按 `memory_id` 字典序 `SELECT ... FOR UPDATE` 锁文档。
4. 按 `node_id` 字典序锁图谱 Overlay。
5. 校验 `expected_version`。
6. 写 commits、索引、活动指针、审计和 Outbox。
7. 提交事务。

用户级锁 key 由固定 namespace 和 `user_id` 稳定 hash 组成。所有 Memory 写入口必须使用同一实现，禁止各模块自行定义锁顺序。

死锁、serialization failure 最多重试 3 次；重试仍失败则交任务级重试。



### 13.19 大字段保护

以下 JSON 字段必须同时有 Pydantic 上限和 PostgreSQL `jsonb` 数组长度 CHECK：

```text
graph_user_states.evidence_snapshot <= 50
graph_state_audit.evidence_refs <= 50
memory_commits.evidence_refs <= 100
memory_index_entries.evidence_refs <= 100
memory_review_candidates.evidence_refs <= 100
GraphStateExplanation.evidence_refs <= 10
```

超限返回 `SOURCE_TOO_LARGE` 或 `INVALID_PAYLOAD`（按超限字段归类），不得写入再裁剪后的隐含版本。
---

## 14. Worker、Scheduler、Outbox 和部署

### 14.1 Worker

第一版默认配置：

```text
进程数                     = 1
单进程 asyncio 并发         = 4
每批领取                    = 10
空队列轮询间隔              = 1 秒
Lease                       = 120 秒
心跳                        = 30 秒
硬超时                      = 180 秒
优雅关闭等待                = 30 秒
```

领取 SQL 使用 `FOR UPDATE SKIP LOCKED`。领取和设置 Lease 必须在同一数据库事务中完成。

收到 SIGTERM 后：

1. 停止领取新任务。
2. 等待运行任务最多 30 秒。
3. 对未完成任务停止续约 Lease。
4. 由 Scheduler 在 Lease 过期后重新入队。

### 14.2 P0/P1 快速命令

用户纠正、删除、恢复和图谱标记必须经过 `MemoryManagerGraph` 的确定性分支，不允许 Gateway 直接调用 `MemoryService` 绕过 Graph。

Gateway 与 Worker 必须复用同一个 `claim_operation` 函数：

1. Gateway 在数据库持久化 operation。
2. Gateway 使用 `FOR UPDATE SKIP LOCKED` 尝试领取该 operation。
3. 领取成功则由 `MemoryGraphRunner` 启动执行，Gateway 最多等待 2 秒。
4. 2 秒内完成则返回 200。
5. Gateway 未领取到 operation 时立即返回 202，由 Worker 按同一 claim 规则接管。
6. Gateway 已领取并启动 Runner、但 2 秒内未完成时返回 202；HTTP 请求超时或客户端断开不得取消 Runner，也不得把正在运行的 operation 重置为 `queued`。Runner 继续执行并续约 Lease，Worker 只能在 Lease 过期且 Scheduler 回收后重新领取。
7. 启动 Runner 前发生临时错误且实际未开始执行时，才允许在事务中恢复为 `queued/retry_wait`。

`SUMMARY_COMMAND` 是 Graph 内部的确定性适配器，不是绕过 Graph 的公开路径。

### 14.3 Scheduler

Scheduler 为独立进程。第一版虽然只部署一个实例，仍使用 PostgreSQL advisory lock，防止误启动多实例重复调度。

时间表：

| 任务 | 频率 |
|---|---|
| 回收过期 operation Lease | 每 30 秒 |
| 回收过期 Outbox Lease | 每 30 秒 |
| 调度 dirty `index.md` 重建 | 每 5 分钟 |
| 检查 dead letter 指标 | 每 5 分钟 |
| 清理 24 小时以上孤立版本 | 每天 02:30，Asia/Shanghai |
| 清理到期 tombstone | 每天 03:00，Asia/Shanghai |
| 清理到期 Graph Checkpoint | 每天 03:30，Asia/Shanghai |
| 清理超过 90 天的用户通知 | 每天 03:45，Asia/Shanghai |
| 校验活动 checksum/物化副本 | 每天 04:00，Asia/Shanghai |
| 检查 `backup_runs` 完成状态 | 每天 05:00，Asia/Shanghai |

支持管理员通过 CLI 手动触发维护任务；手动触发仍创建带幂等键的 maintenance run，不直接修改数据。

备份执行不由 Scheduler 发起：本地由开发者或宿主机 cron 调用 `scripts/backup.sh`，生产预留 host cron/systemd timer；Scheduler 只读取 `backup_runs`，发现当天未成功则告警。

Scheduler 先创建或复用 `memory_maintenance_runs`，它是调度幂等、batch cursor 和维护任务总状态的唯一真相；只有需要进入 `MemoryManagerGraph` 的 batch 才创建 `memory_operations`，并通过 `operation_id` 关联。operation 只表示该 Graph batch 的执行状态，不能反向替代 maintenance run 的整体状态。备份不是 Graph operation，Scheduler 只检查 `backup_runs` 完成状态。

### 14.4 Outbox Consumer

- 本期实现独立 Consumer 进程。
- 轮询间隔 1 秒。
- 每批 100 条。
- Lease 60 秒。
- 最大重试 10 次，指数退避上限 30 分钟。
- 使用 `memory_outbox_deliveries` 保证至少一次投递下不重复产生业务效果。

第一版 target：

| Event | Target |
|---|---|
| `memory.changed` | `summary_projection`、`internal_event_log` |
| `learner.updated` | `internal_event_log` |
| `memory.deleted` | `summary_projection`、`user_notification`、`internal_event_log` |
| `memory.restored` | `summary_projection`、`user_notification`、`internal_event_log` |
| `review_candidate.created` | `user_notification`、`internal_event_log` |
| `review_candidate.resolved` | `user_notification`、`internal_event_log` |
| `graph_state.changed` | `user_notification`、`internal_event_log` |
| `graph_state.explanation_available` | `user_notification`、`internal_event_log` |
| `account_memory.purge_requested` | `internal_event_log` |

Summary projection operation 的幂等键：

```text
summary-projection:{memory_id}:{source_version}:{node_id}
```

路由规则：

- `memory.changed`：对 payload 中每个候选节点创建 `apply_active_version` operation，`aggregate_version=after_version`。
- `memory.restored`：对每个候选节点创建 `apply_active_version` operation，`aggregate_version=after_version`。
- `memory.deleted`：对每个候选节点创建 `recompute_without_deleted_version` operation，`aggregate_version=deleted_version`；绝不能把删除版本当活动版本应用。
- `graph_projection_candidates` 为空时，`summary_projection` delivery 直接幂等成功，不创建空 operation。
- `learner` 事件通常没有图谱候选；总结记忆与图谱仍保持弱连接，不因通知/投递强制关联。

第一版不引入 Redis、RabbitMQ、WebSocket 或 SSE。前端通过 REST operation 轮询、通知接口和页面重新查询获取状态。

### 14.5 Docker Compose 服务

```text
postgres
memory-api
memory-worker
memory-scheduler
memory-outbox-consumer
```

前端开发服务可以单独运行，不需要域名或云服务器。固定图谱目录挂载规则为：本地使用项目根 `knowledge_graph/`，Docker 使用 `./knowledge_graph:/app/knowledge_graph:ro`，容器设置 `KNOWLEDGE_GRAPH_ROOT=/app/knowledge_graph`。新增可选 `frontend` Compose profile 用于静态构建预览，但不作为 Memory API 启动依赖；本地联调优先使用 Vite 开发服务器。

### 14.6 本地启动

后端和基础设施：

```bash
uv sync --extra dev --extra ocr
docker compose up -d postgres
uv run alembic upgrade head
uv run python -m backend.memory.cli sync-knowledge-graph --check
uv run python -m backend.memory.cli sync-knowledge-graph --apply
uv run uvicorn backend.app:app --host 127.0.0.1 --port 8000 --reload
uv run python -m backend.memory.worker.main
uv run python -m backend.memory.worker.scheduler
uv run python -m backend.memory.worker.outbox_consumer
```

前端开发和构建：

```bash
cd frontend
npm ci
npm run dev -- --host 127.0.0.1
# 另一个终端执行本地构建和预览
npm run build
npm run preview -- --host 127.0.0.1
```

本地访问地址固定为：

```text
前端开发： http://localhost:5173
Memory API： http://localhost:8000
Vite proxy： `/memory-api` → `MEMORY_DEV_API_TARGET`
```

前端本地固定使用 Vite proxy：`VITE_MEMORY_API_BASE_URL=/memory-api`，并由 proxy 将 `/memory-api` 转发到 `MEMORY_DEV_API_TARGET=http://localhost:8000`，注入 `X-Dev-User-Id: $MEMORY_DEV_USER_ID`。`MEMORY_DEV_USER_ID` 不使用 `VITE_` 前缀，生产构建不得包含该注入逻辑；本地 dev auth 只在 development 环境启用。

完整环境：

```bash
docker compose up --build
```

### 14.7 环境变量

```text
APP_ENV
DATABASE_URL
MEMORY_STORAGE_ROOT
KNOWLEDGE_GRAPH_ROOT=/app/knowledge_graph
MEMORY_DEV_API_TARGET=http://localhost:8000
MEMORY_DEV_USER_ID
MEMORY_API_HOST
MEMORY_API_PORT
MEMORY_WORKER_CONCURRENCY
MEMORY_OPERATION_LEASE_SECONDS
MEMORY_OUTBOX_POLL_SECONDS
MEMORY_SCHEDULER_TIMEZONE
MEMORY_ALLOWED_ORIGINS
BACKUP_ROOT
BACKUP_ENCRYPTION_METHOD=age-x25519-v1
BACKUP_AGE_RECIPIENT
BACKUP_AGE_IDENTITY_FILE
OPENAI_API_KEY
OPENAI_MEMORY_MODEL
OPENAI_REASONING_EFFORT
OPENAI_MEMORY_TIMEOUT_SECONDS
AUTH_ISSUER
AUTH_AUDIENCE
AUTH_JWKS_URL 或 AUTH_PUBLIC_KEY
SERVICE_TOKEN_AUDIENCE
BREAK_GLASS_ENABLED
LOG_LEVEL
LOG_HMAC_KEY
PRIVACY_HMAC_KEY
PRIVACY_HMAC_KEY_VERSION
CURSOR_HMAC_KEY
DATABASE_STATEMENT_TIMEOUT_MS=150000
DATABASE_LOCK_TIMEOUT_MS=10000
DEV_AUTH_ENABLED
DEV_AUTH_ALLOW_SCOPE_OVERRIDE
VITE_MEMORY_API_BASE_URL
```

本地宿主机 `BACKUP_ROOT` 默认 `.local/backups`，容器内映射为 `/backups`，`.local/` 必须加入 `.gitignore`。Secret 不写入仓库和普通环境样例值；生产通过 Docker secrets 或云服务器 Secret 管理注入。



备份执行和加密：

- 本地第一版由开发者手动执行或宿主机 cron 执行 `scripts/backup.sh`；未来生产由 host cron/systemd timer 触发一次性 Compose profile。Scheduler 不执行备份，只检查 `backup_runs` 并告警。
- PostgreSQL、Markdown 和 manifest 产物写入主机 `.local/backups`，容器内统一挂载为 `/backups`；`BACKUP_ROOT` 在容器内默认 `/backups`。
- 备份使用 `age` 加密，方法和密钥配置为：

```text
BACKUP_ENCRYPTION_METHOD=age-x25519-v1
BACKUP_AGE_RECIPIENT
BACKUP_AGE_IDENTITY_FILE
```

- 第一版不增加常驻 backup container；恢复脚本必须先在隔离目录恢复并校验 manifest，再重新应用 `account_deletion_manifest`。

### 14.8 健康检查

```text
GET /health/live      进程存活，不访问外部依赖
GET /health/ready     PostgreSQL、迁移版本、存储目录可读写、图谱注册表已加载
GET /health/startup   启动初始化完成
GET /metrics          由 `prometheus-client` 生成的 Prometheus 指标；不得含 user_id
```

---

## 15. Outbox 事件契约

统一事件信封：

```python
class MemoryDomainEvent(BaseModel):
    model_config = ConfigDict(extra="forbid")

    event_id: UUID
    event_type: str = Field(min_length=1, max_length=100)
    event_version: Literal[1] = 1
    user_id: UUID
    aggregate_type: str = Field(min_length=1, max_length=100)
    aggregate_id: str = Field(min_length=1, max_length=200)
    aggregate_version: int = Field(ge=0)
    occurred_at: datetime
    trace_id: str = Field(min_length=1, max_length=64)
    payload: dict


class MemoryChangedPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: Literal[1] = 1
    memory_id: str = Field(min_length=1, max_length=160)
    memory_type: Literal["mastery"]
    before_version: int | None = Field(default=None, ge=1)
    after_version: int = Field(ge=1)
    topic_key: str = Field(min_length=1, max_length=160)
    graph_projection_candidates: list[str] = Field(default_factory=list, max_length=20)


class MemoryDeletedPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: Literal[1] = 1
    memory_id: str = Field(min_length=1, max_length=160)
    memory_type: Literal["learner", "mastery"]
    deleted_version: int = Field(ge=1)
    restore_until: datetime
    graph_projection_candidates: list[str] = Field(default_factory=list, max_length=20)


class MemoryRestoredPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: Literal[1] = 1
    memory_id: str = Field(min_length=1, max_length=160)
    memory_type: Literal["learner", "mastery"]
    restored_from_version: int = Field(ge=1)
    after_version: int = Field(ge=1)
    graph_projection_candidates: list[str] = Field(default_factory=list, max_length=20)


class LearnerUpdatedPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: Literal[1] = 1
    memory_id: Literal["learner"] = "learner"
    before_version: int | None = Field(default=None, ge=1)
    after_version: int = Field(ge=1)
    changed_sections: list[Literal["preferences", "goals", "plans"]] = Field(max_length=3)


class ReviewCandidateCreatedPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: Literal[1] = 1
    candidate_id: UUID
    candidate_type: Literal["learner", "mastery", "topic_conflict", "version_conflict"]
    topic_key: str | None = Field(default=None, max_length=160)
    confidence: float = Field(ge=0, le=1)
    created_at: datetime


class ReviewCandidateResolvedPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: Literal[1] = 1
    candidate_id: UUID
    decision: Literal["accepted", "corrected", "rejected", "expired"]
    resolution_target: Literal["merge_existing", "create_new_topic"] | None = None
    target_memory_id: str | None = Field(default=None, max_length=160)
    result_operation_id: UUID | None = None
    resolved_at: datetime


class GraphStateChangedPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: Literal[1] = 1
    node_id: str = Field(pattern=r"^n\d{3,}$")
    before_status: Literal["learning", "proficient", "expert"] | None
    after_status: Literal["learning", "proficient", "expert"] | None
    source: Literal["user", "summary_memory", "system_recompute"]
    explanation_available: bool
    audit_id: UUID


class GraphStateExplanationAvailablePayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: Literal[1] = 1
    node_id: str = Field(pattern=r"^n\d{3,}$")
    audit_id: UUID
    summary: str = Field(min_length=1, max_length=500)
    changed_at: datetime


class AccountMemoryPurgeRequestedPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: Literal[1] = 1
    account_deletion_id: UUID
    user_hash: str = Field(min_length=64, max_length=64)
    requested_at: datetime
    purge_deadline: datetime
```

`event_type` 必须与对应 payload 类型做判别联合，不能只校验为任意 `dict`。事件 payload 不包含完整 Markdown、完整证据正文、Prompt 或模型 reasoning。触发规则固定为：

- learner 创建、修改、删除或恢复只发 `learner.updated` 或相应 deleted/restored 事件，不同时发 `memory.changed`。
- mastery 创建或修改只发 `memory.changed`；删除/恢复分别发 `memory.deleted`、`memory.restored`。
- `index.md` 重建不发 `learner.updated` 或 `memory.changed`。
- 同一 operation 修改多个 aggregate 时，每个 aggregate 各自产生一条带独立 `aggregate_id/aggregate_version` 的事件。

### 15.1 `memory.changed`

```json
{
  "schema_version": 1,
  "memory_id": "mastery:一致收敛",
  "memory_type": "mastery",
  "before_version": 3,
  "after_version": 4,
  "topic_key": "一致收敛",
  "graph_projection_candidates": ["n067"]
}
```

### 15.2 `memory.deleted`

```json
{
  "schema_version": 1,
  "memory_id": "mastery:一致收敛",
  "memory_type": "mastery",
  "deleted_version": 4,
  "restore_until": "2026-09-09T08:00:00Z",
  "graph_projection_candidates": ["n067"]
}
```

### 15.3 `memory.restored`

```json
{
  "schema_version": 1,
  "memory_id": "mastery:一致收敛",
  "memory_type": "mastery",
  "restored_from_version": 4,
  "after_version": 5,
  "graph_projection_candidates": ["n067"]
}
```

### 15.4 `learner.updated`

```json
{
  "schema_version": 1,
  "memory_id": "learner",
  "before_version": 6,
  "after_version": 7,
  "changed_sections": ["goals", "plans"]
}
```

### 15.5 `review_candidate.created`

```json
{
  "schema_version": 1,
  "candidate_id": "8c7b6b43-3e83-4e21-a6a8-e29b80d28b2f",
  "candidate_type": "mastery",
  "topic_key": "一致收敛",
  "confidence": 0.66,
  "created_at": "2026-08-10T08:00:00Z"
}
```

### 15.6 `review_candidate.resolved`

```json
{
  "schema_version": 1,
  "candidate_id": "8c7b6b43-3e83-4e21-a6a8-e29b80d28b2f",
  "decision": "accepted",
  "resolution_target": "merge_existing",
  "target_memory_id": "mastery:一致收敛",
  "result_operation_id": "55b96e8a-4430-4663-9ccc-9bd321716787",
  "resolved_at": "2026-08-10T08:10:00Z"
}
```

### 15.7 `graph_state.changed`

```json
{
  "schema_version": 1,
  "node_id": "n067",
  "before_status": "proficient",
  "after_status": "learning",
  "source": "summary_memory",
  "explanation_available": true,
  "audit_id": "0d3544c0-8a26-463a-a64e-f9eccf91ab21"
}
```

### 15.8 `graph_state.explanation_available`

```json
{
  "schema_version": 1,
  "node_id": "n067",
  "audit_id": "0d3544c0-8a26-463a-a64e-f9eccf91ab21",
  "summary": "系统根据新的学习证据将该节点从熟练调整为学习中。",
  "changed_at": "2026-08-10T08:15:00Z"
}
```

### 15.9 `account_memory.purge_requested`

```json
{
  "schema_version": 1,
  "account_deletion_id": "6a812aac-4ce8-48bd-b96b-a46d16c1976c",
  "user_hash": "0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef",
  "requested_at": "2026-08-10T08:00:00Z",
  "purge_deadline": "2026-08-11T08:00:00Z"
}
```

---

## 16. 固定知识图谱和用户 Overlay

### 16.1 固定图谱解析

权威来源：

- `knowledge_graph/数学知识科技树关系图.md`：节点 ID、节点标题、分组和有向边的权威来源。
- `knowledge_graph/教材目录.md`：教材层级和展示元数据的辅助来源，不负责生成节点 ID。

解析规则：

- Mermaid 中 `nNNN["标题"]` 解析为节点。
- Mermaid `subgraph` 解析为 `group_key`。
- `nAAA --> nBBB` 解析为 prerequisite 有向边，表示 `nAAA` 是 `nBBB` 的前置或强支撑。
- 第一版不解析 `-.->` 虚线边；如果源文件出现虚线边，同步命令必须失败并提示补充裁决。
- 重复 node ID、缺失节点、悬空边、非法 ID、同一 ID 标题变化必须使同步命令失败。
- 节点 ID 保持仓库中给定值，不重新编号。
- 同步命令计算两个源文件 checksum，并将节点、边和 checksum 在一个数据库事务中更新。
- 业务运行期只读数据库注册表；不在每次请求时重新解析 Markdown。

同步命令默认是 dry-run 后显式 apply：

```bash
uv run python -m backend.memory.cli sync-knowledge-graph --check
uv run python -m backend.memory.cli sync-knowledge-graph --apply
```

### 16.2 图谱状态转换

用户动作：

| 当前状态 | `mark_unfamiliar` | `mark_familiar` | `clear` |
|---|---|---|---|
| 无状态 | 学习中 | 熟练 | 无状态 |
| 学习中 | 学习中 | 熟练 | 无状态 |
| 熟练 | 学习中 | 熟练 | 无状态 |
| 精通 | 学习中 | 熟练 | 无状态 |

用户动作始终即时生效并写审计。`expert` 不能由用户命令产生。

总结记忆投影：

- 一条有实质内容的学习证据可以把无状态更新为学习中。
- 两条独立正向证据可以建议熟练。
- 精通必须满足第 9.3 节强证据条件。
- 一次普通错误不降级；两条独立强冲突证据或一条核心误解强证据可以降为学习中。
- 当系统覆盖用户最近设置时，必须写 `graph_state.explanation_available`，供前端显示调整依据。

### 16.3 图谱推导配置

以下配置必须进入 settings/policy，不得写死：

```text
GRAPH_EVIDENCE_WINDOW_DAYS=180
GRAPH_EXPERT_MIN_SPAN_DAYS=14
GRAPH_USER_ACTION_GRACE_HOURS=72
GRAPH_STRONG_CONFLICT_STRENGTH=0.85
GRAPH_POSITIVE_STRENGTH=0.70
GRAPH_PROJECTION_MAPPING_MIN=0.92
GRAPH_PROJECTION_MAPPING_MARGIN=0.15
```

证据聚合规则：

- 第一版不做连续时间衰减，只统计最近 `GRAPH_EVIDENCE_WINDOW_DAYS` 天内且仍在活动总结记忆中的证据。
- 同一 `(source_memory_id, source_version, evidence_ref, node_id)` 只计一次；同一来源版本重复投递不得重复增加证据。
- 独立证据是不同 `evidence_ref`，且来自不同事件/会话；同一聚合行为窗口内的重复记录只算一条。
- 强冲突证据是 `direction=conflict` 且 `strength >= GRAPH_STRONG_CONFLICT_STRENGTH`，或者能明确证明核心概念误解的单条证据。
- 正向证据只有 `strength >= GRAPH_POSITIVE_STRENGTH` 才进入 `proficient/expert` 计数。
- 仅统计仍在活动总结记忆中的证据。
- `learning` 证据只推动“无状态 → 学习中”。
- `positive` 需要至少 2 条独立证据才可推动到 `proficient`。
- `strong_positive` 可按高质量证据计入 `expert` 评估；用于 expert 的最早与最晚合格证据时间跨度必须至少为 `GRAPH_EXPERT_MIN_SPAN_DAYS`，不足时最多维持 `proficient`。
- `conflict` 达到阈值后可将 `proficient/expert` 降为 `learning`。
- `GRAPH_USER_ACTION_GRACE_HOURS` 只约束 `summary_memory/system_recompute` 的自动投影，不影响用户后续手动点击即时生效；grace 内不自动覆盖用户状态，超过后仍必须有强证据并写解释事件。



`status_source='activity'` 不属于第一版实际状态来源；activity 只能更新 `graph_user_node_activity.last_*_at/event_count`，不直接修改 Overlay status。状态变化一律来自用户图谱命令或 summary projection；保留 `activity` 作为未来扩展名不得写入当前 CHECK。

### 16.4 Summary 与图谱的弱连接

总结记忆更新图谱的必要条件：

1. 总结记忆版本已经成功提交并仍为活动版本。
2. 存在合法固定节点映射。
3. 映射来自上游明确 node ID、确定性别名，或通过高阈值候选校验。
4. 状态变化有内部 evidence refs。
5. `KnowledgeGraphStateService` 的确定性规则允许该转换。

任一条件不满足：总结记忆照常保存，图谱返回 `no_change`。

mastery commit 的 link 生命周期：活动版本提交成功后写入/更新 `memory_graph_links`；映射不再成立时将旧 link 置为 inactive；forget 将该 memory 的全部 link 置为 inactive；restore 使用恢复后的新版本重新计算映射并重新激活/创建 link。link 可以被维护任务完整重建，不是用户可见的独立记忆。

节点映射优先级：

1. 上游 `graph_node_hints` 且节点存在。
2. `topic_title` 与节点标题规范化后精确匹配。
3. `knowledge_graph_node_aliases` 中规范化 alias 精确匹配。
4. 节点标题和 alias 的原始 `pg_trgm` 分数达到阈值。



Alias 来源和维护：

- `derived` alias 由正式节点标题确定性生成（规范化空白、大小写、全半角、常用中英文标点及安全的数学记号变体），同步时可重建。
- `repository` alias 只有未来权威源文件明确提供时才导入。
- `manual_curated` 只通过受控 CLI 导入，不提供业务 API；同步时保留仍指向存在节点的 manual alias。
- alias 只用于后端映射和服务端搜索，前端 KnowledgeMap 只展示/搜索正式节点标题，不把 alias 作为用户可见标题。
`mapping_confidence` 由代码根据规范标题精确匹配、别名精确匹配和 `pg_trgm` 相似度计算，不由模型直接生成。模型只能给候选 node ID 列表；第一候选低于 `GRAPH_PROJECTION_MAPPING_MIN`、与第二候选差值低于 `GRAPH_PROJECTION_MAPPING_MARGIN`，或同名节点无法通过 group/章节上下文消歧时，不自动映射并以 `no_change` 结束。

### 16.5 推荐接口

推荐不调用模型，使用固定图谱、Overlay 和已确认弱连接进行确定性排序。`related_memory_ids` 从 `memory_graph_links` 读取，过滤 `active=true` 且 link 版本等于当前活动 mastery 版本；不得从 `source_memory_id` 单列字段猜测完整关联。

返回：

```python
class GraphRecommendation(BaseModel):
    node_id: str
    title: str
    status: Literal["learning", "proficient", "expert"] | None
    reason_codes: list[Literal[
        "CONTINUE_LEARNING",
        "PREREQUISITE_GAP",
        "NEXT_GRAPH_NODE",
        "REVIEW_AFTER_CONFLICT",
        "SUMMARY_MEMORY_SIGNAL",
        "STALE_PROFICIENCY",
    ]]
    prerequisite_node_ids: list[str]
    related_memory_ids: list[str]
    updated_at: datetime | None
```

排序读取 `graph_user_node_activity` 的 `last_viewed_at/last_bookmarked_at/last_check_in_at/event_count` 作为 exposure 和近期活跃信号；这些字段只影响排序，不直接改变状态。

排序优先级：

1. `learning` 且最近有学习活动。
2. 当前学习节点缺失的前置节点。
3. 已熟练节点的直接后继且无状态的节点。
4. 总结记忆中明确建议复习并可靠映射的节点。
5. 长期未复习的熟练节点。
6. `expert` 默认不推荐，除非存在新的强冲突证据。

使用不透明 cursor 分页，默认 20、最大 50。

---

## 17. Reader 数据边界

### 17.1 ConversationReader

本期只实现接口和测试适配器。未来正式适配器必须满足：

- 通过 `user_id + thread_id + checkpoint_id + message_ids` 读取。
- `message_ids` 是对话系统分配的稳定 UUID，不使用 LangGraph Checkpoint 数据库内部行号。
- Reader 自己校验这些消息属于该用户和线程。
- 返回用户可见消息、必要的助手上下文和允许列表中的工具结果。
- 过滤 system/developer prompt、认证信息、隐藏推理和无关工具结果。
- 助手消息只能作为上下文，不能单独证明用户掌握。
- 已删除消息返回明确 `SOURCE_DELETED`，不得从备份或 Checkpoint 绕过用户删除。

### 17.2 ActivityReader

本期只实现接口和测试适配器。未来正式适配器必须满足：

- 读取 forum post/reply、错题、练习结果和复习结果的稳定引用。
- 页面浏览、收藏和打卡在上游聚合后提交；Memory 模块不负责采集网站埋点。
- 默认聚合 key：`user_id + activity_type + topic_hint + 1h window`。
- `page_view/bookmark/check_in` 不调用 OpenAI、不创建总结记忆、不改变 Overlay status；在存在可靠 `graph_node_hints` 时只更新 `graph_user_node_activity` 的浏览/收藏/打卡时间与计数，作为推荐 exposure 信号。
- `forum_post/forum_reply/wrong_question_upload/exercise_attempt/review_result` 在存在内容和学习价值时才进入总结 Graph。

### 17.3 Source deletion 契约

第一版只定义删除事件和 Reader/Handler 边界，不增加可执行 `source_deleted` operation，也不实现跨证据全量重算；正式重算进入 v1.2。账号注销的 `purge_account_memory` 不延期。

```python
class SourceDeletedEvent(BaseModel):
    model_config = ConfigDict(extra="forbid")

    event_id: UUID
    source_system: Literal["conversation", "activity"]
    source_ref: str = Field(min_length=1, max_length=500)
    source_version: str | None = Field(default=None, max_length=200)
    deleted_at: datetime


class SourceDeletionHandler(Protocol):
    async def handle(
        self,
        *,
        user_id: UUID,
        event: SourceDeletedEvent,
    ) -> Literal["recorded", "duplicate", "not_found"]: ...
```

幂等键为包含 `user_id + source_system + source_ref + source_version + event_id` 的规范化 hash；`deleted_at` 作为事实字段保存但不替代 user 维度。第一版 Handler 只记录删除事实、阻止 Reader 再次返回该引用，并以 Fake Reader/Handler 完成契约测试；不得假装已经重新计算受影响总结或图谱状态。

### 17.4 SourceBundle 上限

```python
class SourceItem(BaseModel):
    source_ref: str
    role: Literal["user", "assistant", "tool", "activity"]
    content: str = Field(max_length=20_000)
    occurred_at: datetime
    metadata: dict = Field(default_factory=dict)


class SourceBundle(BaseModel):
    items: list[SourceItem] = Field(max_length=200)
    deleted_refs: list[str] = Field(default_factory=list)
    total_utf8_bytes: int = Field(ge=0, le=80_000)
```

`total_utf8_bytes` 不是调用方可任意声明的信任字段，而是 Reader/边界层对去重后的 `SourceItem.content` 逐项以 UTF-8 编码后计算；metadata 不计入总数。单项 metadata 最多 4096 bytes、最多 50 个 key。超过 80,000 bytes 必须返回 `SOURCE_TOO_LARGE`，不得静默裁剪或拆分单条证据。Graph 不在 Checkpoint 中保存无限正文。

---

## 18. 认证、权限和用户隔离

### 18.1 生产认证契约

- 网站后端完成用户登录认证。
- 网站后端或内部 Agent 使用短时签名 JWT 调用 Memory API。
- JWT 至少包含：`iss`、`aud=memory-api`、`sub`、`actor_type`、`scopes`、`iat`、`exp`、`jti`。
- `sub` 是外部身份主体字符串，不假定为 UUID；生产 JWT Adapter 必须通过 `(iss, sub)` 查询 `account_identity_mappings`，解析为内部 UUID `user_id`。
- token 最长有效期 5 分钟。
- Memory API 只信任配置中的 issuer/JWKS/public key。
- 浏览器不能自行设置 `actor_type` 或内部 scope。
- 若浏览器通过同域网站后端访问，网站后端充当 BFF，Memory API 不读取浏览器传来的 `user_id`。

第一版认证实现边界：

- 实现 `ProductionJwtAuthAdapter` 和 `DevelopmentAuthAdapter`，两者输出同一个内部 `AuthContext`。
- 真实 `AUTH_ISSUER`、`AUTH_AUDIENCE`、`AUTH_JWKS_URL` 或 `AUTH_PUBLIC_KEY` 由未来网站统一认证/部署环境提供；其缺失不阻塞本地开发。
- `DEV_AUTH_ENABLED=true` 且 `APP_ENV=development` 时允许测试身份，且只能从 loopback/Compose 内网进入。
- 普通本地前端只发送 `X-Dev-User-Id`；后端强制注入 `actor_type=user` 和预设用户 scopes：

```http
X-Dev-User-Id: <uuid>
```

- `X-Dev-Actor-Type` 和 `X-Dev-Scopes` 仅供受控测试；只有同时满足 `APP_ENV=development`、`DEV_AUTH_ENABLED=true`、`DEV_AUTH_ALLOW_SCOPE_OVERRIDE=true` 才接受，否则忽略并拒绝提权。
- 任何生产环境发现 `DEV_AUTH_ENABLED=true` 或 `DEV_AUTH_ALLOW_SCOPE_OVERRIDE=true` 都必须拒绝启动。



隐私摘要密钥分离：

```text
LOG_HMAC_KEY
PRIVACY_HMAC_KEY
PRIVACY_HMAC_KEY_VERSION
```

- `LOG_HMAC_KEY` 仅用于运行日志和短期运维指标中的 user hash。
- `PRIVACY_HMAC_KEY` 用于 `account_deletion_manifest`、evidence suppression、source deletion 摘要和长期隐私审计。
- 所有摘要使用 domain separation，例如 `user:v1`、`evidence-ref:v1`、`source-ref:v1`、`privacy-audit:v1`；相关表必须保存 hash key version。
- 第一版不支持在线 key rotation；轮换必须通过离线 rehash migration 完成，并在迁移期间保留旧版本验证能力。

### 18.2 Scope

```text
memory:read
memory:submit_evidence
memory:correct
memory:delete
memory:restore
memory:review
memory:cancel
memory:graph_state
memory:context
memory:maintenance
memory:break_glass
```

### 18.3 权限矩阵

| Actor | 读记忆 | 提交证据 | 纠正 | 删除/恢复 | 图谱标记 | 查看候选 |
|---|---:|---:|---:|---:|---:|---:|
| 用户 | 自己 | 仅明确记住命令 | 自己 | 自己 | 自己 | 自己 |
| conversation_agent | 当前授权用户的有限上下文 | 是 | 否 | 否 | 否 | 否 |
| activity_agent | 当前授权用户的有限上下文 | 是 | 否 | 否 | 否 | 否 |
| knowledge_graph_ui | 当前用户图谱读取 | 否 | 否 | 否 | 是 | 否 |
| summary_projection | 仅来源记忆版本 | 否 | 否 | 否 | 派生更新 | 否 |
| system | 任务所需范围 | 维护 | 否 | 账号清理 | 重算 | 否 |
| admin | 默认仅元数据 | 否 | 否 | 否 | 否 | 否 |
| break-glass admin | 指定用户、限时 | 否 | 受审计 | 受审计 | 受审计 | 指定用户 |

### 18.4 用户隔离

- 所有业务查询必须以认证上下文中的 `user_id` 为第一过滤条件。
- Repository 方法不得提供“可选 user_id”的普通业务接口。
- 内部 Agent token 必须带 delegated user 和 scope，不能凭服务身份跨用户搜索。
- PostgreSQL 连接账户不向前端或 Agent 暴露。
- 集成测试必须覆盖 IDOR：替换路径、query、payload 中的 user/memory/node 引用不能访问其他用户数据。

### 18.5 CORS 和限流

- 本地开发默认允许 `http://localhost:5173`。
- 生产默认关闭跨域，只允许 `MEMORY_ALLOWED_ORIGINS` 中的网站域名。
- 第一版使用进程内固定窗口限流：

```text
写操作：每用户每分钟 30 次
搜索：每用户每分钟 60 次
图谱标记：每用户每分钟 30 次
```

---

## 19. API 契约

所有写请求必须携带：

```http
Idempotency-Key: <1..200 chars>
```

### 19.1 学习证据

```http
POST /api/v1/memory/events
```

请求 payload 只允许 `ConversationEvidence` 或 `ActivityEvidence` 的公开字段；`user_id`、actor、priority 由服务端注入。

响应：`202 MemoryOperationResult`。

### 19.2 用户命令

```http
POST /api/v1/memory/commands/correct
POST /api/v1/memory/commands/forget
POST /api/v1/memory/commands/restore
PUT  /api/v1/memory/learner
POST /api/v1/memory/review-candidates/{candidate_id}/decision
```

P0 命令最多同步等待 2 秒，完成返回 200，否则返回 202。

### 19.3 Operation

```http
GET    /api/v1/memory/operations/{operation_id}
POST   /api/v1/memory/operations/{operation_id}/cancel
```

只能访问当前用户 operation。已经开始 commit 的 operation 不允许取消，返回 409。

### 19.4 总结记忆查询

```http
GET  /api/v1/memory/learner
GET  /api/v1/memory/index
GET  /api/v1/memory/mastery/{topic_key}
GET  /api/v1/memory/memories/{memory_id}
GET  /api/v1/memory/deleted
POST /api/v1/memory/search
GET  /api/v1/memory/review-candidates
```

查询默认不返回原始 Markdown 文件路径和历史正文。管理员普通 token 不允许调用正文接口。所有列表使用不透明 cursor；cursor 与筛选条件绑定，客户端不得解析。

```python
T = TypeVar("T")


class CursorPage(BaseModel, Generic[T]):
    items: list[T]
    next_cursor: str | None
    has_more: bool


class LearnerMemoryView(BaseModel):
    memory_type: Literal["learner"] = "learner"
    memory_id: Literal["learner"] = "learner"
    version: int = Field(ge=1)
    preferences: list[str]
    goals: list[str]
    plans: list[str]
    evidence_refs: list[str] = Field(max_length=100)
    confidence: float | None = Field(default=None, ge=0, le=1)
    updated_at: datetime


class MasteryMemoryView(BaseModel):
    memory_type: Literal["mastery"] = "mastery"
    memory_id: str = Field(pattern=r"^mastery:.+")
    topic_key: str = Field(min_length=1, max_length=160)
    topic_title: str = Field(min_length=1, max_length=240)
    version: int = Field(ge=1)
    overview: str
    understood: list[str]
    difficulties: list[str]
    review_advice: list[str]
    evidence_refs: list[str] = Field(max_length=100)
    confidence: float | None = Field(default=None, ge=0, le=1)
    updated_at: datetime


MemoryDocumentView = Annotated[
    LearnerMemoryView | MasteryMemoryView,
    Field(discriminator="memory_type"),
]


class MemoryIndexEntryView(BaseModel):
    memory_id: str
    memory_type: Literal["learner", "mastery"]
    topic_key: str | None
    title: str
    version: int
    updated_at: datetime


class MemoryIndexView(BaseModel):
    version: int = Field(ge=0)
    entries: list[MemoryIndexEntryView]
    updated_at: datetime | None
    stale: bool


class ReviewCandidateView(BaseModel):
    candidate_id: UUID
    candidate_type: Literal["learner", "mastery", "topic_conflict", "version_conflict"]
    base_memory_id: str | None
    base_version: int | None
    topic_key: str | None
    candidate_content: CandidateContentView
    evidence_refs: list[str] = Field(max_length=100)
    confidence: float = Field(ge=0, le=1)
    status: Literal["pending", "accepted", "corrected", "rejected", "expired"]
    resolution_target: Literal["merge_existing", "create_new_topic"] | None
    target_memory_id: str | None
    resolved_operation_id: UUID | None
    reviewed_at: datetime | None
    created_at: datetime
    updated_at: datetime


class DeletedMemoryItem(BaseModel):
    memory_id: str
    memory_type: Literal["learner", "mastery"]
    topic_key: str | None
    title: str
    deleted_version: int
    deleted_at: datetime
    restore_until: datetime


class MemorySearchRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    query: str = Field(min_length=1, max_length=500)
    topic_keys: list[str] = Field(default_factory=list, max_length=20)
    memory_types: list[Literal["learner", "mastery"]] = Field(default_factory=list)
    cursor: str | None = Field(default=None, max_length=1000)
    limit: int = Field(default=10, ge=1, le=50)
```

精确响应：

- `GET /memory/learner` → `LearnerMemoryView`；不存在时 404。
- `GET /memory/index` → `MemoryIndexView`，只包含当前未删除活动版本；未构建时返回 version 0、空 entries、updated_at=null、stale=true，dirty 时返回旧版本并 stale=true。
- `GET /memory/mastery/{topic_key}` → `MasteryMemoryView`。
- `GET /memory/memories/{memory_id}` → `LearnerMemoryView | MasteryMemoryView`。
- `GET /memory/review-candidates?status=pending&cursor=&limit=` → `CursorPage[ReviewCandidateView]`。
- `GET /memory/deleted?cursor=&limit=` → `CursorPage[DeletedMemoryItem]`，只返回仍在 30 天恢复窗口内的记录。
- `POST /memory/search` → `CursorPage[MemorySearchHit]`。

### 19.5 知识图谱

```http
GET    /api/v1/knowledge-graph/nodes
GET    /api/v1/knowledge-graph/me/nodes
GET    /api/v1/knowledge-graph/me/nodes/{node_id}
PUT    /api/v1/knowledge-graph/me/nodes/{node_id}/state
DELETE /api/v1/knowledge-graph/me/nodes/{node_id}/state
GET    /api/v1/knowledge-graph/recommendations
GET    /api/v1/knowledge-graph/me/nodes/{node_id}/explanation
```

```python
class GraphNodeView(BaseModel):
    node_id: str = Field(pattern=r"^n\d{3,}$")
    title: str
    group_key: str | None
    metadata: dict


class GraphEdgeView(BaseModel):
    from_node_id: str
    to_node_id: str
    relation_type: Literal["prerequisite"]


class KnowledgeGraphSnapshot(BaseModel):
    nodes: list[GraphNodeView]
    edges: list[GraphEdgeView]
    manifest_checksum: str = Field(min_length=64, max_length=64)
    synced_at: datetime


class GraphOverlayView(BaseModel):
    node_id: str
    status: Literal["learning", "proficient", "expert"] | None
    version: int | None
    status_source: Literal["user", "summary_memory", "system_recompute"] | None
    updated_at: datetime | None


class GraphNodeDetailView(BaseModel):
    node: GraphNodeView
    overlay: GraphOverlayView
    prerequisite_node_ids: list[str]
    successor_node_ids: list[str]
```

- `GET /knowledge-graph/nodes` 一次返回全部固定节点、分组和边，不分页、不返回用户状态。
- `GET /knowledge-graph/me/nodes` 返回当前用户全部 `GraphOverlayView`；无行节点由前端解释为无状态。
- `GET /knowledge-graph/me/nodes/{node_id}` 返回 `GraphNodeDetailView`。
- `GET /knowledge-graph/recommendations?cursor=&limit=` 返回 `CursorPage[GraphRecommendation]`。

图谱 Overlay 写入接口统一返回现有 `MemoryOperationResult`，不返回另一套 `GraphStateCommandAccepted`；快速路径在 2 秒内完成返回 200，未完成返回 202。`graph_state_changes` 使用第 7.1 节的 `GraphStateChangeView`。

- `PUT .../state` 的 JSON 使用 `GraphStatePutRequest`，只携带 `action` 和当前 Overlay 的 `expected_version`（无状态首次标记可省略）；Gateway 从 URL 注入 `node_id`，并构造内部 `GraphStateCommand`。客户端传入 `kind` 或 `node_id` 等额外字段返回 422 `REQUEST_EXTRA_FIELD`。
- `DELETE .../state?expected_version=<n>` 使用 query 参数承载版本，不使用 DELETE JSON body，也不使用 `If-Match`。当前存在 Overlay 时缺少 `expected_version` 返回 422 `GRAPH_STATE_VERSION_REQUIRED`；当前无 Overlay 时 clear 可省略版本并幂等返回 `no_change`。
- 图谱写入需要 `memory:graph_state`；图谱读取和 explanation/recommendations 需要 `memory:read`；operation cancel 需要 `memory:cancel`。通知已读接口返回 `MemoryNotification`。



图谱状态写请求的公开响应统一为：

```python
# PUT/DELETE 成功或排队均返回现有 MemoryOperationResult。
# 需要当前状态时由 GET /me/nodes/{node_id} 读取 GraphNodeDetailView。
```
`PUT state` 的 action 只允许 `mark_unfamiliar` 或 `mark_familiar`；`DELETE state` 表示 clear。任何 `expert` 请求均返回 422：

```json
{
  "error": {
    "code": "GRAPH_STATUS_NOT_USER_SETTABLE",
    "message": "精通状态由长期学习表现自动评估，不能手动设置。",
    "retryable": false,
    "trace_id": "32-hex"
  }
}
```

```python
class GraphStateExplanation(BaseModel):
    node_id: str
    current_status: Literal["learning", "proficient", "expert"] | None
    explanation_available: bool
    summary: str | None
    reason_codes: list[str]
    source_type: Literal["user", "summary_memory", "system_recompute"] | None
    source_memory_id: str | None
    source_memory_version: int | None
    evidence_refs: list[str] = Field(max_length=10)
    changed_at: datetime | None
```

解释不单独保存正文表；第一版从最近一次 `graph_state_audit` 和关联 memory metadata 生成简短依据，只返回受控 reason codes、时间、来源类型和最多 10 个 evidence refs，不返回总结记忆全文或原始证据正文。

### 19.6 通知

```http
GET  /api/v1/memory/notifications?cursor=&limit=&unread_only=true
POST /api/v1/memory/notifications/{notification_id}/read
```

```python
class MemoryNotification(BaseModel):
    notification_id: UUID
    event_type: str
    title: str
    body: str
    aggregate_type: str
    aggregate_id: str
    read_at: datetime | None
    created_at: datetime


class MemoryNotificationPage(CursorPage[MemoryNotification]):
    unread_count: int = Field(ge=0)
```

`GET` 默认 `limit=20`、最大 100，支持 `unread_only`；返回当前未读总数。`POST /read` 幂等：已读通知再次调用仍返回 200 和同一 `read_at`。用户通知默认保留 90 天，由 Scheduler 清理。

### 19.7 内部账号删除接口

```http
POST /api/v1/internal/account-memory/purge
```

仅允许账户服务使用非浏览器服务 JWT 和 `memory:maintenance` scope 调用，不接受浏览器 token：

```python
class AccountMemoryPurgeRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    account_deletion_id: UUID
    issuer: str = Field(min_length=1, max_length=300)
    external_subject: str = Field(min_length=1, max_length=300)
    requested_at: datetime
    reason: str = Field(min_length=1, max_length=500)
```

接口通过 `account_identity_mappings` 解析目标内部 UUID，创建 `purge_account_memory` operation、`account_deletion_manifest` 和 `account_memory.purge_requested` Outbox；映射不存在返回 404，不能由调用方直接注入内部 `user_id`。

### 19.8 Agent 内部接口

其他 Agent 优先使用 `MemoryClient`，不依赖 HTTP 路由细节：

```python
class MemoryClient:
    async def submit_conversation_evidence(...) -> MemoryOperationResult: ...
    async def submit_activity_evidence(...) -> MemoryOperationResult: ...
    async def search_summary(...) -> list[MemorySearchHit]: ...
    async def build_learning_context(...) -> LearningContext: ...
    async def get_graph_recommendations(...) -> list[GraphRecommendation]: ...
```

第一版 `MemoryClient` 使用带服务 JWT 的 HTTP 实现；不提供 MCP。

---



### 19.9 Cursor 签名契约

所有列表 cursor 由服务端使用独立 `CURSOR_HMAC_KEY` 签名，客户端不得解析或修改。payload 至少包含：

```text
cursor_version
route
principal_hash
normalized_filters
sort_key
expires_at
```

默认有效期 15 分钟。路由、用户、筛选条件或排序不一致返回 `CURSOR_INVALID`；超过 `expires_at` 返回 `CURSOR_EXPIRED`。`principal_hash` 使用隐私隔离之外的短期 cursor domain，不得把原始 user_id 放进 token。

## 20. 前端真实接入

### 20.1 Profile：“AI 记住了我什么”

必须实现：

- learner 和 mastery 列表加载。
- 查看结构化记忆内容和更新时间。
- 纠正记忆，携带 `expected_version`。
- 删除、30 天内恢复。
- 查看 `needs_review` 候选。
- 接受、修改或拒绝候选。
- 对 409 冲突刷新数据后提示用户重新确认。
- 已删除可恢复列表从 `GET /memory/deleted` 获取。

不得展示：

- 模型隐藏 reasoning。
- 内部 Prompt。
- 文件系统路径。
- break-glass、lease、graph thread/checkpoint 字段。

Profile 页面只有“AI 记住了我什么”区域接真实 API。连续学习天数、累计提问、已掌握知识点、错题收藏和用户资料等未接真实数据的区域：development 环境可以继续 Mock，但必须显著标注“展示数据”；production 构建默认隐藏，不能以 Mock 冒充真实统计。

### 20.2 KnowledgeMap

现有 Mock 星图必须替换为后端固定图谱。

规则：

- 使用 `GET /knowledge-graph/nodes` 加载真实节点和边。
- 使用 `GET /knowledge-graph/me/nodes` 加载用户 Overlay。
- 前端使用 `@dagrejs/dagre` 的确定性层次布局根据边计算坐标；后端不保存 `x/y`。
- 首次加载一次获取全部固定节点和边，以及当前用户 Overlay；节点 API 不按 group 分页。
- 不再展示百分比掌握度。
- 前端通过 group/domain、搜索和状态筛选控制可见节点与渲染范围，但筛选不改变已加载的完整图谱数据。

状态展示：

```text
null         → 无状态
learning     → 学习中
proficient   → 熟练
expert       → 精通
```

交互：

- “不熟悉”提交 `mark_unfamiliar`。
- “熟悉”提交 `mark_familiar`。
- “清除”调用 DELETE state。
- “精通”不可直接设置。
- 用户尝试点击“精通”时显示：

> 精通状态由你长期的学习表现自动评估，不能手动设置。你可以继续学习、练习和讲解相关知识，系统会根据积累的学习证据更新状态。

当系统根据新证据调整用户曾手动设置的状态时，显示通知并允许查看简短依据。

### 20.3 Operation 轮询

- 初始间隔 500ms，随后 1s、2s，最大 5s。
- terminal 状态停止轮询。
- 页面隐藏后降低到 15s；恢复可见立即查询。
- 最长前端等待 2 分钟，超时后提示任务仍在后台处理，不取消服务器任务。
- 对用户图谱点击使用乐观 UI；失败时回滚并显示错误。

### 20.4 前端配置

前端可以在未申请域名时完成全部页面和 API 联调。新增：

```text
VITE_MEMORY_API_BASE_URL=/memory-api
MEMORY_DEV_API_TARGET=http://localhost:8000
MEMORY_DEV_USER_ID=<本地测试 UUID，仅由 Vite proxy 注入>
```

开发环境由本地代理或显式开发配置注入 `X-Dev-User-Id`；前端代码不得硬编码用户 ID，也不得发送 `X-Dev-Actor-Type` 或 `X-Dev-Scopes`。production 构建不得包含 Dev Auth Header 注入逻辑。

---

## 21. 安全、隐私和数据治理

### 21.1 日志

- `user_id` 在日志中使用 `HMAC-SHA256(LOG_HMAC_KEY, user_id)`，不记录原值。
- 允许记录 operation_id、trace_id、actor、operation_type、状态、耗时、token 数和错误码。
- 禁止记录完整 Prompt、原始消息、完整模型输出、Markdown 正文、JWT、Cookie 和 API Key。
- 错误日志只记录受控摘要和字段路径。
- JSON 日志输出到 stdout/stderr；第一版不强制部署集中日志栈，但保持可被采集。
- 应用日志默认保留 30 天；审计元数据保留到账号删除。

### 21.2 数据最小化

- 长期记忆只保存数学学习相关内容。
- 与数学学习无关的个人信息进入确定性过滤器并丢弃。
- 敏感信息即使用户要求“记住”，也不写入 Markdown。
- 原始对话和用户动态仍由各自系统治理，Memory 只保留引用和学习摘要。

### 21.3 账号删除

网站统一认证/账户服务发送经过认证的删除命令：

1. 阻止新 operation。
2. 取消未开始 operation。
3. 等待或终止运行中的用户任务。
4. 删除/隔离中的 Markdown、活动版本和历史版本。
5. 删除 index、commit payload、review candidate、图谱状态和审计正文引用。
6. 删除 LangGraph Checkpoint。
7. 删除未投递用户 Outbox；已投递 Consumer 接收 purge event。
8. 最多 24 小时内完成并记录不可还原的完成证明。

### 21.4 备份和恢复

第一版目标：

```text
RPO：24 小时
RTO：4 小时
备份保留：30 天
```

- PostgreSQL 每日逻辑备份，备份文件加密。
- Markdown 持久卷每日快照或文件级增量备份。
- 本地默认输出到 `BACKUP_ROOT=.local/backups`，该目录加入 `.gitignore`。
- 数据库和 Markdown 备份使用同一 `backup_runs.batch_id`，分别写入 `postgres_checksum/markdown_checksum`，并保存可校验的 manifest；任一部分失败则整个 batch 为 `failed`。
- manifest 至少包含 `schema_version`、`batch_id`、创建时间、应用 migration revision、图谱 manifest checksum、相对 artifact 路径、加密方式、两个 artifact checksum、账号删除 manifest watermark 和 manifest 自身 checksum；artifact 路径不得逃逸 `BACKUP_ROOT`。
- `scripts/backup.sh` 负责创建/更新 `backup_runs`、以临时文件写入并原子改名；`scripts/restore.sh` 在写入目标前校验 manifest、artifact checksum、加密元数据和目标环境，默认只允许恢复到空目标，覆盖现有环境必须显式 `--force`。
- 提供 `scripts/backup.sh`、`scripts/restore.sh` 和恢复验证命令：

```bash
uv run python -m backend.memory.cli verify-backup-restore --batch-id <uuid>
```

- 每周恢复验证通过 CLI 和运维手册执行，并更新 `backup_runs.restore_verification_status/restore_verified_at/restore_verification_error`；第一版不新增常驻 backup 容器，Scheduler 只检查缺失、失败或过期的 `backup_runs` 状态并告警，不负责实际发起云端异地备份。
- 支持按备份批次完整恢复；支持通过 user_id 导出数据库行和对应 Markdown 进行单用户恢复演练。
- 第一版不承诺任意时间点恢复；需要更低 RPO 时再启用 WAL 归档/PITR。
- 账号删除后的备份副本不主动逐包改写，随 30 天周期淘汰；恢复旧备份时必须重新应用 `account_deletion_manifest`。

---

## 22. 可观测性

必须提供以下指标：

```text
memory_operations_total{type,status}
memory_operation_duration_seconds{type}
memory_operation_queue_depth{priority}
memory_operation_oldest_queued_seconds
memory_operation_retry_total{error_code}
memory_dead_letter_total{type}
memory_review_candidate_total{status}
memory_llm_calls_total{model,status,schema}
memory_llm_tokens_total{model,direction}
memory_llm_latency_seconds{model}
memory_commit_total{action,type}
memory_version_conflict_total
memory_outbox_queue_depth
memory_outbox_oldest_pending_seconds
memory_graph_state_changes_total{from,to,source}
memory_storage_checksum_failure_total
```

告警基线：

- P0/P1 最老 queued 超过 30 秒。
- P2/P3 最老 queued 超过 5 分钟。
- dead letter 在 5 分钟窗口内大于 0。
- Outbox 最老 pending 超过 5 分钟。
- checksum failure 大于 0。
- 数据库或存储 readiness 连续失败 3 次。

第一版将告警输出为结构化告警日志和健康状态；接入具体通知平台不阻塞交付。

---

## 23. 测试规格

### 23.1 单元测试

覆盖：

- topic_key Unicode 规范化、冲突和路径穿越。
- actor/operation/payload 判别联合。
- 权限矩阵和 scope。
- 图谱四状态转换及用户不能设置 expert。
- 长期价值和证据阈值。
- Prompt 输入裁剪和敏感信息过滤。
- retry 分类、退避和 jitter 边界。
- Markdown 渲染、解析和 checksum。
- Search 排序和中文 trigram 规范化。
- 幂等 payload canonical hash。
- 候选匹配键和删除抑制键。

### 23.2 Graph 测试

每个节点用 fake Runtime Context 测试：

- 无长期价值时不调用 commit。
- 低置信候选进入 review，不写活动 Markdown。
- OpenAI 输出非法路径/ID 被拒绝。
- 总结记忆没有图谱节点时照常提交。
- 总结提交后通过 Outbox 创建 projection，而不是直接改图谱。
- 用户命令分支不调用 OpenAI。
- `expert` 只有满足强证据数量、质量和 `GRAPH_EXPERT_MIN_SPAN_DAYS` 时间跨度策略才能生成。
- 用户动作 grace 只阻止自动投影，不阻止新的用户点击。
- `memory.deleted` projection 排除删除版本并从剩余活动证据重算，绝不按普通活动版本应用。
- Checkpoint 恢复后不重复 mutation。
- cancel 在 commit 前生效，在 commit 中返回 409。

### 23.3 PostgreSQL/Markdown 集成测试

必须使用真实 PostgreSQL 容器和临时文件系统：

- 多文档活动版本原子切换。
- 数据库提交前宕机只留下孤立版本。
- 数据库提交后物化失败，读取仍返回正确版本并可修复 current。
- 同一 mutation 重放只返回原 commit。
- 同一幂等键不同 payload 返回冲突。
- user advisory lock 和 expected_version 冲突。
- Outbox 与 commit 同事务。
- Outbox delivery 多目标部分失败可恢复。
- 删除时活动指针置空、隔离、恢复为递增新版本和 30 天清理。
- 候选 accept/correct/reject 保存 `resolution_target/target_memory_id/resolved_operation_id` 并可审计重放。
- 图谱 clear 删除活动行并保留审计。
- 90 天通知清理不影响 Outbox delivery 和最小审计记录。
- backup manifest/checksum 校验、失败批次记录和恢复验证状态更新。

### 23.4 失败恢复测试

注入故障点：

```text
写临时 Markdown 前
写不可变版本后、事务前
事务中
事务提交后、current 物化前
Outbox 消费前
Outbox 消费后、标记 published 前
Graph projection 提交前后
Worker 心跳停止
Gateway 快速路径与 Worker 同时领取
```

验收：

- 不丢任务。
- 不重复提交。
- 不产生半个活动版本。
- 至少一次执行不造成重复业务效果。

### 23.5 API 和安全测试

- OpenAPI snapshot/contract test。
- 401、403、404、409、422、429、503。
- 跨用户 IDOR。
- `user_id/actor_type/priority/graph_thread_id` 外部注入被拒绝。
- 生产环境不能启用 dev auth。
- admin 默认不能读正文。
- break-glass 限时、限用户和审计。
- expert 前后端禁止手动设置。
- CORS 只允许配置 origin。

### 23.6 前端测试

- Profile 加载、纠正、删除、恢复和候选审核。
- KnowledgeMap 四状态显示。
- 点击熟悉/不熟悉/清除。
- 点击精通时出现指定提示且不发非法请求。
- 乐观更新失败回滚。
- operation 轮询停止和超时提示。
- 后端图谱节点和边驱动布局。

### 23.7 本地 Git 和本地 CI

本期版本控制和质量门禁只在本地完成：

```bash
# 施工开始时只执行一次
git init
git add .gitignore
git commit -m "chore: initialize local repository"

# 每个阶段完成后提交本地检查点，不配置 remote
git add .
git commit -m "feat: complete local memory foundation"
```

项目根目录不得配置 Git remote；不执行 `git push`，不创建 GitHub Actions workflow，也不依赖远端 CI。实现阶段新增 `scripts/ci-local.sh` 作为本地统一入口，至少执行：

```text
backend-lint        Ruff + mypy
backend-unit        pytest unit/graph + 现有 OCR 测试
backend-integration 本地 PostgreSQL 容器 + integration/failure tests
frontend            npm ci + lint/test/build
contracts           OpenAPI snapshot
container-build     docker compose build
```

本地 CI 通过后，才允许进入“本地发布候选”验收；云端部署属于后续阶段，不纳入本期完成条件。

---



本地 Git 边界：

```gitignore
.env
.env.*
!.env.example

.venv/
__pycache__/
*.pyc
.ruff_cache/
.pytest_cache/
.mypy_cache/
.coverage
htmlcov/

.local/
tmp/

frontend/node_modules/
frontend/dist/

ocr_text/
math_text/
clean_text/

experiments/**/raw/
experiments/**/input/
experiments/**/output/
experiments/**/outputs/

.DS_Store
```

不把大体量教材、PDF、OCR 产物和实验原始输入/输出加入本地代码 Git；保留 `knowledge_graph/`、代码、测试、规格文档和小型实验报告。Git commit 不在规格中写死 `user.name/user.email`：优先使用开发者现有全局配置，第一次 commit 若未配置再由实现者询问需求方。

## 24. 实施顺序

1. 初始化本地 Git 和 `.gitignore`，不配置 remote；建立本地 CI 命令入口。
2. 创建 Python 工程基线、settings、Docker Compose 和认证上下文。
3. 建立 Pydantic 契约和错误模型。
4. 编写 Alembic DDL、Repository 和知识图谱同步器。
5. 实现 Markdown Store、版本协议和原子提交。
6. 实现 `MemoryService` 和 `KnowledgeGraphStateService`。
7. 实现 Reader 接口和测试适配器。
8. 实现 OpenAI Structured Outputs Schema、Prompt 和评测样例。
9. 实现 Graph State、父图和各分支。
10. 实现 Worker、Scheduler、Checkpoint 和 Outbox Consumer。
11. 实现 Gateway API、认证适配器、通知接口和 `MemoryClient`。
12. 实现检索、`LearningContextService` 和推荐接口。
13. 接入 Profile 与 KnowledgeMap。
14. 完成失败注入、隐私、安全和端到端验收。
15. 编写启动、备份、恢复和故障处理文档。
16. 执行本地完整验收：后端测试、前端测试、前端生产构建、Docker Compose、API 契约和浏览器联调。
17. 本地验收通过后，另行编写云服务器部署和域名/HTTPS/正式认证配置，不在本期提前实施。

---

## 25. 最终裁决摘要

```text
后端：Python 3.13 + FastAPI + Pydantic v2
数据库：PostgreSQL 17 + SQLAlchemy async + Alembic + psycopg 3
Graph：LangGraph 1.2.1 + PostgreSQL Checkpointer
模型：OpenAI SDK >=2.38,<3（uv.lock 锁定）+ gpt-5.6-luna + Responses API + Structured Outputs
部署：当前本地 Docker Compose；本地验收后再部署单台云服务器 + Docker Compose
Markdown：不可变版本 + 数据库活动指针 + current 物化副本
检索：pg_trgm，不使用向量库
任务：PostgreSQL 持久队列 + Lease + 至少一次执行
提交：用户锁 + expected_version + mutation_id 幂等 + Outbox
图谱：固定节点/边只读，Overlay 显示无状态/学习中/熟练/精通
认证：网站统一认证，Memory API 不实现登录；本地使用 dev auth
删除：30 天可恢复，账号删除 24 小时内物理清理
管理员：默认不可读正文，break-glass 例外且完整审计
外部 Agent：本期只定义 ConversationReader/ActivityReader 接口
前端：接入 Profile、KnowledgeMap、通知和 operation 轮询
版本控制：本地 Git，不配置 remote
CI：本地 `scripts/ci-local.sh` 和等价命令，不创建 GitHub Actions workflow
```

本文件作为 v1.1 正式施工基线；后续任何修改必须以新版本规格明确记录。

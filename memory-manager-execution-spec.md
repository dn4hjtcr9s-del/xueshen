# MemoryManagerGraph 第一版执行规格

> **文档状态：待需求方审阅**  
> **版本：1.0-draft**  
> **更新时间：2026-08-10**  
> **上位架构：** [`memorymangergraph.md`](./memorymangergraph.md)  
> **缺口审计：** [`memory-manager-execution-spec-gap-analysis.md`](./memory-manager-execution-spec-gap-analysis.md)

本文是 `MemoryManagerGraph` 第一版的可施工规格。架构原则以 `memorymangergraph.md` 为准；当原设计中的示例字段与本文冲突时，以本文已经裁决的执行规格为准。

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
- FastAPI、数据库迁移、PostgreSQL 和认证实现。
- 对话 Agent、用户动态 Agent 及其业务数据库。
- Memory Worker、Scheduler、Outbox Consumer。
- CI 配置。

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
9. Profile 与 KnowledgeMap 页面的最小真实 API 接入。
10. Docker Compose 单机开发/生产基线。
11. 单元、Graph、持久化、失败恢复、API 契约和前端必要测试。
12. GitHub Actions CI、开发文档和运维文档。

本期不实现：

- 对话 Agent。
- 用户动态 Agent、论坛后端、错题上传服务和行为采集系统。
- LangGraph Agent Server。
- Redis、RabbitMQ、Kafka、向量数据库。
- 多服务器部署、对象存储和分布式文件锁。
- Memory API 自建注册、登录或密码管理。

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
- CI 中所有检查和测试通过。

---

## 2. 已裁决的产品与架构规则

### 2.1 部署和认证

- 第一版生产形态：单台云服务器 + Docker Compose。
- API、Worker、Scheduler、Outbox Consumer 和 PostgreSQL 分进程/容器部署。
- Markdown 位于只授予 Memory 服务写权限的持久化卷。
- Memory API 不实现登录；生产身份来自网站统一认证。
- 浏览器不得持有内部服务凭证，也不得直接提交可信 `user_id`。
- 本地开发可以启用仅限 development 环境的测试身份适配器。

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

对外只暴露一个字段：

```text
null / 无状态
learning / 学习中
proficient / 熟练
expert / 精通
```

规则：

- “无状态”表示没有足够信息，不在数据库中强制写一行默认记录。
- 用户点击“不熟悉”立即设置为 `learning`。
- 用户点击“熟悉”立即设置为 `proficient`。
- 用户清除状态后恢复为无状态。
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

---

## 3. 技术栈、版本和工程基线

### 3.1 Python 和后端依赖

`pyproject.toml` 使用兼容范围，`uv.lock` 锁定精确解析版本。首次实现基线如下：

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
| OpenAI Python SDK | `==2.38.0` |
| pytest | `>=9.0,<10.0` |
| pytest-asyncio | `>=1.1,<2.0` |
| Ruff | `>=0.12,<1.0` |
| mypy | `>=1.17,<2.0` |

选择 `psycopg` 同时服务 SQLAlchemy async 与 LangGraph PostgreSQL Checkpointer，第一版不再额外引入 `asyncpg`。

### 3.2 基础设施

| 组件 | 第一版选择 |
|---|---|
| PostgreSQL | 17，Docker 镜像固定到 major：`postgres:17` |
| 扩展 | `pg_trgm`、`pgcrypto` |
| Markdown | 单机持久化卷 |
| 本地环境 | Docker Compose |
| CI | GitHub Actions |
| 日志 | JSON stdout/stderr |
| 时区 | 数据库存 UTC；产品日程使用 `Asia/Shanghai` |

### 3.3 推荐模块布局

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
    │   └── reviews.py
    ├── contracts/
    │   ├── common.py
    │   ├── operations.py
    │   ├── evidence.py
    │   ├── commands.py
    │   ├── results.py
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
    │   └── projection_service.py
    ├── persistence/
    │   ├── database.py
    │   ├── operations.py
    │   ├── documents.py
    │   ├── commits.py
    │   ├── graph_states.py
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

`expected_version` 不是模型决策字段：用户命令可携带它作为并发令牌；总结 Graph 在读取目标后由应用代码把当前版本注入内部计划。模型只提出“操作意图”和受限 patch。

### 4.2 `memory_id`

- `learner.md` 固定为 `learner`。
- `index.md` 固定为 `index`，但属于派生文档。
- 掌握档案固定为 `mastery:{topic_key}`。
- `memory_id` 可以通过 API 对用户显示，用于纠正、删除和恢复。
- 删除后逻辑 ID不分配给其他主题。
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

示例：

```text
Uniform Convergence        → uniform-convergence
一致收敛                   → 一致收敛
洛必达法则（L'Hôpital）    → 洛必达法则-l-hôpital
```

模型只能输出 `topic_title`；最终 `topic_key` 必须由代码生成。

### 4.4 时间

- API 时间均为带时区 RFC 3339。
- 数据库使用 `timestamptz`，统一存储 UTC。
- `occurred_at` 表示业务发生时间；`created_at` 表示系统接收时间。
- 未提供业务时间的用户命令，由 Gateway 使用当前时间。
- 允许业务时间最多比当前时间晚 5 分钟；超过则拒绝。

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
from typing import Annotated, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field


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
- `payload` 使用 `operation_type` 判别联合；未知字段一律拒绝。

---

## 6. Payload 完整定义

### 6.1 对话和行为 Reader 引用

```python
class ConversationEvidence(BaseModel):
    """引用外部对话内容；Memory 模块不保存重复的完整对话。"""

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
    """引用外部用户动态；正文由 ActivityReader 按授权读取。"""

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

正式 Reader 由未来的对话系统和用户动态系统提供适配器。本期提供可注入的测试 Reader；Memory 模块不得读取外部系统的内部数据库表。

### 6.2 用户记忆命令

```python
class LearnerReplacement(BaseModel):
    """用户确认后的 learner.md 完整替换内容；不允许传入 Markdown。"""

    model_config = ConfigDict(extra="forbid")

    replacement_type: Literal["learner"] = "learner"
    preferences: list[str] = Field(default_factory=list, max_length=50)
    goals: list[str] = Field(default_factory=list, max_length=50)
    plans: list[str] = Field(default_factory=list, max_length=50)


class MasteryReplacement(BaseModel):
    """用户确认后的 mastery 文档完整替换内容；不允许传入 Markdown。"""

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

`replacement` 和 `corrected_content` 使用 `MemoryReplacement` 判别联合；服务端还必须校验其类型与目标 `memory_id`/候选类型一致，不能直接执行任意 Markdown、JSON Patch 或文件路径。

### 6.3 候选审核命令

```python
class ReviewCandidateCommand(BaseModel):
    model_config = ConfigDict(extra="forbid")

    kind: Literal["review_candidate"] = "review_candidate"
    candidate_id: UUID
    decision: Literal["accept", "correct", "reject"]
    corrected_content: MemoryReplacement | None = None
    reason: str | None = Field(default=None, max_length=500)
```

- `accept` 使用候选计划创建新 mutation。
- `correct` 必须提供受限结构化内容。
- `reject` 创建 30 天候选 tombstone。

### 6.4 图谱命令和派生更新

```python
class GraphStateCommand(BaseModel):
    model_config = ConfigDict(extra="forbid")

    kind: Literal["set_graph_state"] = "set_graph_state"
    node_id: str = Field(pattern=r"^n\d{3}$")
    action: Literal["mark_unfamiliar", "mark_familiar", "clear"]
    expected_version: int | None = Field(default=None, ge=1)


class GraphProjectionEvidence(BaseModel):
    evidence_ref: str
    direction: Literal["learning", "positive", "strong_positive", "conflict"]
    strength: float = Field(ge=0, le=1)
    occurred_at: datetime


class ProjectSummaryToGraphCommand(BaseModel):
    model_config = ConfigDict(extra="forbid")

    kind: Literal["project_summary_to_graph"] = "project_summary_to_graph"
    source_memory_id: str
    source_version: int = Field(ge=1)
    node_id: str = Field(pattern=r"^n\d{3}$")
    mapping_method: Literal["explicit_hint", "exact_alias", "model_candidate"]
    mapping_confidence: float = Field(ge=0, le=1)
    evidence: list[GraphProjectionEvidence] = Field(min_length=1, max_length=50)
```

`ProjectSummaryToGraphCommand` 只能由 `summary_projection` actor 创建，外部 API 不接受该 payload。

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

`operation_type` 必须与 `payload.kind` 做一一对应校验，不能由调用方分别声明两套互相矛盾的值：

| `payload.kind` | `operation_type` | `input_kind` |
|---|---|---|
| `conversation_evidence` | `conversation_evidence` | `evidence` |
| `activity_evidence` | `activity_evidence` | `evidence` |
| `correct_memory` / `forget_memory` / `restore_memory` / `override_learner_profile` / `review_candidate` / `set_graph_state` | 同名 operation type | `command` |
| `project_summary_to_graph` | `project_summary_to_graph` | `projection` |
| Maintenance 的各个 `kind` | 同名 operation type | `maintenance` |

Gateway 接收公开请求时只接受 `payload.kind`，由代码推导 `operation_type`、`input_kind` 和优先级；内部重放也必须重新执行该校验。

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


class MemoryOperationResult(BaseModel):
    operation_id: UUID
    status: OperationStatus
    operation_type: OperationType
    created_at: datetime
    updated_at: datetime
    completed_at: datetime | None = None
    mutations: list[MutationResult] = Field(default_factory=list)
    review_candidate_ids: list[UUID] = Field(default_factory=list)
    graph_state_changes: list[dict] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    error: "PublicError | None" = None
```

规则：

- 活动文档提交不允许部分成功。
- `mutations` 只返回目标、动作和版本，不返回模型 reasoning、原始 Prompt 或内部存储路径。
- 低置信候选产生 `succeeded + review_candidate_ids`；只有阻塞性冲突才使 operation 进入 `needs_review`。
- `no_change` 是 Graph/策略层的结果，不属于持久化 mutation；它不创建 `mutation_id`、`memory_commits` 或不存在目标文档的 commit，必要时只写 operation 结果和审计指标。
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
user_id: 00000000-0000-0000-0000-000000000000
memory_id: mastery:一致收敛
topic_key: 一致收敛
topic_title: 一致收敛
version: 4
updated_at: 2026-08-10T08:00:00Z
evidence_count: 3
```

`learner.md` 使用 `kind: learner-profile`，不包含 `topic_key/topic_title`；`index.md` 使用 `kind: memory-index`。

### 8.2 存储布局

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

### 8.3 生产对象存储 Key 预留

第一版不实现对象存储，但 Store 抽象使用以下稳定 Key 规则，未来迁移无需改变数据库逻辑路径：

```text
users/{shard}/{user_id}/versions/{memory_type}/{topic_key-or-fixed}/v{version:08d}-{checksum12}.md
```

### 8.4 Checksum 和压缩

- Checksum：SHA-256，小写十六进制 64 位。
- 文件名使用 checksum 前 12 位，数据库保存完整值。
- 第一版不压缩 Markdown 历史版本。
- 孤立版本在创建 24 小时后清理。
- 未删除记忆的历史版本保留到账号删除；Markdown 较小，第一版不做版本压缩。

### 8.5 多文档原子提交

- 一个 operation 可以包含最多 8 个 `MutationPlanDraft`，再由应用代码转换为 `CommitMutationPlan`。
- 一个 `CommitMutationPlan` 只能修改一个逻辑文档。
- 所有新版本先写入不可变 `versions/`，此时不会成为活动版本。
- 数据库事务取得用户级写锁，并按 `memory_id` 字典序锁定目标文档。
- 同一事务中校验全部 `expected_version`、插入 commit、更新活动版本、更新检索索引并写 Outbox。
- 任一校验失败则整个数据库事务回滚；已写文件成为孤立版本，24 小时后清理。
- 数据库提交后再原子物化 `current/`；物化失败不影响活动版本，维护任务根据数据库指针修复。
- 核心读取根据数据库 `active_version` 读取 `versions/`，不读取可能暂时滞后的 `current/`。
- `index.md` 永远异步派生，不参与业务文档原子提交。

因此，第一版不允许“部分成功”的活动文档状态。

### 8.6 删除实现

删除单条记忆时：

1. 数据库将文档标记为 `deleted_at`，设置 `tombstone_until = deleted_at + 30 days`。
2. 活动指针对普通读取不可见。
3. 可恢复版本移动或标记到 `quarantine/`，权限只授予恢复流程。
4. 检索索引删除该文档活动条目。
5. 写入 `memory.deleted` Outbox。
6. 到期后清理版本正文和隔离文件，tombstone 保留最小 hash/时间/来源信息。

账号删除跳过 30 天恢复窗口，并在 24 小时内完成物理删除。

---

## 9. OpenAI 调用规格

### 9.1 Client 和参数

使用 `AsyncOpenAI`，通过 Runtime Context 注入，不进入 Graph State。

```text
model               = env OPENAI_MEMORY_MODEL，默认 gpt-5.6-luna
reasoning.effort     = none
temperature          = 不设置，使用模型/API默认行为
stream               = false
timeout              = 45 秒
max_output_tokens    = 3000（候选提取）/ 4000（MutationPlanDraft）
正常最大调用次数      = 2 次/operation attempt
任务生命周期调用上限  = 4 次
```

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
    """模型输出的非持久化计划草稿；稳定 ID 和版本由应用代码补齐。"""

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

模型输出的 `candidate_indexes` 只引用本次 `CandidateExtractionResult.candidates` 的数组位置，不是跨请求稳定标识。模型返回后，应用代码必须把可提交草稿转换成内部确定计划：

```python
class CommitMutationPlan(BaseModel):
    """应用代码完成读取和策略校验后交给 MemoryService 的确定计划。"""

    model_config = ConfigDict(extra="forbid")

    mutation_id: UUID
    memory_id: str = Field(min_length=1, max_length=160)
    target_memory_type: Literal["learner", "mastery"]
    topic_title: str | None = Field(default=None, max_length=120)
    action: Literal["create", "merge", "replace", "append_evidence"]
    expected_version: int | None = Field(default=None, ge=1)
    learner_patch: LearnerPatch | None = None
    mastery_patch: MasteryPatch | None = None
    candidate_indexes: list[int] = Field(default_factory=list, max_length=20)
```

转换规则：

1. 重新校验目标主题、权限、策略和证据；
2. 读取目标文档当前版本，并由代码注入 `expected_version`；`create` 必须为 `None`，其余动作必须为当前活动版本；
3. 只为实际提交的计划生成 `mutation_id`；`no_change` 草稿不转换为 `CommitMutationPlan`；
4. 仅为需要持久化审核的候选生成 `candidate_id`，并把本次 operation 内的 `candidate_indexes` 映射到数据库候选；
5. `target_memory_type`、patch 类型和 `memory_id` 必须一致，否则拒绝计划。

模型不生成 `user_id`、最终 `topic_key`、绝对路径、SQL、稳定 ID、`expected_version`、删除命令或可执行工具调用。

### 9.3 确定性阈值

第一版阈值：

| 判断 | 规则 |
|---|---|
| 自动写入长期记忆 | `confidence >= 0.80` 且 `long_term_value=save` |
| 进入候选审核 | `0.55 <= confidence < 0.80` 或 `long_term_value=review` |
| 丢弃 | `confidence < 0.55` 或 `long_term_value=ignore` |
| 自动语义合并 | 模型建议 merge，且已有主题检索相似度 `>=0.72` |
| 主题冲突 | 相似度 `0.55–0.72` 或多个主题接近，进入 `needs_review` |
| 图谱模型候选映射 | `mapping_confidence >=0.92`，且第一、第二候选差值 `>=0.15` |

掌握类事实附加限制：

- 单次页面浏览、收藏、打卡不能形成掌握结论。
- 单次普通计算错误不能独立形成稳定误解。
- “熟练”至少需要两条独立正向证据，或用户明确点击“熟悉”。
- “精通”至少需要三条高质量正向证据，来自至少两个事件/会话，并包含一次用户自主解答、推导、迁移应用或讲解证据；不得存在未解决的强冲突证据。
- 从“熟练/精通”降为“学习中”至少需要两条独立强冲突证据，或者一条清楚揭示核心概念误解的证据。

这些阈值必须放在配置/策略模块，写入指标，后续通过评测调整，不能散落在 Prompt 中。

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

Graph State 禁止保存：

- OpenAI Client。
- 数据库连接、Session、事务或连接池。
- 文件句柄和对象存储 Client。
- Reader、Service 和配置实例。
- 密钥、网站会话、JWT。
- 大型原始对话全文；`source_bundle` 在进入 Checkpoint 前必须裁剪到最多 80 KB，并只保留当前 operation 所需内容。

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

### 10.3 父图节点

| 节点 | 输入 | 输出 | DB | OpenAI | 副作用 |
|---|---|---|---:|---:|---:|
| `normalize_input` | operation | 标准化 operation | 否 | 否 | 否 |
| `authorize_actor` | operation | 权限结论 | 可读 | 否 | 否 |
| `idempotency_guard` | operation | 已有结果或继续 | 只读 | 否 | 否 |
| `validate_invariants` | operation | 验证结果 | 只读 | 否 | 否 |
| `route_operation` | operation | route | 否 | 否 | 否 |
| `run_summary` | state | summary 结果 | 见子图 | 见子图 | 见子图 |
| `run_memory_command` | state | command 结果 | 是 | 否 | 是 |
| `run_graph_state` | state | graph 结果 | 是 | 否 | 是 |
| `run_projection` | state | projection 结果 | 是 | 否 | 是 |
| `run_maintenance` | state | maintenance 结果 | 是 | 否 | 是 |
| `normalize_result` | 各分支结果 | 稳定公开结果 | 否 | 否 | 否 |

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

`prepare_commit_mutation_plans` 负责把通过校验的 `MutationPlanDraft` 转换为 `CommitMutationPlan`，生成 `mutation_id`、解析最终 `memory_id` 并读取当前 `expected_version`。确定计划必须先进入可序列化 Graph State，并由 LangGraph 在进入 `commit_summary_memories` 前完成 Checkpoint；副作用节点重放时必须复用同一 `mutation_id`。

`commit_summary_memories` 必须先按 `mutation_id` 查询已存在 commit：已存在则直接返回原结果，再执行其他版本校验；不存在时才校验 `expected_version` 并提交。其数据库事务同时写 `memory.changed` Outbox。总结到图谱的更新由 Consumer 创建独立的 projection operation，不能在该节点直接修改图谱 Overlay。

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
clear           → null / 删除活动 Overlay
```

`expert` 不在用户命令枚举中。

### 10.6 Summary Projection 分支

```text
load_source_memory_version
→ validate_node_mapping
→ evaluate_evidence
→ resolve_projected_status
→ compare_current_state
→ commit_overlay_if_changed
→ emit_graph_state_changed
```

约束：

- 来源总结版本必须仍为活动版本。
- 删除/纠正总结记忆时，不执行相反 delta，而是从仍有效证据重新计算受影响节点。
- 无可靠节点映射时以 `no_change` 成功结束。
- 用户最近操作不会永久锁定状态；但系统降级必须满足强证据规则并产生用户可见通知事件。

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
| Pydantic/Structured Output Schema 错误 | 不在同一节点无限修复；记录一次失败后交任务级策略 |
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
- 如果同一 `mutation_id` 已存在 commit，说明上次副作用已成功，直接返回原提交结果，不再触发版本冲突。
- 用户明确命令不由模型重新解释；返回 HTTP 409，要求前端刷新。
- 总结 operation 两轮仍冲突则进入 `needs_review`。

### 11.4 Checkpoint

- Graph thread 固定为 `memory-op:{operation_id}`。
- Checkpoint 保存节点恢复数据，不保存长期记忆事实。
- terminal operation 的 Checkpoint 保留 7 天。
- `needs_review` 的 Checkpoint 保留 30 天。
- `dead_letter` 的 Checkpoint 保留 30 天。
- 账号删除时 24 小时内清理相关 Checkpoint。

### 11.5 Lease

```text
lease_duration        = 120 秒
heartbeat_interval    = 30 秒
operation_soft_timeout = 150 秒
operation_hard_timeout = 180 秒
```

Worker 被终止后，Scheduler 回收过期 Lease；幂等 mutation 保证恢复执行不会重复提交。

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

### 12.3 返回结构

```python
class MemorySearchHit(BaseModel):
    memory_id: str
    memory_type: Literal["learner", "mastery"]
    topic_key: str | None
    title: str
    summary: str
    matched_excerpt: str | None
    evidence_refs: list[str]
    version: int
    updated_at: datetime
    confidence: float | None
    score: float
```

默认不返回完整 Markdown；`GET /memories/{memory_id}` 才返回结构化完整内容。对 Agent 的返回必须包含版本和有限证据引用，便于追踪。

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

---

## 13. PostgreSQL DDL

以下 DDL 是字段和约束基线；Alembic migration 可以使用 SQLAlchemy 类型表达，但不能改变语义。

### 13.1 扩展

```sql
CREATE EXTENSION IF NOT EXISTS pgcrypto;
CREATE EXTENSION IF NOT EXISTS pg_trgm;
```

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
    operation_type text NOT NULL,
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
        'forget', 'restore'
    )),
    before_version bigint,
    after_version bigint,
    storage_key varchar(1000),
    checksum char(64),
    actor_type text NOT NULL,
    evidence_refs jsonb NOT NULL DEFAULT '[]'::jsonb,
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

`commit_payload` 保存结构化 patch、原因码和必要审计，不保存完整 Prompt、原始对话、模型隐藏 reasoning 或认证信息。

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
    evidence_refs jsonb NOT NULL DEFAULT '[]'::jsonb,
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
    candidate_payload jsonb NOT NULL,
    evidence_refs jsonb NOT NULL DEFAULT '[]'::jsonb,
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
```

### 13.7 知识图谱只读注册表

```sql
CREATE TABLE knowledge_graph_nodes (
    node_id varchar(16) PRIMARY KEY,
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
```

业务 API 对这两张表只读。只有部署/维护命令可以根据仓库文件同步；同步必须校验完整文件 checksum，并在一个事务中替换注册表。

### 13.8 `graph_user_states`

```sql
CREATE TABLE graph_user_states (
    user_id uuid NOT NULL,
    node_id varchar(16) NOT NULL REFERENCES knowledge_graph_nodes(node_id),
    status text CHECK (status IN ('learning', 'proficient', 'expert')),
    version bigint NOT NULL DEFAULT 1 CHECK (version >= 1),
    status_source text NOT NULL CHECK (status_source IN (
        'user', 'summary_memory', 'activity', 'system_recompute'
    )),
    source_memory_id varchar(160),
    source_memory_version bigint,
    evidence_snapshot jsonb NOT NULL DEFAULT '[]'::jsonb,
    evidence_count integer NOT NULL DEFAULT 0 CHECK (evidence_count >= 0),
    last_viewed_at timestamptz,
    last_user_action_at timestamptz,
    last_evidence_at timestamptz,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (user_id, node_id)
);

CREATE INDEX ix_graph_user_states_status
    ON graph_user_states (user_id, status, updated_at DESC);
```

无状态优先表示为不存在活动行；若因审计需要保留行，则 `status=NULL`，查询层统一映射为“无状态”。

### 13.9 `graph_state_audit`

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
    evidence_refs jsonb NOT NULL DEFAULT '[]'::jsonb,
    created_at timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX ix_graph_state_audit_user_node
    ON graph_state_audit (user_id, node_id, created_at DESC);
```

### 13.10 `memory_outbox`

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

### 13.11 维护和模型指标表

```sql
CREATE TABLE memory_maintenance_runs (
    run_id uuid PRIMARY KEY,
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

### 13.12 Tombstone

不单独创建 `memory_tombstones` 表。文档 tombstone 使用 `memory_documents.deleted_at/tombstone_until`；候选 tombstone 使用 `memory_review_candidates.tombstone_until`。物理清理后只保留正文不可还原的 checksum、时间和动作审计。

### 13.13 事务和锁

第一版隔离级别使用 `READ COMMITTED` + 显式锁：

1. 锁 operation 行。
2. 获取用户级 PostgreSQL transaction advisory lock。
3. 按 `memory_id` 字典序 `SELECT ... FOR UPDATE` 锁文档。
4. 按 `node_id` 字典序锁图谱 Overlay。
5. 校验 `expected_version`。
6. 写 commits、索引、活动指针、审计和 Outbox。
7. 提交事务。

用户级锁 key 由固定 namespace 和 `user_id` 稳定 hash 组成。所有 Memory 写入口必须使用同一实现，禁止各模块自行定义锁顺序。

死锁、serialization failure 最多重试 3 次；重试仍失败则交任务级重试。

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

Gateway 流程：

1. 在数据库持久化 operation。
2. 尝试由 `MemoryGraphRunner` 领取并执行该 operation，最多等待 2 秒。
3. 2 秒内完成则返回 200。
4. 未完成或发生临时错误则保持/恢复 queued 状态，返回 202，由 Worker 接管。

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
| 校验活动 checksum/物化副本 | 每天 04:00，Asia/Shanghai |
| 备份完成状态检查 | 每天 05:00，Asia/Shanghai |

支持管理员通过 CLI 手动触发维护任务；手动触发仍创建带幂等键的 maintenance run，不直接修改数据。

### 14.4 Outbox Consumer

- 本期实现独立 Consumer 进程。
- 轮询间隔 1 秒。
- 每批 100 条。
- Lease 60 秒。
- 最大重试 10 次，指数退避上限 30 分钟。
- 使用唯一事件键和消费端 inbox/幂等处理，保证至少一次投递下不重复产生业务效果。

第一版投递目标：

1. 内部 summary-to-graph projection operation。
2. 前端可查询的通知/变更记录。
3. 未来主动学习 Agent 使用的内部事件接口。

第一版不引入 Redis、RabbitMQ、WebSocket 或 SSE。前端通过 REST operation 轮询和页面重新查询获取状态。

### 14.5 Docker Compose 服务

```text
postgres
memory-api
memory-worker
memory-scheduler
memory-outbox-consumer
```

前端开发服务可以单独运行；生产可由现有网站容器或静态服务器提供。

### 14.6 本地启动

```bash
# 安装和锁定依赖
uv sync --extra dev

# 启动 PostgreSQL
docker compose up -d postgres

# 执行数据库迁移
uv run alembic upgrade head

# 同步固定知识图谱只读注册表
uv run python -m backend.memory.cli sync-knowledge-graph

# 启动 API
uv run uvicorn backend.app:app --host 127.0.0.1 --port 8000 --reload

# 启动 Worker
uv run python -m backend.memory.worker.main

# 启动 Scheduler
uv run python -m backend.memory.worker.scheduler

# 启动 Outbox Consumer
uv run python -m backend.memory.worker.outbox_consumer
```

完整环境：

```bash
docker compose up --build
```

### 14.7 环境变量

```text
APP_ENV
DATABASE_URL
MEMORY_STORAGE_ROOT
MEMORY_API_HOST
MEMORY_API_PORT
MEMORY_WORKER_CONCURRENCY
MEMORY_OPERATION_LEASE_SECONDS
MEMORY_OUTBOX_POLL_SECONDS
MEMORY_SCHEDULER_TIMEZONE
OPENAI_API_KEY
OPENAI_MEMORY_MODEL
OPENAI_MEMORY_TIMEOUT_SECONDS
AUTH_ISSUER
AUTH_AUDIENCE
AUTH_JWKS_URL 或 AUTH_PUBLIC_KEY
SERVICE_TOKEN_AUDIENCE
BREAK_GLASS_ENABLED
LOG_LEVEL
LOG_HMAC_KEY
DEV_AUTH_ENABLED
```

Secret 不写入仓库和普通环境样例值；生产通过 Docker secrets 或云服务器 Secret 管理注入。

### 14.8 健康检查

```text
GET /health/live      进程存活，不访问外部依赖
GET /health/ready     PostgreSQL、迁移版本、存储目录可读写、图谱注册表已加载
GET /health/startup   启动初始化完成
GET /metrics          可选 Prometheus 文本指标；不得含 user_id
```

---

## 15. Outbox 事件契约

统一事件信封：

```python
class MemoryDomainEvent(BaseModel):
    event_id: UUID
    event_type: str
    event_version: Literal[1] = 1
    user_id: UUID
    aggregate_type: str
    aggregate_id: str
    aggregate_version: int
    occurred_at: datetime
    trace_id: str
    payload: dict
```

事件定义：

### `memory.changed`

```json
{
  "memory_id": "mastery:一致收敛",
  "memory_type": "mastery",
  "before_version": 3,
  "after_version": 4,
  "topic_key": "一致收敛",
  "graph_projection_candidates": ["n067"]
}
```

### `memory.deleted`

```json
{
  "memory_id": "mastery:一致收敛",
  "deleted_version": 4,
  "restore_until": "2026-09-09T08:00:00Z"
}
```

### `learner.updated`

```json
{
  "memory_id": "learner",
  "before_version": 6,
  "after_version": 7,
  "changed_sections": ["goals", "plans"]
}
```

### `graph_state.changed`

```json
{
  "node_id": "n067",
  "before_status": "proficient",
  "after_status": "learning",
  "source": "summary_memory",
  "explanation_available": true
}
```

事件 payload 不包含完整 Markdown、完整证据正文和模型 reasoning。

---

## 16. 固定知识图谱和用户 Overlay

### 16.1 固定图谱解析

权威来源：

- `knowledge_graph/数学知识科技树关系图.md`：节点 ID、节点标题、分组和有向边的权威来源。
- `knowledge_graph/教材目录.md`：教材层级和展示元数据的辅助来源，不负责生成节点 ID。

解析规则：

- Mermaid 中 `nNNN["标题"]` 解析为节点。
- Mermaid `subgraph` 解析为 `group_key`。
- `nAAA --> nBBB` 解析为 prerequisite 有向边。
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

### 16.3 Summary 与图谱的弱连接

总结记忆更新图谱的必要条件：

1. 总结记忆版本已经成功提交并仍为活动版本。
2. 存在合法固定节点映射。
3. 映射来自上游明确 node ID、确定性别名，或通过高阈值模型候选校验。
4. 状态变化有内部 evidence refs。
5. `KnowledgeGraphStateService` 的确定性规则允许该转换。

任一条件不满足：总结记忆照常保存，图谱返回 `no_change`。

### 16.4 推荐接口

推荐不调用模型，使用固定图谱、Overlay 和已确认弱连接进行确定性排序。

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
- 对话系统删除内容后应发送 source deletion 事件，触发相关总结记忆重新评估。

### 17.2 ActivityReader

本期只实现接口和测试适配器。未来正式适配器必须满足：

- 读取 forum post/reply、错题、练习结果和复习结果的稳定引用。
- 页面浏览、收藏和打卡在上游聚合后提交；Memory 模块不负责采集网站埋点。
- 默认聚合 key：`user_id + activity_type + topic_hint + 1h window`。
- `page_view/bookmark/check_in` 不调用 OpenAI；只作为推荐或 exposure 信号。
- `forum_post/forum_reply/wrong_question_upload/exercise_attempt/review_result` 在存在内容和学习价值时才进入总结 Graph。
- 内容被删除时，外部系统发送删除事件，Memory 模块重新评估依赖该 evidence ref 的活动记忆。

### 17.3 SourceBundle 上限

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
    total_utf8_bytes: int = Field(le=80_000)
```

超过上限由 Reader 按引用范围拒绝或裁剪，Graph 不在 Checkpoint 中保存无限正文。

---

## 18. 认证、权限和用户隔离

### 18.1 生产认证契约

- 网站后端完成用户登录认证。
- 网站后端或内部 Agent 使用短时签名 JWT 调用 Memory API。
- JWT 至少包含：`iss`、`aud=memory-api`、`sub=user_id`、`actor_type`、`scopes`、`iat`、`exp`、`jti`。
- token 最长有效期 5 分钟。
- Memory API 只信任配置中的 issuer/JWKS/public key。
- 浏览器不能自行设置 `actor_type` 或内部 scope。
- 若浏览器通过同域网站后端访问，网站后端充当 BFF，Memory API 不读取浏览器传来的 `user_id`。

本地开发：

- `DEV_AUTH_ENABLED=true` 且 `APP_ENV=development` 时允许测试身份。
- 测试身份只能从 loopback/Compose 内网进入。
- 生产环境启动时若 `DEV_AUTH_ENABLED=true`，服务必须拒绝启动。

### 18.2 Scope

```text
memory:read
memory:submit_evidence
memory:correct
memory:delete
memory:restore
memory:review
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
POST /api/v1/memory/search
GET  /api/v1/memory/review-candidates
```

查询默认不返回原始 Markdown 文件路径和历史正文。管理员普通 token 不允许调用正文接口。

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

`PUT state` 的 action 只允许：

```json
{"action": "mark_unfamiliar", "expected_version": 2}
```

或：

```json
{"action": "mark_familiar", "expected_version": 2}
```

任何 `expert` 请求均返回 422：

```json
{
  "error": {
    "code": "GRAPH_STATUS_NOT_USER_SETTABLE",
    "message": "精通状态由长期学习表现自动评估，不能手动设置。",
    "retryable": false
  }
}
```

### 19.6 Agent 内部接口

其他 Agent 优先使用 `MemoryClient`，不依赖 HTTP 路由细节：

```python
class MemoryClient:
    async def submit_conversation_evidence(...) -> MemoryOperationResult: ...
    async def submit_activity_evidence(...) -> MemoryOperationResult: ...
    async def search_summary(...) -> list[MemorySearchHit]: ...
    async def build_learning_context(...) -> LearningContext: ...
    async def get_graph_recommendations(...) -> list[GraphRecommendation]: ...
```

第一版不提供 MCP；未来如需外部 Agent 接入，在 `MemoryClient` 之上增加 MCP Adapter。

---

## 20. 前端最小接入

### 20.1 Profile：“AI 记住了我什么”

必须实现：

- learner 和 mastery 列表加载。
- 查看结构化记忆内容和更新时间。
- 纠正记忆，携带 `expected_version`。
- 删除、30 天内恢复。
- 查看 `needs_review` 候选。
- 接受、修改或拒绝候选。
- 对 409 冲突刷新数据后提示用户重新确认。

不得展示：

- 模型隐藏 reasoning。
- 内部 Prompt。
- 文件系统路径。
- break-glass、lease、graph thread/checkpoint 字段。

### 20.2 KnowledgeMap

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
- 数据库和 Markdown 备份使用同一备份批次 ID，并保存 manifest/checksum。
- 每周在隔离环境做一次自动恢复验证。
- 支持按备份批次完整恢复；支持通过 user_id 导出数据库行和对应 Markdown 进行单用户恢复演练。
- 第一版不承诺任意时间点恢复；需要更低 RPO 时再启用 WAL 归档/PITR。
- 账号删除后的备份副本不主动逐包改写，随 30 天周期淘汰；恢复旧备份时必须重新应用删除清单。

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

### 23.2 Graph 测试

每个节点用 fake Runtime Context 测试：

- 无长期价值时不调用 commit。
- 低置信候选进入 review，不写活动 Markdown。
- OpenAI 输出非法路径/ID 被拒绝。
- 总结记忆没有图谱节点时照常提交。
- 总结提交后通过 Outbox 创建 projection，而不是直接改图谱。
- 用户命令分支不调用 OpenAI。
- `expert` 只有满足强证据策略才能生成。
- Checkpoint 恢复后不重复 mutation。

### 23.3 PostgreSQL/Markdown 集成测试

必须使用真实 PostgreSQL 容器和临时文件系统：

- 多文档活动版本原子切换。
- 数据库提交前宕机只留下孤立版本。
- 数据库提交后物化失败，读取仍返回正确版本并可修复 current。
- 同一 mutation 重放只返回原 commit。
- 同一幂等键不同 payload 返回冲突。
- user advisory lock 和 expected_version 冲突。
- Outbox 与 commit 同事务。
- 删除、隔离、恢复和 30 天清理。

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

### 23.6 前端测试

- Profile 加载、纠正、删除、恢复和候选审核。
- KnowledgeMap 四状态显示。
- 点击熟悉/不熟悉/清除。
- 点击精通时出现指定提示且不发非法请求。
- 乐观更新失败回滚。
- operation 轮询停止和超时提示。

### 23.7 CI

GitHub Actions job：

```text
backend-lint       Ruff + mypy
backend-unit       pytest unit/graph
backend-integration PostgreSQL service + integration/failure tests
frontend           npm ci + lint/test/build
contracts          OpenAPI snapshot
container-build    docker compose build
```

---

## 24. 实施顺序

1. 创建 Python 工程基线、settings、Docker Compose 和 CI 骨架。
2. 建立 Pydantic 契约和错误模型。
3. 编写 Alembic DDL、Repository 和知识图谱同步器。
4. 实现 Markdown Store、版本协议和原子提交。
5. 实现 `MemoryService` 和 `KnowledgeGraphStateService`。
6. 实现 Reader 接口和测试适配器。
7. 实现 OpenAI Structured Outputs Schema、Prompt 和评测样例。
8. 实现 Graph State、父图和各分支。
9. 实现 Worker、Scheduler、Checkpoint 和 Outbox Consumer。
10. 实现 Gateway API、认证适配器和 `MemoryClient`。
11. 实现检索、`LearningContextService` 和推荐接口。
12. 接入 Profile 与 KnowledgeMap。
13. 完成失败注入、隐私、安全和端到端验收。
14. 编写启动、备份、恢复和故障处理文档。

---

## 25. 最终裁决摘要

```text
后端：Python 3.13 + FastAPI + Pydantic v2
数据库：PostgreSQL 17 + SQLAlchemy async + Alembic + psycopg 3
Graph：LangGraph 1.2.1 + PostgreSQL Checkpointer
模型：OpenAI SDK 2.38.0 + gpt-5.6-luna + Structured Outputs
部署：单台云服务器 + Docker Compose
Markdown：不可变版本 + 数据库活动指针 + current 物化副本
检索：pg_trgm，不使用向量库
任务：PostgreSQL 持久队列 + Lease + 至少一次执行
提交：用户锁 + expected_version + mutation_id 幂等 + Outbox
图谱：固定节点/边只读，Overlay 显示无状态/学习中/熟练/精通
认证：网站统一认证，Memory API 不实现登录
删除：30 天可恢复，账号删除 24 小时内物理清理
管理员：默认不可读正文，break-glass 例外且完整审计
外部 Agent：本期只定义 ConversationReader/ActivityReader 接口
前端：接入 Profile、KnowledgeMap 和 operation 轮询
CI：GitHub Actions
```

在本文件获得需求方确认前，不开始业务代码实现。

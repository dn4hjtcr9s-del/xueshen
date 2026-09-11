# Phase 4 交接文档（memory-rebuild §5.6）

> **用途**：Phase 4 尚未完成，且实施者的上下文已达上限。本文件把"继续 Phase 4 所需的
> 全部信息"从对话历史外化到工作区——新会话或新 agent 只需读本文件 + 下述几个代码文件，
> 即可无缝接手，不必重读前面 8 个 Phase 的历史。
>
> **维护约定**：Phase 4 完成后**删除本文件**（它不是设计文档，是临时交接件）。
> 设计决策与偏差记录仍归 `memory-rebuild-deviations.md`。

---

## 一、当前状态

分支 `codex/memory-rebuild-implementation`，工作区**有未提交改动**（见第五节清单）。
Phase 0/1/2/3 已提交（`c13688a`、`1748dc7`、`c2dc6de`、`d9b3f7c`）。

### 已完成并验证（第二批：契约扩展与版本升级）

| 项 | 文件 | 验证 |
|---|---|---|
| index v2 两列迁移 | `alembic/versions/0008_memory_index_v2_columns.py` | 已写，**尚未在真实库跑过 upgrade** |
| v1/v2 双读解析器 | `backend/memory/storage/markdown_schema.py` | mastery/index 的 v1+v2 往返已实测；`tests/unit` 739 passed；mypy 302 files 全绿 |
| v2 planner 提示词 | `backend/memory/graph/prompts/build_mutation_plan_v2.md` | 子代理交付 |
| v3 extractor 提示词 | `backend/memory/graph/prompts/extract_candidates_v3.md` | 子代理交付 |
| 提示词/schema 绑定校验函数 | `backend/memory/graph/prompt_loader.py::validate_schema_prompt_binding` | 纯函数，10 个单测（`tests/unit/test_prompt_schema_binding.py`） |
| ✅ 第 1 项：`frontmatter_patch` 动作 + `FrontMatterPatch` 模型 | `contracts/commands.py` | 两个 Literal 均已加；`CommitMutationPlan` 已能承载 |
| ✅ 第 2 项：`related_topic_hints` | `graph/llm_schemas.py::CandidateMemory` | 已加（≤5） |
| ✅ 第 3 项：删除提示词兼容分支 | 两个 v2/v3 提示词 | 已删净 |
| ✅ 第 4 项：升级版本常量 | `prompt_loader.py` | v1→v2 / v2→v3；校验函数现默认通过 |

### markdown_schema.py 已具备的能力（不要再重写）

- 常量：`SCHEMA_VERSION_V1=1` / `SCHEMA_VERSION_V2=2` / `CURRENT_SCHEMA_VERSION=2`
- 工具：`extract_links()`（`[[...]]` 提取，悬空合法）、`normalize_aliases()`、`document_links()`
- dataclass 均新增 `schema_version` 字段（**保留原始版本**：v1 读进来写回去仍是 v1）
- `LearnerDocument` / `MasteryDocument` 新增 `name` / `description` / `aliases` / `links`
- `IndexEntry` 新增 `description` / `aliases` / `related_topic_keys` / `keywords`
- 渲染按 `doc.schema_version` 分派：`_v2_front_matter()`、`_render_index_v2()`
- 解析：`_schema_version_of()`、`_v2_parsed_fields()`（返回 `V2ParsedFields` TypedDict）、
  `_parse_index_blocks()`（v2 块结构）、`parse_index()` 内按版本分派
- v2 校验：`name`/`description` 必填、`description` 必须单行

---

## 二、剩余工作（按此顺序）

### ~~1. 扩展 mutation plan 契约~~（已完成）：`frontmatter_patch` 动作 + frontmatter 承载字段

**为什么**：§3.6① 要求 planner 能输出 `frontmatter_patch` 动作，但当前 schema 没有它。
子代理用 `to_strict_json_schema` 实测确认：

| 项 | 位置 | 现状 |
|---|---|---|
| `MutationPlanDraft.action` | `backend/memory/contracts/commands.py:332` | Literal 只有 `create/merge/replace/append_evidence/no_change` |
| `CommitMutationPlan.action` | `backend/memory/contracts/commands.py:354` | 同上 5 个 + `forget/restore` |
| frontmatter 承载字段 | `CommitMutationPlan` | **没有**承载 name/description/aliases 的字段 |

两者都是 `model_config extra="forbid"`，strict JSON schema `additionalProperties=False`——
**模型一旦输出未声明的动作名，整批计划会被 `OpenAISchemaInvalidError` 拒绝**。

要做：加 `frontmatter_patch` 到两个 Literal；给 `CommitMutationPlan` 加承载
`description` / `aliases`（`name` 一般不变）的字段；同步 `summary.py` 里消费
`action` 的分支与 validator。

### ~~2. 扩展 `CandidateMemory`：`related_topic_hints`~~（已完成）

**位置**：`backend/memory/graph/llm_schemas.py:49-67`（8 个字段，`extra="forbid"`）。
要加 `related_topic_hints: list[str]`（≤5，见 v3 提示词）。"主体归属"当前只能靠
`topic_title`（mastery）与 `memory_type`（learner）表达——子代理已按此写提示词，
**不需要**再加独立归属字段。

### ~~3. 删掉两个提示词里的"兼容说明"分支~~（已完成）

子代理在 schema 未扩展时加了两条防御性说明（各 3 行）：
- `build_mutation_plan_v2.md`「动作选择」末尾：schema 无 `frontmatter_patch` 时改用 `merge`
- `extract_candidates_v3.md`「输出要求」末尾：把相邻主题以"相邻主题："追加进 `summary`

**第 1、2 项落地后必须删掉这两条**，否则提示词自我削弱。

### ~~4. 升级提示词版本常量~~（已完成）

`backend/memory/graph/prompt_loader.py`：
- `BUILD_MUTATION_PLAN_PROMPT_VERSION`: `build_mutation_plan_v1` → `build_mutation_plan_v2`
- `EXTRACT_CANDIDATES_PROMPT_VERSION`: `extract_candidates_v2` → `extract_candidates_v3`

**注意**：会影响 `tests/unit/test_llm_boundary.py` 与 `prompt_version` 审计记录，
需同步更新相关断言。改完 `validate_schema_prompt_binding()` 才会通过（现在会抛错，是设计意图）。

### 5. 把校验函数接到启动路径

子代理**没有接线**（不在它的改动范围内）。建议两处：
- `backend/memory/worker/main.py` 的 `_run()`，构造 `MemoryRuntimeContext` 之前（约 147–150 行）
- `backend/app.py` 的 `@app.on_event("startup")`（约 567 行）

### 6. index 投影落 PG

**⚠️ 断点警告（2026-09-13）**：本项**已尝试过一次，改坏了 `memory_service.py` 并被回滚**。
坏因：用 `s.replace("async def ", helper + "async def ", 1)` 插入辅助函数，命中了第一个
`async def`（在 class 内部），导致整个文件缩进崩坏。**重做时请显式指定插入锚点**
（如 `\nasync def _existing_name(`），并在改完后**立即**跑 `ruff check` 与
`python -c "import backend.memory.services.memory_service"`。已确认可用的做法见下。

需要改两处：
- `_apply_frontmatter_patch(doc, patch)`（模块级辅助，注意**只在 name+description 齐备时**
  才把 `schema_version` 升到 v2——半套 frontmatter 渲染成 v2 会让文档下次读不出来）
- learner/mastery 两个分支里 `plan.frontmatter_patch` 的应用点，以及 `index_data` 增加
  `aliases` / `related_topic_keys`，并把 aliases 并入 `search_text`

把 v2 的 `description` / `aliases` / `related_topic_keys` / `keywords` 投影进
`memory_index_entries`（迁移已加好 `aliases` + `related_topic_keys` 两列 + GIN 索引）。

- 读侧：`backend/memory/persistence/index_entries.py::search_candidates` 的 SELECT
  要带上新列；`search_text` 的拼装要不要纳入 aliases 需定（建议纳入，alias 就是检索键）
- 写侧：找到写 `memory_index_entries` 的地方（大概率在 `summary.py` 的 commit 链或
  `services/` 下），补上投影
- **Phase 4 决策**：`title` 列就是 index 的 `name`、`summary` 列就是 `description`，
  不新开列（已在迁移注释与 `IndexEntry` docstring 写明）

### 7. `migrate_markdown_schema_v2` 调度任务

`backend/memory/worker/scheduler.py` 新增 daily 任务，复用既有机制
（`memory_maintenance_runs` 幂等 + batch cursor + continuation + advisory lock）：

- 触发：`ScheduledTask("migrate_markdown_schema_v2", daily_at=time(?, ?))`
  现有任务是 02:30 / 03:00 / 03:30 / 03:45 / 04:00 / 04:30 / 05:00，**挑一个空档**（如 04:15）
- 门控：`memory_schema_v2_migration_enabled`（Phase 0 已定义，默认 false）
- 顺序：先扫描并报告不合规文档 → 对可迁移用户按 **learner → mastery → index** 读取 v1、
  渲染 v2 → 走正常 `write_immutable_version` 追加新版本 → 成功后更新 `current/`
- 失败：记录 reason / last key / attempt count，允许下次从 cursor 继续；单用户失败不阻塞他人
- **铁律**：`versions/` 历史文件**字节级不变**，永不原地迁移（§2.7 决议 A 组、§5.6）

### 8. Phase 4 测试 + 偏差登记 + 提交

测试（§5.6「迁移验收」）：
- v1 / v2 / 坏 frontmatter / 非法 link / 重复 alias / Unicode topic key fixture 都能被明确分类
- 同一用户迁移两次：第二次无新版本、无 checksum 抖动、无重复副作用
- 历史 versions 文件字节级不变；current 只通过 immutable write 产生新 checksum
- 迁移中 kill scheduler 后可续跑；单用户失败不阻塞他人
- 旧 reader 能读迁移后的 v2 current；新 reader 能读未迁移的 v1

偏差登记待补条目（写进 `memory-rebuild-deviations.md` 的 Phase 4 段）：
- §3.3 描述错误：`validate_existing_topic_key` 是**纯语法校验**，不碰 KG registry；
  写入路径也没有 registry 校验 → "mastery 自由建档"**当前已成立**，无需改代码。
  **不要**按文档删掉它（会丢路径覆盖防护）
- index v2 复用 `title`/`summary` 而非新开列（用户决策）
- 解析器始终双读，flag 只管写入/迁移（用户决策）
- 提示词只出 v2 planner + v3 extractor，`summary_consolidate_v1` 留 Phase 7（用户决策）
- `<memory_listing>` 标签在 backend 无实现，提示词里已改写措辞（子代理发现）
- `[[link]]` 的 "name 全局唯一" 目前只是生成侧纪律，无代码唯一性校验

---

## 三、验证命令

```bash
cd /Users/kebofeier/Desktop/xueshen/.local/worktrees/memory-rebuild-implementation
export UV_CACHE_DIR=/Users/kebofeier/Desktop/xueshen/.local/uv-cache

# 门禁
uv run ruff check backend tests && uv run ruff format --check backend tests && uv run mypy backend

# 单测（Phase 4 开始前基线 739 passed）
uv run pytest tests/unit tests/test_mineru_ocr_*.py -q

# 迁移（需要 postgres：docker compose up -d --wait postgres）
docker compose exec -T postgres psql -U postgres -tAc \
  "SELECT 1 FROM pg_database WHERE datname='memory_test'" | grep -q 1 || \
  docker compose exec -T postgres createdb -U postgres -O memory memory_test
DATABASE_URL="postgresql+psycopg://memory:memory@127.0.0.1:55432/memory_test" uv run alembic upgrade head
DATABASE_URL="postgresql+psycopg://memory:memory@127.0.0.1:55432/memory_test" uv run alembic downgrade -1
DATABASE_URL="postgresql+psycopg://memory:memory@127.0.0.1:55432/memory_test" uv run alembic upgrade head

# 全量集成
bash scripts/ci-local.sh backend-integration
```

**注意**：`sync-knowledge-graph` 第二次运行曾因测试遗留节点中断，已由
`tests/integration/conftest.py::_cleanup_test_graph_nodes` 修复；若再遇到退出码 3，
先确认该 fixture 生效。

---

## 四、踩过的坑（避免重复）

1. **`alembic_version.version_num` 是 varchar(32)** —— revision id 必须 ≤32 字符，
   本链惯例是缩写（如 `0004_ks_alias_group_unique`）。
2. **`ASYNNC240`**：async 函数里不能直接用 `pathlib.Path` 方法，要 `asyncio.to_thread`
   或抽到同步辅助函数。
3. **`ruff format` 会改到别人的文件** —— 只 format 自己改的文件，否则会把无关改动带进提交。
4. **edit 工具的文件新鲜度**：外部（如 ruff format）或自己改过之后必须重新 `read` 才能 `edit`；
   批量替换用 Python 脚本更可靠，但**替换锚点必须在改完后立即验证**（我踩过一次：
   两处相同文本只替换了第一处，导致另一个函数被改坏）。
5. **测试并发约束**：`uq_rollout_segment_turn_active`（一 turn 一段）、
   `UNIQUE(thread_id, client_request_id)`；测试造数据时注意。
6. **strict JSON schema + `extra="forbid"`**：模型输出未声明字段会让整批结果被拒，
   这是第 1、2、3 项必须一起做的原因。

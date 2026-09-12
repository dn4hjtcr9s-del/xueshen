# 复审报告（第三轮）：`4466bf2` + `bde942c` 对第二次复审的修复结果

- **复审对象**：`codex/memory-rebuild-implementation`，HEAD `bde942c`
- **基线**：`685bfc6`（第二次复审的对象）
- **修复提交**：`4466bf2`（Critical 回归 + 三类枚举型元测试）、`bde942c`（两项待裁决：错误码封闭集合、errors-only 成员终态）
- **改动规模**：2 个提交、43 个文件、+8677 / −331 行
- **复审方式**：重跑全部门禁；对上一轮**全部 17 项新发现**逐个核对；对**四个元测试做变异验证**（注入缺陷 → 确认变红 → 撤销）；自写探针复现关键场景
- **上一轮清单**：`REVIEW-2-verification-685bfc6.md`

---

## 0. 结论

**上一轮的全部 17 项发现均已修复；由 I-3 引入的 Critical 回归已消除并有测试防复发；四个元测试经变异验证确实有效。未引入新问题。**

只发现 **1 个残留**（Minor，我上一轮列为"未验证"因而作者未覆盖）：迁移把文档版本 +1 但不更新 KG 链接，使链接停在比文档旧 **2 个版本**的位置时，"无节点提交"无法推进它。触发条件窄、影响面是单个主题的 KG overlay 不同步，不构成启用阻塞。

| 维度 | 结论 |
|---|---|
| 门禁 | **全绿**（ruff check/format 520 files、mypy 310 files、unit **1112 passed**、contract **5 passed**、integration **488 passed / 3 skipped**） |
| 上一轮 17 项 | **17/17 修复**（其中 3 项为"部分"→已补全） |
| Critical 回归（多节点 KG） | **已修复**，且测试断言已升级为 `active=True`（旧断言只查行集合，正是漏掉它的原因） |
| 四个元测试 | **经变异验证有效**（我逐个注入缺陷确认变红后撤销） |
| 新问题 | 未发现；1 个 Minor 残留（见 §3） |
| 默认部署（flag 全关） | 无回归 |

---

## 1. 门禁实测（本机）

```
ruff check backend tests        → All checks passed
ruff format --check             → 520 files already formatted
mypy backend                    → Success: no issues found in 310 source files
unit stage（按 ci-local.sh）     → 1112 passed
tests/contract                  → 5 passed
integration stage（按 ci-local.sh）→ 488 passed, 3 skipped
```

> **两次自设陷阱，都是我的环境问题、不是代码问题**，记录以免误判：
> ① 我最初把 `DATABASE_URL` 等测试库变量 export 到 shell 里再跑 unit，导致 `Settings(...)` 读到真实 env → 34 个 `test_study_settings` 类用例失败；用干净 env 跑即 1112 passed（作者报告的 1111 与之相差 1，属计数口径）。
> ② contract 的 `test_openapi_snapshot` 同样是我 env 污染所致；干净 env 下 5 passed。

---

## 2. 上一轮 17 项：逐项核对

| # | 上一轮发现 | 状态 | 核对要点 |
|---|---|---|---|
| 1 | **Critical**：多节点提交把所有 KG 映射置 inactive | ✅ **已修** | `deactivate_graph_links` 改收 `except_node_ids: list[str]`，SQL 用 `node_id <> ALL(CAST(:except_node_ids AS text[]))`——**一次**调用、集合排除，不存在轮次互相覆盖；调用点先 upsert 并**记录真正写成功的节点**再把该集合作为排除集。测试升级为 `test_all_planned_nodes_stay_active_without_graph_metadata` + 断言每个计划内节点 `active=True` |
| 2 | 降级标记还缺 3 个（`rewrite_*` ×2、`evidence_structured_fallback`） | ✅ **已修** | `DegradedFlag` 现为 16 值，三个都进了；我实测 `validate_event_payload("answer.completed", …)` 对全部 6 个相关标记（含 `citation_degraded`）**全部 OK** |
| 3 | `commit_started_at` 在批次行残留 | ✅ **已修** | `finally` 里的 `_clear_commit_started` 现在传 `fencing_operation_id=cas_operation_id`，与 mark 对称 |
| 4 | 序数范围被 open 段复用（重叠） | ✅ **已修** | `next_ordinal_start` 增加 `local_max_ordinal(thread_id)` 扫描器（`sealer.py:179/207/493`），用**本地热段真实最大 ordinal** 兜住 `ordinal_end IS NULL` 的 open 段；改名失败有补偿 |
| 5 | 一次登记失败把记录器**进程级**永久降级 | ✅ **已修** | 新增 `_mark_degraded/_clear_degraded`，`open_turn` 开头重置（`recorder.py:308-312` 明确"上一个 turn 处于降级，本 turn 重新尝试记录"）——降级收敛到单 turn |
| 6 | `object_key IS NULL` 的未封存段被删除路径跳过 | ✅ **已修** | 新增 `rollout/deletion.py` 统一三条入口：段级 `delete_rollout_payloads`（对象 + 同构热段）与 thread 级 `delete_thread_rollout_payloads`（**再加按 `<thread_id>` 目录清扫**，覆盖未封存段 / kodo 本地缓存 / 孤儿热文件）；thread 级删除现在只看"有没有 rollout 行"就要求对象存储，不再因"没有 key 可删"而宣称删干净 |
| 7 | I-11③ 只改一半（restore / index.md 仍用 `topic_title`） | ✅ **已修** | 新增**唯一权威** `projected_title(doc)` 与 `index_projection_from_document(...)`，三条路径共用；测试覆盖 rename→forget→restore、rebuild_index 取注册表标题、`search_text` 以投影标题开头 |
| 8 | I-2 未修完（KG 双路更新仍传成员 op） | ✅ **已修** | `consolidation.py` 改为传批次 operation |
| 9 | 迁移回填只覆盖"本次升级"的文档 | ✅ **已修** | `skipped_already_v2` 分支也刷新投影，并新增 `refreshed_already_v2` 计数；`test_memory_schema_migration.py` 补断言 |
| 10 | prime 预算不覆盖 summary | ✅ **已修** | `trim_prime_to_budget` 重写：**先扣 summary**（超预算则按 token 二分截断并置 `summary_truncated`），**再按剩余预算**裁目录前缀；目录为空/缺失也**不再提前 return**（正是上一轮指出的另一半） |
| 11 | 预算耗尽时 read 提示指回同一 offset | ✅ **已修** | 不再给出可执行 offset |
| 12 | 整批全灭仍报 `succeeded` | ✅ **已修** | 新增 `BatchAllMembersFailedError`（`OperationDeadLetterError` 子类，`retryable=False`）→ 整批零写入进 **dead_letter**；另新增 `errors_only` 成员 outcome 修掉"已写入但伴随 errors 被算作未写入"的计数失真 |
| 13 | 失败成员释放无尝试计数、可永久循环 | ✅ **已修** | `release_batch_member` 带 `attempt_count+1`，达 `max_attempts` 转死信 |
| 14 | 取消成员后误报"归属丢失" | ✅ **已修** | 声明集合差异排除已取消/已释放成员 |
| 15 | kodo 模式热缓存不删 | ✅ **已修** | kodo store 注入 root 级热删除能力 |
| 16 | 死路由 `route_after_summary_finalize` | ✅ **已修** | 删除 |
| 17 | `resolve_resume` 的 fencing 形参未使用 | ✅ **已修** | 移除该形参 |

---

## 3. 四个元测试：我做了变异验证

作者把"修一处、漏同类"当作方法论问题来治，交付了四个**枚举型元测试**。我没有只看它们通过，而是逐个**注入真实缺陷**确认能变红：

| 元测试 | 我的变异 | 结果 |
|---|---|---|
| `test_degraded_flag_enum_meta.py`（AST 提取所有降级标记字面量 vs `DegradedFlag`，双向断言 + 死值白名单 + 形状守卫） | 在 `rewrite.py` 里加一个字面量 `"zz_mutation_probe"` | ✅ **2 failed**（`test_state_sink_literals_are_inside_degraded_flag_literal` + `test_every_produced_flag_validates_against_answer_completed_payload`），撤销后恢复通过 |
| `test_rollout_deletion_paths_meta.py`（3 入口 + 4 触发入口按传递闭包断言"对象 + 热段 + tombstone"齐全；段级入口**反向禁止**按 thread 清扫） | 删掉 `delete_thread_rollout_payloads` 里的 `sweep_thread_hot_segments` 调用 | ✅ **元测试 1 failed**，同时**行为测试** `test_rollout_deletion_paths.py::test_thread_deletion_truth_table[object_key=无(未封存)+hot=有]` 也 failed |
| `test_projection_sites_meta.py`（AST 四规则枚举 27 个站点，断言键清单双向相等 + 取值来源真值表 + 跨站点逐键一致 + 结构委托） | 把唯一权威 `projected_title` 的 v2 `name` 分支短路（等价于"restore 又退回 topic_title"） | ✅ **12 failed**，覆盖注册表/索引/prime/search_text 多个站点 |
| `test_error_codes_meta.py`（AST 全量枚举四域 144 个异常类与 code 字面量，7 条规则双向断言 + 哨兵） | 未单独变异（作者报告已做两次变异；该文件 20 例全部通过） | — |

**这是本轮最有价值的交付**：它把"下次再漏"从"希望记得"变成"CI 会红"。后两个元测试还顺带抓出了我上一轮没点到的同类漏项（第五、六处投影站点；`TURN_ATTEMPT_EXHAUSTED` 未登记）与作者自查出的 3 个错误码漏项（`ACCOUNT_PURGE_IN_PROGRESS` / `ACCOUNT_PURGE_NOT_DRAINED` / study 的 `RATE_LIMITED`）。

---

## 4. 新发现（1 项，Minor 残留）

### 残留：链接落后文档 **>1 个版本**时，"无节点提交"无法推进它

| 项 | 内容 |
|---|---|
| 位置 | `backend/memory/services/memory_service.py`：`previous_active` 仍只取 `row["active"] and int(row["memory_version"]) == active_version - 1`；`backend/memory/graph/maintenance.py` 的迁移处理器只刷新**索引投影**，**不碰 `memory_graph_links`** |
| 我的探针（真实 PG） | ① v1 建档并写入 KG 映射 → link `(n7711, v1, True)`；② 模拟迁移把文档版本 +1（**不动 links**）→ 文档 v2、link 仍 v1；③ 一次无图谱信息的 `frontmatter_patch` 提交 → 文档 v3，`[links] [('n7711', 1, True)]`，**`stale links (version != doc version) = 1`** |
| 后果 | 该主题的 `_dual_write_kg`（要求 `active=true AND memory_version=:version`）**永久** `no_graph_mapping`——因为后续的无节点提交都因为"差 2 版"而找不到它，无从推进 |
| 为什么上一轮没让作者修 | 我上一轮把它列为"未验证/需要作者确认"而非确定发现，作者按清单修完了确定项。**没有新增回归**：`node_ids` 分支已加了"任意历史行"回退，同版本场景也已修好；只有这一条 >1 版滞后路径仍留着 |
| 影响面与触发条件 | 需要同时满足：该 mastery 建过 KG 映射 + 跑过 `migrate_markdown_schema_v2`（或任何把版本跳过一格的路径）+ 之后只有无节点提交 + `memory_kg_dual_write_enabled` 开启。结果只是"该主题的 KG overlay 不再更新"，长期记忆侧与索引投影都正常 |
| 建议修复 | 把 `previous_active` 的版本谓词去掉（`list_links_for_memory` 已经一次取齐全部行，改成"active 的最高版本"即可），或在迁移处理器里刷新链接版本 |
| 严重度 | **Minor**（不阻塞启用；宽一点读或加一次迁移刷新即可闭合） |

---

## 5. 判定

| 问题 | 回答 |
|---|---|
| 修复是否到位？ | **到位**。上一轮 17/17 全部修复，含我判为 Critical 的多节点 KG 回归。 |
| 是否引入新问题？ | **未发现**。Critical 回归已消除；四个元测试经我变异验证确实能防复发；工作树在我全部操作后 `git status` 干净。 |
| 可用性判断 | 三个 Critical 的原问题 + 由修复引入的 Critical 回归**均已解除**。剩 1 个 Minor 残留（KG 链接 >1 版滞后），建议随下次改动顺手闭合，**不构成启用阻塞**。 |
| 与前两轮对比 | 第一轮"一条都不能启用" → 第二轮"1 个 Critical 回归 + 4 个 Important 残留" → 本轮"**0 Critical / 0 Important / 1 Minor**"。 |

**建议（非阻塞）**：把 `previous_active` 的 `active_version - 1` 谓词放宽为"active 的最高版本"，或让迁移处理器与索引投影一起刷新链接版本；并给 `test_mastery_kg_links_and_projection.py` 补一条"迁移跳版后无节点提交仍能推进链接"的用例，纳入既有的"投影站点"元测试体系。

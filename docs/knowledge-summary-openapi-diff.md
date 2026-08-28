# 知识总结 OpenAPI 预期差异清单

> 对应：`docs/knowledge-summary-implementation-plan.md` §15、§19、§23。
>
> 状态：Phase 2 已实现 PATCH/DELETE 并更新 OpenAPI 快照；生成与评审接口仍留待后续阶段。

## 路由分阶段增量

### Phase 1：只读总结接口

- `GET /api/v1/knowledge-summaries`
- `GET /api/v1/knowledge-summaries/topic-groups`
- `GET /api/v1/knowledge-summaries/stats`
- `GET /api/v1/knowledge-summaries/{summary_id}`
- `GET /api/v1/knowledge-summaries/{summary_id}/sources`

### Phase 2：编辑和删除接口

- `PATCH /api/v1/knowledge-summaries/{summary_id}`
- `DELETE /api/v1/knowledge-summaries/{summary_id}`

### Phase 4：生成与评审接口

- `POST /api/v1/conversations/{thread_id}/turns/{turn_id}/knowledge-summary-generations`
- `GET /api/v1/conversations/{thread_id}/turns/{turn_id}/knowledge-summary-generation`
- `GET /api/v1/knowledge-summary-generations/{generation_id}`
- `POST /api/v1/knowledge-summary-generations/{generation_id}/dismiss-review`

## 预期 Schema 增量

- `KnowledgeSummaryListResponse`、`KnowledgeSummaryListItem`；
- 大主题、统计、详情、消息级来源聚合 DTO；来源聚合 DTO 使用 `source_turn_id`（第一版等于 `turn_id`），不公开消息级 `source_id`；
- `KnowledgeSummaryPatchRequest` 与带 `item_id` 的章节编辑 DTO；
- 手动生成、当前 Turn Generation、Generation 状态、逐条 dismiss review DTO；
- §16 冻结的 `KNOWLEDGE_SUMMARY_*` PublicError 代码及 409 `current_version` 扩展。

## Feature Flag 对 OpenAPI 的约束

- 主开关关闭：不暴露任何知识总结用户路由；
- 主开关开启、generation 关闭：暴露只读、编辑、删除和既有 Generation 状态读取；不暴露生成 POST；
- generation 开启：在主开关开启的前提下暴露手动生成和 dismiss review；
- 自动生成开关只控制自动 enqueue，不改变已挂载的用户 API。

## 快照更新门禁

每个实际挂载路由的阶段完成后，必须执行：

```bash
UPDATE_OPENAPI_SNAPSHOT=1 .venv/bin/python -m pytest tests/contract -q
```

提交前必须人工审查 `tests/contract/openapi_snapshot.json`：确认没有新增 SSE 事件、没有误暴露关闭态路由，且所有请求 Schema 保持 `extra="forbid"`。

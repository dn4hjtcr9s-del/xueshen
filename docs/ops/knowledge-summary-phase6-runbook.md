# KnowledgeSummary Phase 6 运维与灰度 Runbook

适用范围：Conversation 域的知识总结（KnowledgeSummary）上线、自动生成熔断、retention
维护和生产运维 CLI。本手册只处理知识总结；账号 purge 主链路尚未接入 Conversation，不能把
本功能的表清理误判为账号删除完成。

## 1. 上线前置条件

1. Conversation 迁移链已升级到知识总结版本；**不回滚 migration**。
2. `CONVERSATION_KNOWLEDGE_SUMMARY_ENABLED`、
   `CONVERSATION_KNOWLEDGE_SUMMARY_GENERATION_ENABLED` 和
   `CONVERSATION_KNOWLEDGE_SUMMARY_AUTO_GENERATE_ENABLED` 均保持默认 `false`。
3. §22.6 的离线评测集至少 200 条，双人独立标注及仲裁记录完整；以下命令能通过结构校验：

   ```bash
   .venv/bin/python -m evals.run_knowledge_summary_eval \
     --dataset evals/knowledge_summary_cases_v1.jsonl \
     --model-snapshot <明确的模型快照> \
     --prompt-version knowledge_extract_v1 \
     --extract-schema-version knowledge_extract_schema_v1 \
     --merge-schema-version knowledge_merge_schema_v1 \
     --normalizer-version knowledge_canonical_v1
   ```

4. 真实预测评测报告的所有门槛均通过：候选 Precision `>= 0.90`、来源不支持条目率
   `<= 1%`、自动合并 Precision `>= 0.95`、保护章节事故 `= 0`、非数学误保存率
   `<= 1%`、重复一致率 `= 100%`、端到端成功或 `no_change` 率 `>= 98%`。
5. 若任一门槛未达到，**不得灰度或扩大自动生成**。豁免报告必须写明 owner、原因、风险、
   补救措施和到期日期。
6. 在向真实用户开放、可能写入生产数据前，必须完成 Conversation account purge 主链路接入。
   当前 Phase 2 记录为：`Conversation account purge integration = deferred`；“账号删除后无
   知识总结残留”验收项未通过。

## 2. 关闭态部署

先部署 migration 和代码，再保持三级开关关闭：

```dotenv
CONVERSATION_KNOWLEDGE_SUMMARY_ENABLED=false
CONVERSATION_KNOWLEDGE_SUMMARY_GENERATION_ENABLED=false
CONVERSATION_KNOWLEDGE_SUMMARY_AUTO_GENERATE_ENABLED=false
```

关闭态含义：用户 API、前端导航和知识总结 Generation Worker 不启用；Conversation Worker 的
retention maintenance 仍可运行，生产 CLI 仍可读取历史表并执行受审计的维护操作。关闭态不得
创建自动或手动 Job。

验证：

```bash
uv run python -m backend.conversation.cli.knowledge_summary show-runtime-control
uv run python -m backend.conversation.cli.knowledge_summary run-retention
```

第二条命令默认 dry-run，不写数据库。

## 3. 灰度顺序

严格按顺序推进，每一步至少观察一个完整业务日并留存报告：

1. **只读页面**：仅将 `CONVERSATION_KNOWLEDGE_SUMMARY_ENABLED=true`，保持 generation/auto
   为 `false`。已有总结可读写；当前 Generation 和单 Job 的 GET 端点保留，所有手动生成、
   重试和 dismiss POST 端点隐藏。
2. **手动生成**：对内部测试账号将 `CONVERSATION_KNOWLEDGE_SUMMARY_GENERATION_ENABLED=true`，
   仍保持 `CONVERSATION_KNOWLEDGE_SUMMARY_AUTO_GENERATE_ENABLED=false`。配置固定模型名、
   Structured Outputs 白名单、30 秒超时和日 token budget。
3. **内部账号自动生成**：只对受控内部账号开启自动入队。观察质量、费用、队列、冲突和
   dead letter；确认 manual/manual_retry/manual_refresh/ops_retry 保留至少一个执行槽。
4. **小流量自动生成**：逐步扩大自动生成覆盖率，每一步都复核 §22.6 门槛和本节指标。

任何 Feature Flag 前后端发布错配都应安全降级：前端隐藏入口、回首页，并仅一次
`console.warn("knowledge_summary.feature_unavailable", ...)` 记录诊断；不得持续请求已隐藏接口。

## 4. 指标、告警和初步诊断

`/metrics` 中重点观察下列无高基数标签指标：

- `conversation_knowledge_summary_jobs_total{trigger,status}`
- `conversation_knowledge_summary_queue_depth{status,trigger}`
- `conversation_knowledge_summary_job_duration_seconds{trigger,status}`
- `conversation_knowledge_summary_model_calls_total{purpose,result,model}`
- `conversation_knowledge_summary_model_tokens_total{purpose,direction}`
- `conversation_knowledge_summary_candidates_total{disposition}`
- `conversation_knowledge_summary_item_mutations_total{section,action}`
- `conversation_knowledge_summary_merge_total{decision}`
- `conversation_knowledge_summary_review_total{reason}`
- `conversation_knowledge_summary_api_requests_total{route,status}`
- `conversation_knowledge_summary_auto_suspensions_total{reason}`
- `conversation_knowledge_summary_retention_operations_total{operation,result}`

必须配置 backend owner 和 on-call 的告警：5 分钟 dead letter 超过 5、pending/retry_wait
最老 Job 超过 5 分钟、Structured Output 非法率超过 2%、quote 校验失败率超过 1%、
`needs_review` 连续 1 小时超过 20%、自动成功率（排除 `no_change`）低于 90%、唯一冲突
重算率持续超过 5%。

自动熔断阈值固定为：自动 pending/retry_wait 队列 `>= 5000`、最老自动 Job `> 600` 秒、
5 分钟模型调用失败率 `>= 50%` 且调用数 `>= 20`、或 UTC 日 input+output token 超过
`CONVERSATION_KNOWLEDGE_SUMMARY_DAILY_TOKEN_BUDGET`。

## 5. 自动生成熔断处置

发生告警或自动熔断时，按此顺序操作：

1. 确认指标和日志，禁止在日志、审计参数或工单中复制问题、回答、总结正文、quote、Prompt、
   token 或密钥。
2. 查询 singleton：

   ```bash
   uv run python -m backend.conversation.cli.knowledge_summary show-runtime-control
   ```

3. 如尚未暂停，人工暂停自动生成；默认 dry-run，生产执行必须使用 operator 与 ticket：

   ```bash
   uv run python -m backend.conversation.cli.knowledge_summary suspend-auto \
     --reason-code <稳定原因码> --apply --operator <on-call> --ticket-id <变更单>
   ```

4. 暂停后保留手动生成、手动重试、只读、编辑、删除 API 和 retention；不要清空 pending Job。
   自动 Job 不再 enqueue、claim 或退避唤醒。
5. 定位根因并修复：队列/数据库容量、模型错误、token 预算或上游延迟。确认指标恢复后由
   backend owner/on-call 人工恢复：

   ```bash
   uv run python -m backend.conversation.cli.knowledge_summary resume-auto \
     --apply --operator <backend-owner> --ticket-id <变更单>
   ```

系统**不得自动恢复**自动生成。暂停和恢复均必须写入 `knowledge_summary_admin_audit`。

## 6. Production CLI 安全操作

入口固定为：

```bash
uv run python -m backend.conversation.cli.knowledge_summary <command> ...
```

只读命令：

```bash
... list-dead-letter-jobs [--user-id <uuid>]
... validate-knowledge-summary-consistency [--user-id <uuid>]
... show-runtime-control
```

修改命令默认 dry-run；只有同时提供 `--apply --operator --ticket-id` 才会写入并记录审计：

```bash
... retry-generation --generation-id <uuid> [--apply --operator <name> --ticket-id <id>]
... rebuild-summary-counts [--user-id <uuid>] [--include-deleted] \
  [--apply --operator <name> --ticket-id <id>]
... run-retention [--apply --operator <name> --ticket-id <id>]
```

`retry-generation` 仅允许 dead_letter，或当前 checkpoint 未变化且不属于 Thread 删除、账号删除、
来源变化的 cancelled Job；`needs_review` 和已 scrub payload 一律拒绝。它创建新的 `ops_retry`
Job，保留原 Job 证据，不修改原 Job。

`rebuild-summary-counts` 仅从消息级 source 重算 distinct Turn、available distinct Turn 和
distinct message 计数；默认跳过 deleted summary，不修改 item 内容或来源状态。

## 7. Retention

每小时 maintenance 运行一次。保留期从终态/删除裁决的 UTC 时间起计算：模型调用 payload
14 天 scrub、Generation payload 30 天 scrub、deleted summary 30 天后物理删除、无 pending
review 的 Generation 180 天后物理删除。tombstone 保留至账号 purge；本期不做 tombstone 删除。

需要人工执行时先 dry-run，再使用 `run-retention --apply`。任何单条 FK/锁异常只影响当前批次，
不得回滚已成功的 scrub；查看
`conversation_knowledge_summary_retention_operations_total{operation,result}` 和 Worker 日志确认结果。

## 8. 回滚

1. 先关闭 `CONVERSATION_KNOWLEDGE_SUMMARY_AUTO_GENERATE_ENABLED`；
2. 如需继续收缩，再关闭 `CONVERSATION_KNOWLEDGE_SUMMARY_GENERATION_ENABLED`；
3. 最后关闭 `CONVERSATION_KNOWLEDGE_SUMMARY_ENABLED`，前端隐藏导航；
4. 保留已生成数据、pending Job、tombstone 和 migration；Worker 重启并重新开启 generation 后可
   按 lease/retry 语义继续；
5. 只有经过明确数据治理审批的操作才能清理总结，不能把功能回滚当作数据删除。

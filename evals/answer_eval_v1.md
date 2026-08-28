# Answer Eval v1 数据说明

## 专用账号

- 用户名：`answer_eval_2026`
- 固定 `user_id`：`2be72e49-22bc-5635-bddb-810acfa32791`
- 本地凭据：`.local/answer_eval_account.json`（权限 `0600`，不进入版本库）
- 账号状态：`active`

该账号只用于 Answer Eval。100 条 Case 共享这个账号已经写入的长期记忆和知识图谱状态。

## 固定 Fixture

- Fixture：`evals/answer_eval_fixture_v1.json`
- Fixture ID：`answer-eval-shared-state-v1`
- learner 文档：1 份
- mastery 文档：8 份
- index 文档：1 份
- Memory 检索索引：9 条
- Memory 与图谱活动链接：8 条
- 用户图谱 Overlay：8 条

Fixture 使用固定版本和固定更新时间。评测过程中不得写入或重算这些状态。

## Case 数据集

- Case：`evals/answer_eval_cases_v1.jsonl`
- Schema：`evals/answer_eval_schema_v1.json`
- Manifest：`evals/answer_eval_manifest_v1.json`
- 总数：100
- 单轮：60
- 多轮：40
- 覆盖教材：21 本
- 复用 Retrieval Case：70 条

多轮 Case 使用固定的 `canonical_prefix` 历史。每个 Case 使用独立 Conversation Thread，
只隔离对话历史，不改变共享 Memory 和图谱状态。

## 后续评测只读约束

运行 Answer Eval 时必须同时满足：

1. 开启 Memory 读取；
2. 关闭 `CONVERSATION_MEMORY_SUBMIT_ENABLED`；
3. 不启动 Conversation Outbox Publisher；
4. 不提交 ConversationEvidence；
5. 不处理显式记忆写入；
6. 不调用图谱状态写接口；
7. 每个 Case 使用新 Thread；
8. 每个 Case 前后核对 Fixture、Memory 文档和图谱 Overlay 哈希或版本；
9. 任意 Memory/图谱写入都视为评测执行错误，而不是普通 Case 失败。

## 重新准备

如需在同一数据库中幂等重建账号和固定状态：

```bash
PYTHONPATH=. .venv/bin/python evals/seed_answer_eval_account.py
```

如 Retrieval Case 或教材 chunk 发生明确版本升级，可重新生成 100 条 Case：

```bash
.venv/bin/python evals/build_answer_eval_cases.py
```

上述两个命令只准备数据，不调用模型、不运行 Answer Eval。

## 当前状态

2026-08-18 已完成账号、Fixture 和 100 条 Case 的创建。本轮没有运行测试套件、
ConversationGraph、模型调用或 Answer Eval。

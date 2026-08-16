"""Study 学习编排域（docs/study-plan-push-implementation-plan.md v1.2）。

- Memory 负责长期语义记忆与知识图谱；
- Study 负责可执行计划的排期、任务状态、进度、每日推荐与真实学习统计；
- 独立 study 数据库与迁移链（study_alembic.ini），LangGraph 编排走
  study_checkpoints schema；跨域读写一律通过 Gateway/Outbox，不做跨库事务。
"""

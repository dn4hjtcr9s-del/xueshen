# 长期记忆 consolidation（summary_consolidate_v1）

你在为用户**整体重写**长期记忆的预注入摘要 `memory_summary.md`。这是一次"通读全部文档
后的统一整理"，不是增量追加：你必须基于给定的全部输入重新判断，而不是把新信息贴到旧
摘要后面。

## 输入

- `learner`：学习者档案（偏好 / 目标 / 计划）。
- `mastery[]`：该用户全部掌握档案（topic_key、name、description、aliases、关键词、
  overview、understood、difficulties、review_advice、链接目标、版本、更新时间）。
- `existing_summary`：当前摘要（可能为空）。
- `batch_diff`：本批证据带来的变更摘要（哪些主题被创建/更新）。
- `degraded`：为 true 时表示输入超出预算，**只允许重写"主题路由"段**，
  `user_profile` 与 `stable_preferences` 必须原样返回 `existing_summary` 中对应内容。

## 输出（严格 JSON）

三个摘要段，逐字遵守下列约束：

1. `user_profile`（用户画像，≤350 词）
   - 只写**稳定的**学习偏好、目标、沟通习惯；
   - **保守推断**：一次性印象、单次表现、助手说过的话都不能落地成画像；
   - 没有把握就少写，写不出来就返回空列表。

2. `stable_preferences`（稳定偏好）
   - 跨主题、**会改变未来回答方式**的可执行偏好（如"讲解时先给结论再给推导"）；
   - 短 bullet，激进去重；同一件事只留一条。

3. `topic_routes`（主题路由）
   - **每个 mastery 主题恰好一条**，不允许遗漏、不允许编造不存在的主题；
   - `topic_key` 必须与输入中的 topic_key 完全一致；
   - `status_line` 是一句话掌握状态（不含正文细节），`proficiency` 取
     `learning` / `proficient` / `expert` 之一。

## 治理输出

4. `alias_merges`（近义主题归并候选）
   - 只有**确认指向同一实体**时才提（如"椭圆"与"椭圆形"同义；
     "圆锥曲线"与"椭圆"是父子关系，**不是**归并对象）；
   - 每条给出 `canonical_topic_key`（保留哪个）、`merged_topic_keys`（并入哪些）、
     `reason`（一句话依据）；
   - 不确定就不要提——错并会让用户原有命名消失。

5. `keywords`（判别性检索词）
   - 为每个主题生成 3–8 个**未来会用到的检索词**：概念名、标准术语、常见错误模式、
     典型题型说法；
   - 不使用整句、不使用泛词（"数学""学习""问题"这类词无检索价值）；
   - `memory_id` 必须是输入中存在的 memory_id。

6. `conflicts`（批内冲突）
   - 只登记**同一事实互相矛盾**的情况（如"已掌握导数" vs "导数仍有困难"）；
   - 给出涉及的 memory_id 与一句话描述；
   - 不要在这里复述"新证据比旧证据新"——更新鲜者优先由服务端裁决，
     你只负责把并列冲突标出来。

## 纪律

- 不生成 user_id、不生成新的 topic_key（除非是 `alias_merges` 里的 canonical 引用）、
  不生成文件路径、不生成 SQL、不生成稳定 ID。
- 不把助手讲解当作用户已掌握的事实。
- 输入里没有的信息一律不写；宁可留空也不要推测。

# 回答 Agent 提示词建设方向建议（草案 v0.1）

> 状态：方向建议稿，供评审；未改动任何线上提示词。
> 对象：`backend/conversation/graph/prompts.py` 中的 `ANSWER_SYSTEM_PROMPT`
> （含流式追加段 `ANSWER_STREAMING_FOLLOWUPS_INSTRUCTION` 与记忆工具追加段
> `ANSWER_MEMORY_TOOLS_INSTRUCTION`），回答节点见 `graph/nodes/answer.py`。

## 0. 现状盘点

当前 `ANSWER_SYSTEM_PROMPT` 只覆盖两类内容：

1. 上下文分区说明（CURRENT_USER_REQUEST / CONVERSATION_CONTEXT / LONG_TERM_MEMORY /
   RAG_EVIDENCE / ANSWER_CONTRACT / RETRIEVAL_DECISION / ANSWER_RULES）；
2. 证据与引用纪律（8 条规则：只引用给定 Citation、不伪造教材、记忆不证明教材事实、
   skip/clarify 行为、PLANNER_UNAVAILABLE 降级说明等）。

**缺失的一整块是"教学人格与教学法"**：它是谁、怎么教、怎么解题、怎么追问、
怎么对待学习者，全部没有约定。模型每次回答的教学风格取决于其自身默认行为，
表现为：风格漂移、直接泄答案、追问随机、难度不适配。这正是本轮建设要补的。

## 1. 身份与角色（Identity）

当前提示词第一句只是"你是数学学习对话助手"，没有任何身份词。建议补：

- **品牌身份**：自称 **xueshen**（与项目名一致），统一称谓，不自称"老师/AI 助手/
  聊天机器人"等造成身份漂移的表述；
- **角色定位**：xueshen 是数学学习助教 / 陪练（不是答题机器，不是阅卷老师）；
- **项目现状注入（关键）**：身份段里写明 xueshen 当前覆盖的教材与知识点范围，
  让 agent 能如实回答"你能教我什么 / 有没有 XX 书"。当前真实覆盖（2026-10 核查）：
  - 教材目录（`knowledge_graph/教材目录.md`）：七年级、八年级、九年级数学（上下册）、
    高中数学（A 版）、大学数学·数学分析（第三版）上册（复旦数分），共 21 个分册、
    133 个章节（四级标题）；
  - 知识图谱（`knowledge_graph/数学知识科技树关系图.md`）：127 个有章节细目的核心
    节点 + 6 个无细目专题节点，节点间为直接先修/强支撑关系边，已同步进
    `knowledge_graph_nodes/edges` 注册表；
  - RAG 语料：以 `rag.corpus_versions` 中 active corpus 为准（`rag.books`），
    运行期可能与上面静态列表不一致。
- **范围写法必须动态化**：书目与知识点清单**不要硬编码进提示词字符串**——语料入库
  后就会漂移。建议由服务端在 `build_answer_view` 里新增 `corpus_scope` 分区，从
  `ActiveCorpusVocabularyLoader`（已有，从 rag.books/chunks 去重生成）+ KG 注册表统计
  动态生成，提示词只写"如实按 CORPUS_SCOPE 回答覆盖范围，范围外的书明确说暂未收录"；
- **口吻基调**：严谨、耐心、鼓励式；不居高临下、不敷衍。

## 2. 教学法核心（Pedagogy）——本项目最差异化的部分

建议作为独立分区 `TEACHING_PRINCIPLES` 写入，核心决策是**默认不直接倾泻完整解答**：

- **诊断先行**：用户说"不会做/看不懂"时，先定位卡点（概念不清？条件不会转化？
  计算失误？），再针对性讲解；
- **提示阶梯（hint ladder）**：对解题请求，默认按"方向提示 → 关键一步 → 完整解答"
  分层揭示；用户明确要求"直接给完整解答"时直接给；
- **苏格拉底式引导**：对概念性困惑，优先用反问引导用户自己得出结论，再给确认；
- **分层讲解结构**：直觉解释 → 严格定义/定理 → 一个最小例子 → 常见误区；
- **学术诚信边界**：识别疑似"直接抄作业"请求（只有题面、无任何自己的尝试），
  先要求用户说出自己的思路或卡点，而非代做。

## 3. 学习者画像适配（Personalization）

记忆域已有 mastery / learner / recommendations，但回答侧没有"怎么用"的约定：

- 回答深度、严格度、节奏随用户掌握度调整（薄弱知识点：多铺垫前置、放慢；
  已掌握：可直接进入变式）；
- 引用用户历史时明确标注"根据你的学习记录…"，与教材事实严格区分
  （现有规则 6/7 已有基础，需与个性化写法对齐）；
- 记忆为空时的冷启动话术统一（现有规则 7 只规定了"说明无法判断"，未规定怎么接住对话）。

## 4. 数学表达与输出结构规范

- **公式排版**：行内/独立公式的 LaTeX 约定（与前端渲染能力对齐，需先确认前端支持
  `$...$` / `$$...$$` 还是其它）；
- **术语一致性**：优先采用 RAG 证据中的教材术语与记号；无证据时用通行标准术语并
  在首次出现时定义记号；
- **解题结构**：结论先行或步骤编号（1)(2)(3)），证明题区分"证"与"解"；
- **长度控制**：按问题类型给默认篇幅（概念问答 ≠ 大题解答），避免每题都写成长文；
- **Markdown 使用**：允许的结构（标题/列表/公式），禁止的结构（如表格滥用）。

## 5. 引导提问 / followups 的教学化设计

现有规则只说"最多 3 条追问"，追问内容完全随机。建议给 followups 定义**分层模板**：

- 巩固层：一个同型变式小题（"要不要试一道同类型的题？"）；
- 溯源层：指向前置概念（"这题用到的 XXX 需要我展开吗？"）；
- 拓展层：指向自然推广（"想知道这个结论在 … 情形下还成立吗？"）；
- 与掌握度联动：对薄弱点优先巩固层，对已掌握优先拓展层；
- 追问必须与本轮内容强相关，禁止通用废话（"还有什么问题吗？"这类不出现在 followups）。

## 6. 交互意图分支（Answer Modes）

`retrieval_decision.answer_mode` 已有 direct / memory_assisted / rag，建议提示词
显式定义各模式的回答策略：

- direct（题面已给全）：进入解题/讲解流程（§2 教学法主导）；
- rag（教材证据）：先给教材结论与出处，再做解释与例子；证据缺口按现有规则 5 处理；
- memory_assisted（个人状态）：以学习记录为唯一证据，不越界推断；
- clarify：最少必要澄清，一次只问最关键的歧义点。

## 7. 情感与动机支持

- 容错反馈范式：用户答错时先肯定思路中正确的部分，再指出具体断点；
- 避免否定性措辞（"这很简单""你怎么还不会"类表达显式禁止）；
- 对挫败表达（"我就是学不会"）先共情再给可执行的下一步。

## 8. 工程化建设（与提示词内容同等重要）

1. **Prompt 分区化与版本号**：参考 REWRITE 的分区写法，把 ANSWER 拆成
   IDENTITY / TEACHING_PRINCIPLES / OUTPUT_FORMAT / FOLLOWUPS / EVIDENCE_RULES 等
   常量段，加 `ANSWER_PROMPT_VERSION`，变更可追溯、可灰度；
2. **Few-shot 示例**：REWRITE 有 4 个示例，ANSWER 一个都没有——为解题、概念讲解、
   记忆为空、clarify 各配 1 个正反示例；
3. **评测基线**：建 golden 集（典型用户提问 30~50 条），对身份一致性、提示阶梯、
   followups 质量、引用纪律做回归评测，纳入 `tests/`（纯单测，Fake 网关即可覆盖
   提示词拼装层）；
4. **可配置化**：教学策略开关（如"直接给答案 vs 提示阶梯"的默认档）放 settings，
   不写死在提示词里，便于 A/B；
5. **指标埋点**：followups 点击率、追问轮次、用户满意度信号，反哺提示词迭代。

## 9. 建议落地顺序

1. 身份 + 教学法分区（§1 §2）：收益最大、风险最低，纯提示词改动；
2. followups 分层模板（§5）：直接改善现有输出；
3. 输出结构规范（§4）：需先确认前端公式渲染能力；
4. 画像适配（§3）与意图分支（§6）：依赖记忆数据质量，第二批；
5. 评测基线（§8.3）尽早建立，否则后续改动无法回归。

## 10. 明确不改的部分

- 证据/引用纪律（现有规则 1–8）保留原语义，只做分区搬家，不改行为；
- `REWRITE_SYSTEM_PROMPT` / `EVIDENCE_SYSTEM_PROMPT` 本轮不动；
- 追加段机制（流式 followups 标记、记忆工具段）保持现有拼接方式不变。

## 11. 附录：Answer Agent 工具现状核查（2026-10-18）

**结论：记忆工具有但没默认开，知识图谱工具完全没有。**

1. **长期记忆工具（已实现，默认关闭）**：`memory.search`（关键词定位，只回元数据）
   / `memory.read`（读正文片段）已实现，工具循环最多 6 轮
   （`graph/nodes/memory_tool.py`、`gateways/openai.py memory_tool_specs()`）。
   由 `MEMORY_TOOLS_ENABLED` 控制，**默认 False**（`backend/settings.py:699`）；
   开启时追加 `ANSWER_MEMORY_TOOLS_INSTRUCTION` 段。按仓库约定，启用需单独批准。
2. **记忆数据本身是被动注入的**：无论工具开关，recall_memory 节点每轮把学习上下文
   （learner / mastery / graph_states / recommendations）经 LONG_TERM_MEMORY 分区
   注入（`graph/nodes/memory.py` 走 `memory_gateway.build_learning_context`）。
   所以"用户掌握状态"agent 看得到（服务端代查），但不能自己按需深挖。
3. **知识图谱工具（不存在）**：全 backend 没有任何面向 conversation 域的图谱查询
   工具（无 graph_search / graph_query）。图谱结构（127+6 节点、先修边）躺在
   memory 域的 `knowledge_graph_nodes/edges` 注册表里，answer agent 够不到：
   它无法主动回答"学 XXX 之前需要先学什么"这类依赖图谱结构的问题——除非
   LONG_TERM_MEMORY 恰好注入了相关 graph_states，但那只有用户自己的掌握状态，
   不含图谱结构本身。
4. **对提示词建设的含义**：
   - 短期：身份段的"覆盖范围"用 §1 的 `corpus_scope` 动态注入解决，不依赖工具；
   - 中期：建议新增只读图谱工具（如 `graph.lookup_node` / `graph.prerequisites`），
     挂在现有 memory_tool 循环上复用 6 轮预算——这是"先修路径推荐"类功能的
     前置条件，单独立项，不在本轮提示词改动范围内；
   - 提示词里必须写清楚"工具不可得时的口径"：被问到先修路径而图谱工具未启用时，
     只能依据教材目录与证据回答，不得编造先修关系。

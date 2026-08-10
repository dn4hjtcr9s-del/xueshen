# 项目技术预研记录

## ocr

选取vlm作为ocr的主要方式，公式更紧凑，识别效果更好。但是有公式粘连和长数字分隔的问题，这些问题反而pipeline更好。因此做结果的兜底处理。

{
  "model_version": "vlm",
  "language": "ch",
  "is_ocr": true,
  "enable_formula": true,
  "enable_table": true
}

ocr的上传方式是按书籍隔离、每 180 页切片上传

### ocr清洗

Stage 1 · 块级过滤
丢弃 page_header / page_footer / page_number / page_aside_text（已有标记，直接沿用）。
水印正则清单删除：仅供个人学习使用[^。！？]{0,20}、中国银行股份有限公司.*、中国教育科技研究院 等；只做子串擦除，擦除后块仍有正文则保留（解决粘连问题）。
装饰图剔除：位于前 5 页且面积占比大的图（封面）、宽高比近 1:1 的小图（二维码），按 bbox + 页位启发式判定。

Stage 2 · 文本规范化
CJK 正文：半角逗号/句号/分号在中文语境下统一为全角；折叠多余空白；数学段（$...$ 内）不做 NFKC，防止破坏 LaTeX。
书名元数据清洗：从文件名提取规范书名，并打 level 标签（小学/初中/高中/大学，21 本书可按编号规则自动映射）。

Stage 3 · 结构修复
跨页段落合并：块末无终止标点（。！？：；"）且次页首块不以标题/列表模式开头 → 合并。
标题层级重建：按模式识别 第X章→h1、第X节→h2、一、→h3、1.→h4，输出真正的 TOC 树。
篇章分段：打 front_matter（封面/CIP/前言/目录）、body、back_matter（附录/习题答案）标签；默认清洗产物只保留 body + 附录，前言答案单独存放不删。

Stage 4 · 公式修复
跨行公式配对合并：相邻两个 equation_interline 左半有未闭合 \{/(、右半有多余闭合 → 合并，合并后用括号平衡校验确认。
LaTeX 空格规范化：\w _ \w → \w_\w 这类 token 间多余空格折叠。
重判风险标记：现有 possible_digit_fragmentation / suspicious_fraction 基本降权为"提示"；修复后仍不平衡的公式，回退策略是引用该公式区域的页面截图路径（images/ 里有原图），留给后续 VLM 重识别，而不是硬改。

Stage 5 · 表格与图片
删 10 个空表格；表格 HTML 保留并挂 caption。
正文图保留占位符但带元数据：![figure](path)<!-- page=N, bbox=... -->，方便以后批量做 VLM 图注

## memorymangeragent

### 为什么不使用langgraphserver

- 核心复杂度是业务规则，不是 Graph 托管
  你现在最重要的是先设计清楚：
  哪些证据值得长期保存
  什么时候合并 mastery/*.md
  用户纠正如何覆盖模型推断
  如何解决并发修改
  如何删除和恢复记忆
  如何维护 Markdown 和索引的一致性
  Agent Server 不会替你解决这些问题。

- MemoryManagerGraph 不是对话 Agent
  它本质上更像一个后台任务处理器：
  输入学习证据
  → 提取候选记忆
  → 判断长期价值
  → 生成修改计划
  → 提交事务
  它通常不需要前端流式输出，也不需要持续保存一个长对话线程，因此 Agent Server 的一部分能力暂时用不上。

- 你的长期记忆不放在 Agent Server Store
  你的长期记忆来源仍然是：
  Markdown + PostgreSQL 索引 + 图谱用户状态
  即使采用 Agent Server，也仍然需要独立的 MemoryService，因此 Agent Server 不能替代你的核心存储层。

- 自托管 Agent Server 会增加基础设施
  官方当前的自托管部署模式涉及 PostgreSQL、Redis、API Server 和 Queue Worker；Standalone Server 还涉及相应的 LangSmith 部署许可配置。
  在项目早期，这可能比一个数据库任务表加 Worker 更复杂。

### 怎么做失败恢复与任务调度

[[retry.md]]



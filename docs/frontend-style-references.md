# 前端样式参考清单 —— MemoryManagerGraph（数学记忆 + 知识图谱 + AI 对话）

> 检索日期：2026-08-26。只评估**前端视觉样式**，不涉及功能与交互逻辑评估。
> 范围：学习/教育平台 + AI 对话为主，知识图谱可视化产品为辅。

## 过线标准（收录门槛）

1. **设计语言统一且现代**：不是 UI 框架默认皮（一眼 Bootstrap/Material 默认主题的直接淘汰）
2. **排版、留白、配色讲究**：有明确的字体层级和视觉呼吸感，达到「高级 SaaS / 获奖产品」水准
3. **可验证**：有在线官网 / Demo / 活跃仓库可直接查看当前样式
4. **活跃维护**：开源项目近两年仍在迭代；闭源产品官网为当前版本

以下 15 个全部通过该标准；被淘汰的典型反例（供校准）：Moodle、Open edX、Open WebUI、Logseq、NextChat —— 功能强但视觉平庸或同质化。

---

## 一、学习 / 教育平台（重点参考）

### 1. Brilliant ⭐ 数学互动学习的视觉天花板
- 官网：https://brilliant.org （闭源）
- 风格：「Playful Premium」——大圆角、糖果色渐变、微动效驱动的互动课件；把数学概念做成了可拖拽的视觉玩具
- 值得看：课程卡片、互动题组件的即时反馈动效、Koji AI 家教页的排版
- 与我们的关联：数学教学内容的最佳呈现范式，没有之一

### 2. Mathigon / Polypad ⭐ 开源、获奖的互动数学画布
- 官网：https://mathigon.org ｜ 画布直达：https://polypad.amplify.com
- GitHub：https://github.com/mathigon （开源，MIT）
- 风格：绘本级插画 + 教科书排版（侧边注释、渐进展开），Polypad 是 50+ 种数学教具组成的拖拽画布，无需登录即可体验
- 与我们的关联：数学教材「正文 + 互动组件」混排的标杆；曾被 Amplify 收购时官方评价「visually compelling」

### 3. Synthesis Tutor ⭐ AI 数学家教的极简温暖风
- 官网：https://www.synthesis.com （闭源）
- 风格：大面积留白 + 柔和暖色 + 手绘感插画；源自 SpaceX 校内学校 Ad Astra 团队，官网本身就是顶级 Landing Page
- 与我们的关联：AI  tutoring 产品如何做到「高级感 + 亲和力」并存

### 4. LearnHouse ⭐ 最现代的开源 LMS
- GitHub：https://github.com/learnhouse/learnhouse ｜ 官网：https://www.learnhouse.app
- 技术栈：Next.js + Tailwind + Radix UI（前端与我们同为 React 系，可直接参考组件实现）
- 风格：Linear 式深色点缀的现代 SaaS 风，课程页、编辑器、看板都干净利落
- 与我们的关联：开源 LMS 里设计最不像「教育软件」的，社区/课程/AI 三合一结构与我们相近

### 5. Frappe Learning（Frappe LMS）干净极简的开源 LMS
- GitHub：https://github.com/frappe/lms ｜ 官网：https://frappe.io/learning
- 风格：浅色极简、大留白、细描边卡片；因嫌弃 Moodle 界面混乱而生，克制是最大特点
- 与我们的关联：「少即是多」的教育界面参考

---

## 二、AI 对话 / RAG 应用（重点参考）

### 6. LobeChat ⭐ 开源 AI 聊天 UI 的精致度标杆
- GitHub：https://github.com/lobehub/lobe-chat（81k+ stars）｜ 官网：https://lobehub.com
- 组件库：https://ui.lobehub.com （Lobe UI，90+ 组件全部可在线实时预览）
- 风格：媲美一线商业 SaaS 的聊天界面——精致的亮/暗双主题、流式 Markdown + KaTeX 数学公式 + Mermaid 渲染、细腻的动效
- 与我们的关联：对话域的直接参照物；**自带 KaTeX 公式渲染，数学场景刚需**；React 组件可直接借鉴

### 7. Scira ⭐ 极简 Answer Engine，免登录直接体验
- GitHub：https://github.com/zaidmukaddam/scira（10k+ stars，AGPL）｜ 在线：https://scira.ai
- 技术栈：Next.js + Tailwind + shadcn/ui
- 风格：极简搜索框居中型，回答卡片 + 引用源展示非常干净，shadcn 作者和 Vercel CEO 都公开夸过
- 与我们的关联：RAG 问答结果 + 引用来源 的呈现范式；打开即可体验，无需注册

### 8. Anything-LLM 桌面级精致的 RAG 对话应用
- GitHub：https://github.com/Mintplex-Labs/anything-llm（53k+ stars，MIT）｜ 官网：https://anythingllm.com
- 技术栈：前端 Vite + React（**与我们前端技术栈完全一致**）
- 风格：深色侧栏 + Workspace 卡片式布局，设置页层级清晰，是「功能密集但不乱」的范本
- 与我们的关联：文档对话 + 多工作区管理的信息架构参考

### 9. Khoj AI 第二大脑（文档对话 + 记忆）
- GitHub：https://github.com/khoj-ai/khoj（34k+ stars，AGPL）｜ 官网：https://khoj.dev ｜ 在线：https://app.khoj.dev
- 风格：暖米色系、衬线标题字，「人文感 AI」路线，在一众冷色 AI 产品里辨识度极高
- 与我们的关联：定位最接近我们（个人知识库 + 长期记忆 + 对话），可直接注册体验

---

## 三、知识图谱 / 可视化知识库（辅助参考）

### 10. Heptabase ⭐ 视觉化学习的白板标杆
- 官网：https://heptabase.com （闭源）｜ 公开 Wiki：https://wiki.heptabase.com
- 风格：卡片 + 无限白板 + 左侧标签栏，米白底 + 柔和阴影，「为深度学习设计」的克制美学
- 与我们的关联：知识点卡片 ↔ 白板 ↔ 图谱 的三层结构与我们「记忆 + 图谱」高度同构

### 11. Capacities 对象化笔记的画廊美学
- 官网：https://capacities.io （闭源）
- 风格：Pinterest 式画廊视图 + 精致的对象卡片，Graph View 干净不杂乱；Product Hunt 4.8 分
- 与我们的关联：图谱视图（Graph View）如何做得好看又不乱的直接参考

### 12. Anytype 开源、深色高级的本地优先知识库
- GitHub：https://github.com/anyproto ｜ 官网：https://anytype.io
- 风格：深色高级感 + 细腻的图谱视图动画，对象关系网的视觉呈现是开源里最好的之一
- 与我们的关联：开源 + 图谱视图 + 对象模型，三点全中

### 13. AFFiNE 开源 Notion + Miro 合体
- GitHub：https://github.com/toeverything/AFFiNE（71k+ stars）｜ 官网：https://affine.pro ｜ 在线 Demo：https://app.affine.pro
- 风格：文档与无边画布（Edgeless）无缝切换，插画和微动效投入很大，官网本身就是设计参考
- 与我们的关联：文档 + 画布双模式融合的开源实现

### 14. Kosmik 无限画布知识采集
- 官网：https://www.kosmik.app （闭源）
- 风格：Framer 级的官网动效 + 画布内嵌浏览器/PDF 阅读器，视觉密度高但秩序感强
- 与我们的关联：教材内容（PDF/网页）在画布上组织的参考

### 15. Kumu 关系图谱可视化老牌标杆
- 官网：https://kumu.io （闭源）｜ 文档：https://docs.kumu.io
- 风格：纯粹的节点-连线图谱美学，装饰系统（按属性着色/缩放节点）是图谱可视化的教科书
- 与我们的关联：知识图谱「装饰规则」设计的直接参照；学术界和 UNDP 都在用

---

## 使用建议

| 我们的模块 | 首选参考 | 备选 |
|---|---|---|
| 对话界面（含数学公式渲染） | LobeChat | Scira、Anything-LLM |
| 教材内容呈现 | Mathigon | Brilliant |
| 知识图谱视图 | Capacities / Anytype | Kumu、AFFiNE |
| 学习路径 / 课程结构 | LearnHouse | Frappe LMS、Brilliant |
| 整体产品气质 | Heptabase | Synthesis、Khoj |

⭐ = 建议优先打开体验的 5 个：Brilliant、Mathigon、Synthesis、LearnHouse、LobeChat、Scira、Heptabase（共 7 个，均为各领域视觉标杆）

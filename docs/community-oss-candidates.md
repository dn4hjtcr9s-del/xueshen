# 社区功能开源项目调研与移植方案（候选对比）

> 调研日期：2026-08-26 · 更新：2026-08-28（同步主计划 v3.9 口径；本文档定位为调研留档，非执行依据）
> 目标需求（MVP）：用户可发帖（纯文字必填 + 最多 3 张配图，仅主帖，回复不带图）；可申请创建贴吧式专有话题社区（如"心理咨询社区""搞笑社区"），管理员审核开通；放弃现有社区核心实现（板块/帖子/回复），保留周边集成（Outbox → Memory 学习证据链、通知、契约测试）；社区图片/附件存七牛云 Kodo（后端代理上传，**图片不进入记忆**），文本数据留 PostgreSQL；非商业用途（入选项目均为宽松协议）。
> 全局前提：**项目尚未上线，community 库无生产数据**——不做双表双写；迁移方式为新增 `0002` 扩展迁移（ALTER 现有表 + 建新表 + 清空旧业务数据），仅保留 4 个 seed 板块。
> MVP 明确不做：置顶/隐藏/吧主治理、富文本编辑器、回复带图、服务端图片预览/缩略图/灯箱/拖拽粘贴（保留浏览器端 objectURL 本地预览）、举报、搜索（均列入第二阶段）。
> 现状基线：FastAPI + SQLAlchemy 2.0 + PostgreSQL（community 独立库）+ React/Vite；现有社区为 4 个固定板块、纯文本帖、平面回复、点赞、软删除。
> **本文档为调研与选型留档，非执行依据；字段级设计、状态机、API 清单、测试矩阵等执行细节以 `docs/community-rebuild-plan.md`（v3.9）为唯一执行依据。**

---

## 一、结论先行

**选定路线（B-1）：以 FlaskBB（BSD-3）为代码捐献方移植进现有 FastAPI 代码库**，附件表结构参考 bbs-go（MIT），"用户申请建吧 + 管理员审核"工作流自研（任何开源项目都没有现成实现，2 张新表（board_applications + attachments）+ boards 扩展（0002 迁移加列）+ 5 个建吧相关接口）。理由：

1. 唯一与我们同语言（Python）且同为 SQLAlchemy ORM 的成熟论坛，模型层可近乎直接搬进 `backend/community/`，改写为 async FastAPI 风格；
2. 完全保留现有周边集成（outbox→memory、通知、契约测试只需更新快照），符合"周边保留、只换核心"的要求；
3. BSD-3 协议，可随意 copy 修改；
4. 前端布局/交互参考 bbs-go（同为 React），视觉沿用现有纸张风格；MVP 只抄骨架（板块宫格、帖子流、发帖表单），不引入其 CSS 框架与 TipTap 编辑器；数据层通过 `0002` 迁移扩展现有表并清空旧业务数据。

**备选路线（A-1）：独立部署 Lemmy（AGPL-3.0）**——功能上最贴合贴吧模式（用户自建 community、图文帖、投票、版主体系、联邦可关闭），但对简化 MVP 来说过重：独立账号体系、社区数据在 Lemmy 库中（Memory 证据链需额外适配器）、多一个 Rust 服务的运维成本。已否决，仅留档。

---

## 二、候选项目详细对比

### 路线 A：可独立部署的现成社区服务

| 项目 | 栈 / 协议 | 贴吧式自建社区 | 图文发布 | 与我们集成难度 | 维护状态（2026-08） |
|---|---|---|---|---|---|
| **Lemmy**<br>https://github.com/LemmyNet/lemmy | Rust + Actix + PostgreSQL / **AGPL-3.0** | ✅ 用户自建 community（subreddit 模式），版主体系完整 | ✅ 内置 pict-rs 图片服务 | 高：独立账号（0.19+ 支持外部 OAuth）、独立库、联邦功能需关闭 | 活跃，13.7k stars |
| **NodeBB**<br>https://github.com/NodeBB/NodeBB | Node.js + Redis/Mongo/PG / GPL-3.0 | ❌ 板块仅管理员创建（有第三方插件但不成熟） | ✅ | 高 | 活跃 |
| **Discourse**<br>https://github.com/discourse/discourse | Ruby on Rails + PG + Redis / GPL-2.0 | ❌ category 仅管理员创建 | ✅ | 高：资源占用大（1GB+ 内存），重运维 | 非常活跃 |
| **Flarum**<br>https://github.com/flarum/flarum | PHP (Laravel) / MIT | ❌ tag/板块仅管理员创建 | 需插件 | 高 | 活跃 |
| **Postmill**<br>https://gitlab.com/postmill/Postmill | PHP (Symfony) + PG / zlib | ✅ 用户自建 forum | ✅（链接帖缩略图） | 高：PHP 栈、社区小 | 原版 GitHub 已归档，GitLab 上低速维护 |
| **bbs-go**<br>https://github.com/mlogclub/bbs-go | Go (Iris) + React(v4) / **MIT** | ❌ 节点管理员创建（支持父子节点）；另有"动态/推文"流 | ✅ 图片 + 附件，支持本地/多存储 | 中：中文社区、中文文档、有管理后台 | 活跃（v4.3.9，2026-03）<br>演示：https://bbs.bbs-go.com |
| **paopao-ce（泡泡）**<br>https://github.com/rocboss/paopao-ce | Go (Gin) + Vue3 + NaiveUI / **MIT** | ⚠️ 微博/推文模式 + 话题标签聚合，非独立板块 | ✅ 文字/图片/视频，LocalOSS/MinIO/S3 多存储 | 中：清新 UI、中文项目、Docker Compose 一键起 | 活跃<br>演示：https://paopao-demo.vercel.app |

**路线 A 小结**：真正"用户自建社区"开箱即用的只有 **Lemmy** 和 Postmill；其余产品板块均为管理员创建，不满足"用户申请建吧"的核心诉求。路线 A 整体不符合"copy 进项目 + 简化 MVP"的方向，已全部否决。

### 路线 B：可移植/参考进我们代码库的项目

| 项目 | 栈 / 协议 | 可 copy 的内容 | 移植适配成本 |
|---|---|---|---|
| **FlaskBB** ⭐ 已选定<br>https://github.com/flaskbb/flaskbb | Python **Flask + SQLAlchemy** / **BSD-3** | Forum/Category/Topic/Post 数据模型、附件（Attachments）、发帖/回帖/删除基本规则——同为 SQLAlchemy，模型层近乎可直接改写为我们 async 风格 | **低**：改写的主要是视图层（Flask → FastAPI 路由），业务逻辑和表结构直接搬 |
| **Spirit**<br>https://github.com/nitely/Spirit | Python Django / MIT | 评论树、通知的模型设计 | 中：Django ORM/信号耦合深，只能参考设计 |
| **Misago**<br>https://github.com/rafalp/Misago | Python Django + React / GPL-2.0 | 功能最全（分类/附件/审核队列）；React 前端组件可参考 | 高：Django 深度耦合，GPL 有传染性顾虑，只参考不抄 |
| **bbs-go** ⭐ 前端参考已选定 | Go（后端）+ React 19（前端 v4）/ MIT | 前端页面布局与交互（对照重写）；后端附件服务、敏感词表结构仅参考设计（Go，不抄代码） | 前端：抄骨架重写组件；后端：仅参考 |
| **paopao-ce**（仅留档） | Go / MIT | LocalOSS 本地图片存储实现、Feature 套件化配置思路 | 参考设计，不抄代码 |

**源码版本固定**：Phase 0 执行时 clone 两个仓库的最新 release tag 并记录 commit SHA 入实施计划附录 D；允许参考目录：FlaskBB `flaskbb/forum/`、`flaskbb/utils/`，bbs-go `web/`（布局）与 `server/` 附件设计；禁止复制：FlaskBB templates/插件系统/auth，bbs-go 后端 Go 代码；第三方 LICENSE 收录进 `docs/licenses/`，移植文件头加来源注释。

### 各项目 UI 预览地址（留档）

| 项目 | UI 预览地址 | UI 风格 |
|---|---|---|
| bbs-go ⭐ | https://bbs.bbs-go.com | 中文现代社区，含管理后台 /admin（MVP 布局参考对象） |
| paopao-ce | https://paopao-demo.vercel.app | 清新微博流，NaiveUI |
| Lemmy | https://lemmy.ml | 类 Reddit，功能性强但朴素 |
| NodeBB | https://community.nodebb.org | 现代论坛，实时感强 |
| Discourse | https://meta.discourse.org | 成熟精致，重 |
| Flarum | https://discuss.flarum.org | 简洁现代 |
| Misago | https://misago-project.org（站内有 demo 入口） | 经典论坛 |
| FlaskBB | https://forums.flaskbb.org | 经典论坛（Jinja 模板，我们反正重写前端） |

> 已确认：前端视觉沿用我们现有 `styles.css` 纸张风格，只抄 bbs-go 的布局与交互骨架；不引入 Tailwind/shadcn/TipTap。

---

## 三、移植方案（MVP 范围）概要

> 本节为范围速查；字段级设计、状态机、API 清单、测试矩阵以 `docs/community-rebuild-plan.md`（最新版本）为唯一执行依据。

### 3.1 功能映射（MVP）

| 需求 | 来源 | 落点 |
|---|---|---|
| 板块（含用户申请创建） | FlaskBB `Forum/Category` 模型 + 自研申请流 | boards 表 0002 扩展（新增 `created_by/updated_at/post_count` 等列；slug/status 为 0001 已有）；新增 `board_applications`（pending/approved/rejected）；**申请只写申请表，审核通过才创建板块** |
| 发帖（纯文本，必填非空） | FlaskBB `Topic/Post` 模型 | posts/replies 服务层按新能力适配改写（表结构不动，保留现有幂等、游标分页设计） |
| 发帖配图（≤3 张，仅主帖） | FlaskBB Attachments 思路 + bbs-go 附件设计参考 | 新增 `attachments` 表（状态机 uploaded→attached→deleted/orphaned）+ `POST /uploads`（**后端代理上传**至 Kodo，URL 由 CDN 域名 + storage_key 动态生成） |
| 回复（纯文本，无图）/点赞/resolve/删除 | 现有实现已具备，按新模型适配 | 保留接口契约（路径/方法/状态码/错误码/游标/UUID 主键不变）；**响应增加冻结字段，其中 `attachments` 为必返数组（恒返回，无附件为 `[]`）** |
| 用户申请建吧 + 管理员审核 | 自研 | `POST /boards/applications`、`GET /boards/applications/mine`、`/admin/board-applications` 系列；审核结果走站内通知；管理员由 `COMMUNITY_ADMIN_USER_IDS` 环境变量判定 |
| ~~吧主治理（置顶/隐藏/删帖）~~ | FlaskBB 版主权限模型 | **MVP 不做**，第二阶段再移植 |

### 3.2 对我们项目的改动面（MVP）

- **community 库**：新增 `0002` 扩展迁移（走 `community_alembic.ini` 链，含 `downgrade()`）：ALTER boards/notifications/idempotency 三表现有结构 + 新建 board_applications、attachments；开发/测试库业务数据清空（TRUNCATE 含 boards 七张表）后纯 INSERT 重插 4 个冻结 UUID 的 seed 板块；
- **新旧切换**：`COMMUNITY_V2_ENABLED`（默认 false）仅开发/测试期控制新路由挂载；上线固定 true，稳定一个版本后删除 flag 与旧代码；回滚 = git revert + alembic downgrade；
- **API**：现有路由契约按上述兼容范围保留，新增 uploads / applications / admin 接口（静态路径先于 `/boards/{slug}` 注册，管理接口统一 `/admin/` 前缀避免路由歧义）→ `UPDATE_OPENAPI_SNAPSHOT=1` 更新快照并 review；
- **保留不动**：Outbox、ActivityPublisher（forum_post/forum_reply 证据链，**只提交文本、帖子证据=标题+正文**）、notifications、限流、内容安全校验、PublicError 信封；
- **前端**：6 个逻辑子视图（社区首页、板块详情、帖子详情、发帖、申请建吧、管理员审核——均为 `page==="community"` 视图内的 PageKey 子状态导航，**不引入 URL 路由**，主计划 D43），布局对照 bbs-go、视觉纸张风格；
- **配置**：`backend/settings.py` 增加 `COMMUNITY_STORAGE_BACKEND` + `KODO_*`（AK/SK/Bucket/Region/CDN 域名）；生产强制 kodo 且缺配置启动抛错，local 仅开发/测试。

### 3.3 风险与注意

1. FlaskBB 是同步 SQLAlchemy，移植时需改写为我们现有 async/会话模式；模型层改动小，服务层逐函数改写；
2. 图片内容安全：现有 `content_safety` 只校验文本；MVP 做格式级校验（MIME+Pillow+5MB+图片炸弹防护），EXIF 清除与内容级审核放第二阶段（可选七牛内容审核 API）；
3. 新社区实现挂 feature flag 灰度，符合"启用必须等批准"约定；
4. 若未来要重启路线 A（Lemmy），Memory 证据链需新增"Lemmy API → ActivityEvidence"适配器，且需身份映射，工作量明显大于路线 B。

---

## 四、来源

- FlaskBB 仓库与 README（功能、BSD-3 协议、维护状态）：https://github.com/flaskbb/flaskbb ，PyPI https://pypi.org/project/FlaskBB/ ，Snyk 维护状态评估（2026-07）：https://security.snyk.io/package/pip/FlaskBB
- Misago 仓库（功能清单）：https://github.com/rafalp/Misago
- bbs-go 仓库、中文 README 与发布记录（v4.3.5/v4.3.9 附件与多存储能力、MIT 协议、v4 前端为 React 19 见其 `web/package.json`）：https://github.com/mlogclub/bbs-go ，https://github.com/mlogclub/bbs-go/releases ，更新日志 https://bbs-go.com/docs/changelog.html
- paopao-ce 仓库 README（MIT、架构、LocalOSS、演示地址）：https://github.com/rocboss/paopao-ce
- Lemmy 项目与文档（community 模式、pict-rs 图片、联邦可关、LocalOnly communities v0.19.4）：https://github.com/LemmyNet/lemmy ，https://join-lemmy.org/news/2024-06-07_-_Lemmy_Release_v0.19.4_-_Image_Proxying_and_Federation_improvements
- Postmill（用户自建 forum、zlib 协议、GitHub 镜像已归档）：https://github.com/neuroradiology/Postmill
- NodeBB / Discourse / Flarum 对比参考：https://github.com/mlogclub/bbs-go （同类产品对比表）

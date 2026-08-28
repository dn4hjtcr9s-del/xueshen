# 社区重建实施方案（执行级规格）：FlaskBB 移植 × bbs-go 交互 × 七牛 Kodo

> 版本：v3.9（执行级冻结） · 2026-08-28
> 读者对象：项目全体成员（含非技术读者）。本文把每个决策的来龙去脉讲清楚：参考了哪个开源项目、项目地址、为什么参考它、具体参考什么、为什么不选其他方案。
> 状态：待开工。执行顺序见「十一、分阶段实施计划」；「七、关键机制设计」「八、API 契约」「十二、测试矩阵」为执行级冻结物，实施者不需要也不允许自行做架构决定。
> 变更记录：
> - v3.0：MVP 简化。v3.1：补齐 18 项执行细节。v3.2：第四轮 32 项 + 服务器 2核4G 方案。v3.3：第五轮 15+10 项修正。
> - v3.4：第六轮 19 项：`body` 字段统一、0002 扩展迁移、错误码 HTTP 映射表、并发竞态、限流合同等。
> - v3.5：第七轮 10 项：以 0001 迁移文件逐行为基准修正数据字典、审核事务顺序 D38、settings 合同 §7.15、migrate 链 §7.16 等。
> - v3.6：第八轮 16 项：清库含 boards + seed 纯 INSERT、约束名解析规则、新表完整 DDL、幂等 hash 输入、调用路径清单 §7.17、reviewer_id 移出 DTO、响应模型逐字核实、maintenance 事务边界、local 相对 URL、前端 PageKey 状态导航（§九重写）等。
> - v3.7：第九轮 17 项 + 1 同步项，**继续按代码核实纠偏**：① settings 合法范围收紧为"只能调小不能超过 MVP 冻结上限"（D10/§7.15，消除"配 4 张图撑爆 position CHECK"矛盾）；简介允许空（范围约束的是上限配置值本身）；② **`COMMUNITY_IDEMPOTENCY_CONFLICT` 改回 422**（代码核实：现有实现与测试均为 422，D13 契约兼容优先）；③ **读接口现为必需认证（代码核实）**——新增可选认证依赖精确行为（D46）；④ 游标绑定 status（沿用现有 route/filter 绑定机制），改 status 必须弃游标否则 422 `COMMUNITY_CURSOR_INVALID`；⑤ orphan 清理**同事务删除对应幂等记录**（消除"幂等记录指向已删附件"窗口）；⑥ 删除重试时序表（1 初次+3 重试=4 次，1h/4h/12h，第 4 次失败进终态）；⑦ 错误码目录补全（`COMMUNITY_BOARD_DISABLED`/`COMMUNITY_CURSOR_INVALID` 入表；新增 code 全部注册进 `COMMUNITY_ERROR_CODES` + 异常类；唯一约束→code 映射表；管理接口用 `ADMIN_REQUIRED` 403 社区域 code）；⑧ Idempotency-Key 二分冻结：缺失 → `COMMUNITY_CONTENT_INVALID`，格式非法 → `INVALID_PAYLOAD`（Header pattern 在路由前拦截，代码核实）；⑨ 40MP 改为**显式 width×height 计算主动拒绝**（Pillow MAX_IMAGE_PIXELS 仅作底层双保险，其 warning/2x 语义不满足"超过即拒"）；⑩ Kodo 崩溃孤儿对象窗口明确接受（D48）；⑪ updated_at 服务层维护、写入点列入 §7.17；⑫ 申请提交时即查 boards 冲突（409 立即反馈），pending 冲突语义统一；⑬ original_filename 清洗规则冻结；⑭ KODO_CDN_DOMAIN 只收裸域名（启动校验）+ Region 合法列表；⑮ 依赖加版本上限 + uv.lock 同步 + `.env.example` 列入 P1 交付物（D49）；⑯ 附录 D 表头冻结、"最新 tag"= Phase 0 执行当日获取；⑰ P5 部署记录表格式/禁止进入 5.7 清单/生产 DB 角色命名默认值/certbot staging→正式命令冻结。
> - v3.9：第十一轮 10 项 + 2 处候选文档残留，全部按代码/配置核实定稿：① **删除并发计数原子性定稿**（改"读态判定 + 条件 UPDATE 状态转换成功才扣计数"唯一机制；锁顺序 posts→replies/attachments→boards；不用 GREATEST 兜底，CHECK 违反即数据异常抛错转人工）；② **Maintenance 拆两阶段**（旧清理保持单事务、附件清理每附件独立 session/事务、单附件失败不扩散、扫描 SQL 完整条件 + LIMIT、各类各自 500 条、不用 SKIP LOCKED）；③ 上传超时矛盾消除（"最坏数十秒"改为"默认 40s/上限 420s 明确接受"+ nginx uploads location `proxy_read_timeout 480s`；取消/断开语义冻结：to_thread 不可取消、事务回滚、已传对象走补偿删除、local 孤儿文件接受；"一个 Kodo 对象"承诺仅限正常并发场景）；④ **空白 Authorization 定稿**：可选认证依赖先 strip()，空白按无凭证→匿名（写接口行为不变仍 401）；⑤ **P5 镜像与 Compose 定稿**：开发用 Dockerfile/compose 不动、`docker-compose.prod.yml` 为新增文件、生产 FROM 行在 P5 改抄录补丁版并提交、node 沿用 24 系补丁版、服务名沿用开发实际名（修正 `conversation-publisher` 笔误）、patch tag 即可（digest 仅 certbot）；⑥ §7.17 九类写入点改字段级 SQL 表达（每行显式 `updated_at=now()`）；⑦ **hidden 板块读取行为冻结**（hidden 板块及其帖子对外一律 404，唯一例外发帖 409 BOARD_DISABLED；读查询 join boards 补 `status='active'`）；⑧ `COMMUNITY_UPLOAD_FAILED` 实现定稿（异常类加构造参数实例级 retryable、SDK 异常完整映射表、612 仅删除视为成功、HTTP 唯一 502、前端只按 code 不读 retryable）；⑨ **0002 破坏性迁移硬门禁**（upgrade 开头行数闸门：发现非 seed 数据 → RuntimeError 失败退出，不允许静默清空）；⑩ **P5 migrate job 定稿**（各库属主角色 + `*_MIGRATE_DATABASE_URL`、`scripts/migrate-all.sh` wrapper、逐链失败即 exit 1、sync 仅在 memory 链成功后执行）；⑪ 候选文档两处表述残留修正（URL 式页面名改 PageKey 子视图、"仅新增可选字段"改"attachments 必返数组"）。
> - v3.8：第十轮 10 项，全部按代码核实定稿：① §六速查表"1 张新表"残留修正为 2 张新表；② **匿名认证实现合同冻结**（D46 扩展为 D46a–d：不改 `AuthContext.user_id` 可空、新增返回 `AuthContext | None` 的可选依赖；凭证优先级按 `verifier.py` 现行为准=Authorization 优先；空 Authorization 头=无凭证；匿名限流仅 IP 桶、无 IP 用固定 `ip:unknown` 桶；读模型 `viewer_user_id` 改 `UUID | None`）；③ 游标 payload 按 `shared/cursor.py` 实际结构重写（status 属于 `normalized_filters`，#14 固定 `"mine"`，#15 `status=all` 写 `"all"`，切换即 422）；④ Kodo Region 改"冻结值 + P1 核对不一致**停工会审**"，列表写入配置校验测试作不可变验收项；⑤ 上传幂等事务边界定稿（事务保持打开、Kodo 调用不发 SQL、并发同键 PG 唯一约束天然等待语义、local 同边界）；⑥ §7.17 改逐 SQL 写入点清单（9 类写入路径全覆盖）；⑦ original_filename 清洗伪代码 + 5 组样例（控制字符与 Cf 格式字符删除、删除后再 trim、按 Python 字符截断）；⑧ CDN 域名校验收紧（逐 label RFC 规则、≥2 标签、禁端口/IDN、大写自动转小写）；⑨ **重复删除语义矛盾消除**（按代码核实：已删除+作者=200 幂等成功、非作者=404、并发双删由确定性 event_id + ON CONFLICT 去重，§7.14 错误残留"0 行→404"删除）；⑩ P5 镜像版本冻结规则（禁一切浮动 tag，执行时抄录值进 5.6 部署记录表）。
>
> **全局前提（冻结）：项目尚未上线，community 库无生产数据。** 新旧切换不做双写双表；迁移清空旧业务数据、仅保留 seed 板块；"契约兼容"只约束接口形状。
> **代码核实结论（依据，逐行核对过 0001 迁移、契约 DTO、错误码、路由层、认证共享模块与前端 App）**：现有请求字段为 `body`；真实表名 `community_*`、主键 `*_id`；**posts/replies 作者列为 `user_id`**；boards 列为 `slug varchar(64) UNIQUE / name varchar(80) / description varchar(500) / sort_order DEFAULT 0 / status CHECK('active','hidden') / created_at`，无 updated_at/created_by/post_count；幂等表 CHECK 仅两值、UNIQUE `(user_id, operation, idempotency_key)`；通知表 `event_type` 两值 CHECK、`post_id/reply_id NOT NULL`、`body varchar(300)`、`dedupe_key UNIQUE`；**DTO 全部 `extra="forbid"`、不暴露内部 user_id、作者视图仅 `{display_name}`、通知 post_id/reply_id 契约层已可空**；**路由真实响应**：boards=`{items}` 无分页、posts=Page 信封、详情=`{post, replies: Page}` 信封、发帖 201=CommunityPostDetail、回帖 201=CommunityReplyView、like/unlike/resolve/delete=200 `{"status":"ok"}`、通知 read/read-all=`{unread_count}`；**错误码全集 7 个**（`contracts/errors.py`）：NOT_FOUND(404)/BOARD_DISABLED(409)/POST_CLOSED(409)/CONTENT_INVALID(422)/**IDEMPOTENCY_CONFLICT(422)**/CURSOR_INVALID(422)/RATE_LIMITED(429, retryable)；**读接口（boards/posts 等）当前依赖 `get_auth_context` 为必需认证**（无匿名路径）；游标已有 route/sort/filter/expiry 绑定机制（`backend/shared/cursor.py`，不绑定 principal）；**Idempotency-Key Header 带 `pattern` 校验：格式非法在路由前被 FastAPI 拦为 422 INVALID_PAYLOAD，缺失才进 `_require_idempotency_key`**；shared `require()` 权限失败抛 `AUTH_FORBIDDEN`(403)；幂等模式 = 同事务"先抢键后写业务，败者重放"，hash=canonical_json(规范化后值)；hash 公式：帖子 `sha256("标题：{title}\n正文：{body}")`、回复 `sha256(body)`；validate 不拒绝 HTML；purge 逐帖 mark_post_deleted + source_deleted outbox；Outbox 事务化；unanswered = `solved_reply_id IS NULL`；`unread_count` = 当前用户全部未读数；settings 现有 `community_*` 系列（维护间隔 3600/批量 500/幂等保留 7 天/正文上限 19500 等）；maintenance 与 ActivityPublisher 为 memory-api 进程内 asyncio 任务；conversation/community/study 路由按配置条件挂载；**前端为 `App.tsx` 内 `useState<PageKey>` 状态导航，无 URL 路由，登录表单在 profile 视图**。

---

## 一、背景：为什么要重建社区

本项目（MemoryManagerGraph / xueshen-math）是一个"数学教材长期记忆 + 知识图谱 + 对话/社区"系统：

- **后端**：FastAPI（Python 3.13）+ SQLAlchemy 2.0 + PostgreSQL，单进程多域，Alembic 分链迁移；
- **前端**：React 19 + Vite + 自研 `styles.css`（纸张色编辑风格），**单页内部状态导航（无 URL 路由）**；
- **部署目标**：阿里云 ECS（当前 2核1.6G/40G，**部署前升级到 2核4G**，附录 B），当前仅有本地开发环境，无生产部署。

现有社区已实现：4 个固定板块、纯文本发帖、平面回复、点赞、标记已解决、软删除、站内通知、事务化 Outbox → Memory 学习证据链。**短板**：① 板块写死，用户不能创建专有社区（贴吧模式）；② 帖子不能发图片；③ 读接口当前要求登录，不支持游客浏览。

**重建方式**：现有核心实现按 FlaskBB 模型改造扩展；**保留**周边设施——事务化 Outbox → Memory 证据链、通知、限流、内容安全、PublicError 信封、契约测试体系。**MVP 原则**：最小可用闭环，治理/富文本/高级图片一律后置。

## 二、MVP 功能清单（验收时的功能列表）

**做：**

1. 板块浏览：板块列表 + 板块内帖子流（保留 4 个 seed 板块）；**未登录可浏览（只读）**——新增可选认证依赖（D46）；
2. **申请建吧**：登录用户申请（吧名 + slug + 简介 + 理由）→ 管理员审核 → 结果站内通知；申请人即吧主（仅 `created_by` 身份记录，MVP 无权限）；
3. **发图文帖**：纯文本正文（必填）+ 最多 3 张配图（单图 ≤ 5MiB，jpeg/png/webp），按选择顺序展示；
4. 回复（纯文本，无图）、点赞、标记已解决（resolve）、删除自己的帖子/回复（软删除）；
5. 站内通知（回复、审核结果）；6. 图片全部存**七牛 Kodo**（后端代理上传），**不进入** Memory 证据。

**MVP 明确不做（第二阶段）**：置顶/隐藏/吧主删帖等治理（不建 `board_moderators`、无置顶入口；`pinned` 列保留但无任何设置入口，恒 false）；编辑/恢复/草稿；富文本；回复带图、拖拽粘贴、灯箱、**服务端缩略图**；新申请通知管理员；封禁用户内容特殊展示；举报、敏感词库、板块关注、热门排序、全文搜索；**URL 自动识别为链接**；EXIF 清除；学习小组、打卡圈。（注：发帖表单的**浏览器端 objectURL 本地预览是做的**，与服务端预览/缩略图无关。）

## 三、选型：模仿谁、为什么、模仿什么

### 3.1 后端移植对象：FlaskBB ⭐
- **地址**：https://github.com/flaskbb/flaskbb （演示 https://forums.flaskbb.org ）
- **是什么**：Python Flask 经典论坛，2.7k stars，BSD-3 协议，仍在维护。
- **为什么选它**：与我们同语言同 ORM（Python + SQLAlchemy），模型可近乎直接搬进 `backend/community/`；功能覆盖 MVP；代码简单；BSD-3 无限制。
- **模仿什么（MVP）**：`flaskbb/forum/models.py` 三级模型与冗余计数；Attachments 附件模型；发帖/回帖/删除基本规则。
- **不抄什么**：前端（Jinja）、权限/版主体系、插件、私信、全文检索。

### 3.2 前端参考对象：bbs-go
- **地址**：https://github.com/mlogclub/bbs-go （演示 https://bbs.bbs-go.com ）
- **是什么**：国人社区平台，Go 后端 + React 19 前端（v4），MIT 协议。
- **为什么选它**：UI 好看且同为 React，布局可对照重写；MIT 无限制。
- **模仿什么（MVP）**：页面布局与交互骨架（首页板块宫格 + 帖子流、板块详情、帖子详情 + 配图 + 评论区、发帖表单）；后端仅参考附件服务设计。**注意：我们前端是状态导航单页，只抄布局/交互，不抄其 URL 路由结构**。
- **不引入什么**：Tailwind/shadcn/TipTap/react-router——保持 `styles.css` 纸张视觉 + 纯文本发帖 + 现有 PageKey 导航。

### 3.3 为什么没有选其他项目

| 项目 | 地址 | 落选原因 |
|---|---|---|
| Lemmy | https://github.com/LemmyNet/lemmy | 最像贴吧，但独立 Rust 服务 + 独立账号 + 数据在它库里，对简化 MVP 过重 |
| NodeBB | https://github.com/NodeBB/NodeBB | 板块只能管理员创建；Node.js 无法移植 |
| Discourse | https://github.com/discourse/discourse | 板块管理员创建；1GB+ 内存 |
| Flarum | https://github.com/flarum/flarum | PHP 栈，板块管理员创建 |
| Misago | https://github.com/rafalp/Misago | Django 耦合深；GPL 顾虑 |
| Spirit | https://github.com/nitely/Spirit | Django 耦合，不如 FlaskBB 贴合 |
| paopao-ce | https://github.com/rocboss/paopao-ce | 微博/推文模式，非独立板块社区 |
| Postmill | https://gitlab.com/postmill/Postmill | PHP 栈、社区小、维护慢 |

## 四、决策表（D1–D50，全部冻结）

| # | 决策 | 结论 |
|---|---|---|
| D1 | 移植路线 | FlaskBB 模型移植 + 自研建吧审核流；不独立部署第三方社区 |
| D2 | 协议 | FlaskBB BSD-3、bbs-go MIT；LICENSE 收录 `docs/licenses/`；来源注释按文件类型：Python `#`、TS/TSX/CSS `/* */`、Markdown `<!-- -->` |
| D3 | 建吧方式 | 用户申请 + 管理员审核；申请人即吧主（仅 `created_by` 记录，MVP 无权限、不可转让） |
| D4 | 治理功能 | MVP 不做 |
| D5 | 存储范围 | 仅社区域文件存 Kodo；对话/记忆域不进对象存储；图片不进 Memory 证据 |
| D6 | Kodo 桶策略 | 公开读 + CDN 域名；URL 动态生成；`KODO_CDN_DOMAIN` 只收裸域名（§7.9 启动校验） |
| D7 | 上传模式 | **模式 A：后端代理上传**；不配 Kodo CORS |
| D8 | 前端视觉 | 保留 styles.css；不引入 Tailwind/shadcn/TipTap/react-router |
| D9 | 部署形态 | 阿里云 ECS **升级 2核4G** + docker-compose + 域名 + certbot 容器 HTTPS |
| D10 | 图片限制 | 单图 ≤ **5MiB = 5,242,880 bytes**；每帖 ≤ 3 图（仅主帖）；jpeg/png/webp；**settings 只能调小不能超过以上冻结上限**（§7.15 范围约束） |
| D11 | 新旧切换 | **flag = 新功能曝光开关**，控制路由清单：uploads、boards/applications（含 mine）、admin/board-applications（含 approve/reject）、permissions、`GET /boards/{slug}`；核心接口永远走新实现；local-uploads 仅受 `backend=local` 控制；完整回滚 = git revert + alembic downgrade |
| D12 | 数据迁移 | 无生产数据；**新增 0002 扩展迁移**：ALTER 现有表 + 建新表 + 清空旧业务数据（含 boards）+ seed 重插 |
| D13 | 契约兼容 | 保留接口路径/方法/状态码/错误码/**HTTP 状态（含 IDEMPOTENCY_CONFLICT=422）**/游标/UUID 主键/**字段名（`body` 等）**不变；响应仅新增字段（`attachments` 恒返回，D37）；`resolve` 保留 |
| D14 | 纯文本规范 | 见 §7.5；正文必填非空；**接受任意字符串（含 HTML 字符），原样存储，前端永远纯文本转义渲染**（= 现有 validate 行为）；按字符数计 |
| D15 | 管理员判定 | `COMMUNITY_ADMIN_USER_IDS` + `require_community_admin`（社区域 403 code = `ADMIN_REQUIRED`）；生产启动强校验 |
| D16 | 通知范围 | 回复（沿用）+ 审核通过/拒绝（新增）；不做"新申请通知管理员"；异步不阻塞主事务 |
| D17 | Kodo 配置 | §7.9；local 仅开发/测试；生产强制 kodo 缺配置启动抛错 |
| D18 | 源码版本 | Phase 0 固定 tag + SHA 入附录 D（"最新 release"= Phase 0 执行当日获取，记录后冻结） |
| D19 | Outbox 语义 | 与业务写入**同一事务**插入，提交后仅异步发布；通知记录同事务 |
| D20 | 附件顺序 | `position` 绑定时按 `attachment_ids` 数组顺序从 0 连续编号，返回按 position ASC |
| D21 | slug 规则 | 2–30 位小写字母/数字开头结尾、中间单个连字符；trim + 自动转小写；保留字拒绝（应用层上限 30 < DB 列宽 64） |
| D22 | 吧名唯一 | trim + NFC；大小写不敏感（`lower(name)` 唯一索引）；DB 唯一索引兜底并发（应用层上限 20 < DB 列宽 80） |
| D23 | 前端管理员识别 | `GET /permissions` → `{is_community_admin}` |
| D24 | 前端上传交互 | 发布时并行上传 → 全部成功才提交；失败保留附件 ID 重试不重传；移除已选图不调删除接口 |
| D25 | 申请-板块关联 | `board_applications.board_id` 持久化，approve 同事务回填；通知链接用 slug |
| D26 | 板块排序 | seed 10/20/30/40（显式值）；**0002 将 `sort_order` 列默认值 ALTER 为 100**，新板块插入显式给 100；列表 `sort_order ASC, created_at ASC, board_id ASC` |
| D27 | seed 写入 | **0002 TRUNCATE 含 boards 后纯 INSERT 四个冻结 seed**（空表保证 UUID=冻结值；D41）；0001 的 `ON CONFLICT (slug)` 保持不动 |
| D28 | MIME 不一致 | Content-Type 白名单 + Pillow 可解码即接受，**以 Pillow 为准**（映射表见 §7.10） |
| D29 | SDK async 边界 | `KodoStorage.upload/delete` 为 async，SDK 同步调用 `anyio.to_thread.run_sync` 包裹 |
| D30 | 审核通知载体 | 仅 notifications 表记录（0002 新增 `board_slug` 列），无 Outbox 事件、不进 Memory |
| D31 | 物理命名 | **以 0001 实际为唯一基准**：表 `community_*`；主键 `board_id/post_id/reply_id/notification_id`，新表主键 `attachment_id/application_id`；正文列/API 字段均为 `body`；**posts/replies 作者列沿用现有 `user_id`**；新表用 `uploader_id/applicant_id/reviewer_id` |
| D32 | 迁移方式 | 不改 0001，新增 `0002_community_v2`：精确清单见 §7.2；**posts/replies/likes/outbox 结构不动** |
| D33 | 内容安全 | 沿用现有 `validate_post/validate_reply`（strip/非空/长度/控制字符白名单，**不拒绝 HTML**）；新字段复用同一工具；**无敏感词库**（第二阶段）；HTML 原样存储、前端纯文本转义渲染 |
| D34 | 限流合同 | 沿用现有 `rate_limit` 组模式，配额与 settings 字段见 §7.13；429 沿用 `COMMUNITY_RATE_LIMITED` |
| D35 | 并发语义 | 附件绑定/删除用条件 UPDATE 原子转换；审核用行锁 + 单语句状态转换（D38）；唯一约束异常映射 PublicError（§八映射表） |
| D36 | 图片处理 | MIME 映射表冻结（§7.10）；**像素超限主动判断**（width×height > settings 阈值 → 拒绝）；`Image.MAX_IMAGE_PIXELS` 启动时设同值作底层双保险；SpooledTemporaryFile（1MiB/64KiB） |
| D37 | attachments 响应字段 | **恒返回**（无附件 `[]`），响应 Schema required 数组；列表与详情一致 |
| D38 | 审核事务顺序 | **锁申请行 → INSERT board → 单语句 UPDATE 申请（四列一次写入）→ INSERT 通知 → COMMIT**；INSERT board 唯一冲突 → 整体回滚、申请保持 pending（§7.14） |
| D39 | settings 合同 | 新增配置精确字段名/环境变量/默认值/合法范围冻结于 §7.15；沿用现有命名模式 |
| D40 | settings 环境差异 | **默认值 = 开发/测试便利值；生产由启动强校验强制显式覆盖**（kodo 五项 + admin 名单）；`community_v2_enabled` 生产固定 true 是 P5 部署验收项，非 Settings 强校验 |
| D41 | 清库与 seed | 0002 TRUNCATE 七张表**含 community_boards**（单语句）→ 纯 INSERT 四 seed；验收断言 UUID=冻结值（§7.2） |
| D42 | local 模式 URL | **返回相对路径** `/api/v1/community/local-uploads/{storage_key}`；不读 Host/X-Forwarded-*；生产 kodo 模式返回 `https://{CDN域名}/{key}` 绝对 URL |
| D43 | 前端导航 | **沿用现有 `PageKey` 状态导航，不引入 URL 路由**；社区为 `page==="community"` 视图，内部子视图用组件 state；登录跳转 = `setPage("profile")`（§九） |
| D44 | 申请 DTO | **响应不返回 reviewer_id**（§6.6 DTO 规则）；列保留供 DB 审计；mine 与 admin 列表一致 |
| D45 | 幂等 hash 输入 | 逐接口冻结（§7.11）：全部基于**规范化后**值；`attachment_ids` **顺序敏感**、null/缺省/[] 统一为 [] |
| D46 | 可选认证 | 新增 `get_optional_auth_context`：**无凭证 → 匿名**（viewer_* 全 false）；**凭证无效/过期 → 401**（不静默降级）；匿名不初始化 Auth DB session；读接口与 local-uploads 使用（§八）；**实现合同细分为 D46a–d，见 §7.6（类型/凭证优先级/限流/读模型适配，按 `verifier.py` 代码核实冻结）** |
| D47 | 申请冲突时机 | **申请提交时即查 boards**（同名/同 slug → 409 `BOARD_NAME_CONFLICT` 立即反馈，不产生 pending）；自己已有 pending → `APPLICATION_DUPLICATE_PENDING`；他人 pending 占名由部分唯一索引兜底 → `BOARD_NAME_CONFLICT`（message 指明待审核占用） |
| D48 | Kodo 孤儿窗口 | 上传成功+DB 写入前崩溃 → 产生无法追踪的 Kodo 孤儿对象：**明确接受**（概率极低、成本可忽略；补偿删除覆盖可追踪路径）；不做意图表/对账（第二阶段评估） |
| D49 | 依赖锁定 | `qiniu>=7,<8`、`pillow>=11,<13`；**改 pyproject 必须同步 uv.lock；CI 与生产只允许按锁文件安装**（`uv sync --frozen`）；`.env.example` 同步更新列入 P1 交付物 |
| D50 | updated_at 维护 | **服务层维护**：所有写入路径显式 `updated_at=now()`（写入点清单 §7.17）；不建数据库 trigger |

## 五、总体架构（云服务器目标态，2核4G）

```text
用户浏览器
   │ HTTPS（你的域名，certbot 容器自动申请/续期 Let's Encrypt）
   ▼
┌─────────────────── 阿里云 ECS（2核4G/40G，docker-compose 编排）────────────────────┐
│ nginx 容器 ── HTTPS 终结、反代 API（SSE 长连接专项配置）、托管前端静态文件              │
│ certbot 容器 ── 证书申请/续期（webroot，共享卷）                                       │
│ memory-api ── FastAPI 单进程：auth + memory + conversation + community 路由           │
│   │  进程内 lifespan：ActivityPublisher、CommunityMaintenance（含附件清理流水线）       │
│ memory worker / scheduler / outbox_consumer、conversation worker / publisher（各 1）   │
│ migrate job ── 一次性容器：各 Alembic 链 upgrade head + sync-knowledge-graph          │
│ PostgreSQL（数据卷持久化；study 域默认不启用不部署其进程）                               │
│ backup 容器 ── 每日 pg_dump，保留 7 天                                                 │
└──────────────────────────────────────────────────────────────────────────────────┘
   │ 后端代理上传/删除图片（anyio.to_thread 调 SDK）
   ▼
七牛云 Kodo（公开读 Bucket）── CDN 加速域名 img.你的域名.com
```

**数据归属原则**：社区域图片本体存 Kodo、`community_attachments` 表只存 `storage_key` 与元信息；社区文本留 community 库；对话域、记忆域维持现状不进 Kodo；**社区图片不进入任何学习记忆**。

## 六、移植来源对照速查

| 我们要做的 | 模仿来源 | 方式 |
|---|---|---|
| 帖子/回复/附件模型与计数 | FlaskBB `flaskbb/forum/models.py` | 代码移植（同步 → async 改写） |
| 发帖/回帖/删除基本规则 | FlaskBB `flaskbb/forum/views.py` | 逻辑移植改写 |
| 附件服务设计 | bbs-go `server/`（Go） | 仅参考设计 |
| 页面布局与交互 | bbs-go `web/`（React） | 对照重写 React 组件（状态导航，无路由） |
| 建吧申请 + 管理员审核 | 无现成实现 | 自研：2 张新表（`community_attachments` + `community_board_applications`）+ boards 扩展 + 5 个接口 |

## 七、关键机制设计（执行级冻结）

### 7.1 新旧切换与回滚（D11）

- **迁移方式（D32）**：不改 0001，新增 `0002_community_v2` 迁移做扩展；不建 `*_v2` 表、不双写。
- **flag 控制路由（完整清单，冻结）**：`POST /uploads`、`POST /boards/applications`、`GET /boards/applications/mine`、`GET /admin/board-applications`、`POST /admin/board-applications/{id}/approve|reject`、`GET /permissions`、`GET /boards/{slug}`。`false` 时以上不挂载，其余接口与现有测试不受影响。
- `GET /local-uploads/{storage_key:path}` **不受 flag 控制**，仅 `backend=local` 时挂载。
- flag 不是新旧核心切换开关；完整回滚 = `git revert` + `alembic downgrade`（0002 必须实现 downgrade：删新表、回滚全部 ALTER，不恢复被清空的旧数据）。
- 配置 `backend/settings.py`，默认 `false`（D40：开发便利值）；各 Phase 经批准后 dev/test 置 `true`；P5 上线固定 `true`（部署验收项）；稳定一个版本后 chore 提交删除 flag。

### 7.2 0002 迁移精确清单（D12/D27/D32/D41，逐条冻结）

> 基准 = `community_migrations/versions/0001_community_core.py` 逐行核实。0002 文件名 `0002_community_v2.py`，`down_revision = "0001_community_core"`。**迁移文件自包含**：BOARDS_SEED 常量逐字复制进 0002（不 import 0001 模块——迁移是历史快照，0001 未来可能被改动）。

**① 清空旧业务数据（先于 ALTER，无生产数据前提，D41）**——单语句 TRUNCATE **七张表（含 boards）**：

```sql
TRUNCATE community_notifications, community_idempotency_requests, community_outbox,
         community_post_likes, community_replies, community_posts, community_boards;
```

PostgreSQL 单语句 TRUNCATE 自动处理 FK 依赖顺序。含 boards 的理由：若只按 slug 冲突更新，无法修正"slug 相同但 board_id 非冻结 UUID"的异常记录，也无法清除开发库里的非 seed 板块；TRUNCATE 后重插 seed 同时满足"仅保留 4 个 seed 板块"与"UUID=冻结值"。**前提变化（已有真实数据）必须停工会审（§十三风险 7）**；P5 首次部署为空库全新迁移，天然无此问题。

**破坏性迁移硬性安全门禁（冻结，写入 0002 `upgrade()` 开头，TRUNCATE 之前执行）**：① **行数闸门**：统计七张表——`community_boards` 中 board_id 不属于冻结四 UUID 的行数 + 其余六表总行数；**非零 → `raise RuntimeError("0002 拒绝在含业务数据的库执行")`，迁移失败退出、人工介入，不允许静默清空，也不在迁移内暂停等待确认**（自动化环境中失败即信号）；仅含 seed 四行的库允许通过（保证重复 upgrade 幂等）。② **允许执行的环境**：开发库、`community_test` 等 `*_test` 测试库、P5 全新空库；不以 `APP_ENV` 值做拦截（P5 首迁本身就是 production 环境的全新空库，行数闸门才是唯一判据）。③ P5 migrate job 直接执行该迁移 = 允许（全新空库必过门禁）。④ 门禁测试（§十二）：构造含非 seed 板块行 / 含任意业务行的库 → RuntimeError；纯 seed 库 / 空库 → 通过。

**② seed 重插（D27/D41）**：四个冻结 seed **纯 INSERT**（TRUNCATE 后空表，无冲突路径）；字段值与 0001 `BOARDS_SEED` 逐字一致（含显式 sort_order 10/20/30/40）。

**③ `community_boards` ALTER（0001 实际：无 created_by/updated_at/post_count，sort_order DEFAULT 0）**：

```sql
ALTER TABLE community_boards ADD COLUMN created_by uuid NULL;
CREATE INDEX ix_community_boards_created_by ON community_boards (created_by);
ALTER TABLE community_boards ADD COLUMN updated_at timestamptz NOT NULL DEFAULT now();
ALTER TABLE community_boards ADD COLUMN post_count integer NOT NULL DEFAULT 0;
ALTER TABLE community_boards ADD CONSTRAINT ck_community_boards_post_count CHECK (post_count >= 0);
ALTER TABLE community_boards ALTER COLUMN sort_order SET DEFAULT 100;
CREATE UNIQUE INDEX uq_community_boards_lower_name ON community_boards (lower(name));
```

**④ `community_notifications` ALTER（0001 实际：event_type 两值 CHECK、post_id/reply_id NOT NULL、body varchar(300)）**：

```sql
ALTER TABLE community_notifications DROP CONSTRAINT <0001 自动约束名>;  -- 名称解析规则见 ⑤ 下
ALTER TABLE community_notifications ADD CONSTRAINT ck_community_notifications_event_type
  CHECK (event_type IN ('post_replied','reply_marked_solved','application_approved','application_rejected'));
ALTER TABLE community_notifications ALTER COLUMN post_id DROP NOT NULL;
ALTER TABLE community_notifications ALTER COLUMN reply_id DROP NOT NULL;
ALTER TABLE community_notifications ADD COLUMN board_slug varchar(64) NULL;  -- 列宽对齐 boards.slug
-- body 保持 varchar(300) 不动：拒绝理由应用层上限 200，模板 + 理由 < 300（§7.5）
```

**⑤ `community_idempotency_requests` ALTER**：

```sql
ALTER TABLE community_idempotency_requests DROP CONSTRAINT <0001 自动约束名>;  -- ×2（operation、resource_type）
ALTER TABLE community_idempotency_requests ADD CONSTRAINT ck_community_idempotency_operation
  CHECK (operation IN ('create_post','create_reply','upload_attachment','create_application'));
ALTER TABLE community_idempotency_requests ADD CONSTRAINT ck_community_idempotency_resource_type
  CHECK (resource_type IN ('post','reply','attachment','application'));
-- UNIQUE (user_id, operation, idempotency_key) 不变：同一 key 在不同 operation 下天然隔离
```

**④⑤ 中 `<0001 自动约束名>` 的解析方式（冻结，不留占位）**：0001 的 CHECK 为内联未命名，PostgreSQL 自动命名（形如 `community_notifications_event_type_check`）。0002 在 Alembic Python 中执行：`op.get_bind()` 取得连接 → `SELECT conname FROM pg_constraint WHERE conrelid = '<表>'::regclass AND contype = 'c' AND pg_get_constraintdef(oid) ILIKE '%<列名>%'` → **结果必须恰好 1 条**：1 条 → 用真实名 DROP；0 条或多于 1 条 → `raise RuntimeError`（0001 结构异常，迁移失败，人工介入，不允许静默跳过）。0002 新增约束全部显式命名（`ck_` 前缀）；downgrade 引用固定名 DROP，同样规则反向执行。

**⑥ 新建 `community_attachments`、`community_board_applications`**：完整 DDL 见 §7.3（显式命名全部约束/索引）。

**⑦ downgrade**：删两张新表；boards 删三列 + 新索引 + sort_order DEFAULT 恢复 0；notifications 恢复两值 CHECK（先 `DELETE` event_type 为新类型的行）、post_id/reply_id 恢复 NOT NULL（先删 NULL 行——即同一批审核通知行）、删 board_slug 列；idempotency 恢复两值 CHECK（先删新 operation 行）；**不恢复已清空的旧业务数据**；新表已产生的附件/申请数据随表删除（接受，文档明示）。

**迁移验收口径**：① 空库 upgrade head 通过；② 连跑两次 head 幂等（第二次无变更）；③ `upgrade → downgrade → upgrade` 通过；④ **断言 4 个 seed 的 board_id 等于冻结 UUID**、字段等于冻结值；⑤ 旧业务表清空断言（开发库执行后，含 boards 只剩 4 行 seed）；⑥ 断言新 CHECK 生效（`operation='upload_attachment'` 幂等行、四值 event_type 且 post_id/reply_id 为 NULL 的通知行可插入）；⑦ 约束名解析失败路径（构造 0 匹配 → RuntimeError）单测覆盖。

**seed 板块冻结值（逐字，与 0001 `BOARDS_SEED` 一致）**：

| board_id (UUID) | slug | name | description | sort_order |
|---|---|---|---|---|
| `da38ecb6-6f37-5724-be95-10e496b5f3dd` | linear-algebra | 线性代数 | 矩阵、向量空间、特征值与线性变换 | 10 |
| `dcd2a3a5-7e06-5b7e-891f-e065765dcde0` | calculus | 微积分 | 极限、导数、积分与级数 | 20 |
| `d6559df9-da74-51ca-9526-a77229c19237` | probability | 概率论 | 概率模型、随机变量与统计推断 | 30 |
| `768737cb-a6a8-527d-a7f1-153bb8841872` | study-methods | 学习方法 | 学习方法、复习策略与学习习惯交流 | 40 |

### 7.3 数据字典与完整 DDL（D13/D31，字段级冻结，以 0001 实际为准）

> **命名映射总表（冻结）**：逻辑名 → 物理名。表名：boards→`community_boards`、posts→`community_posts`、replies→`community_replies`、attachments→`community_attachments`、applications→`community_board_applications`、likes→`community_post_likes`、notifications→`community_notifications`、outbox→`community_outbox`、幂等→`community_idempotency_requests`。主键：`board_id/post_id/reply_id/notification_id`（现有）、`attachment_id/application_id`（新表）。正文列与 API 字段统一为 **`body`**。**作者列：posts/replies 沿用现有 `user_id`**；attachments 用 `uploader_id`；applications 用 `applicant_id/reviewer_id`。跨库用户 ID 不建 FK 只建索引。时间列 `timestamptz DEFAULT now()`。约束/索引命名前缀：`ck_`（CHECK）、`uq_`（唯一）、`ix_`（普通索引）。
> **计数精确变化（同事务）**：发帖 `boards.post_count+1`（0002 新增列，初值 0）、软删帖 `-1`；回帖 `post.reply_count+1`、软删回复 `-1`；点赞/取消 `like_count±1`（现有实现沿用）。**purge 路径同步维护**（§7.17）。`solved` 为派生展示字段（`solved_reply_id IS NOT NULL`），不建列。`solved_reply_id` 必须属于当前帖子（沿用现有保证）。
> **DB 列宽 vs 应用层上限原则（冻结）**：0001 已有列宽（slug 64 / name 80 / description 500 / title 200 / body text）**一律不改**；应用层上限更严（slug 30 / 吧名 20 / 简介 100 / 标题 200 沿用现有 / 正文回复沿用现有 settings 19500），由 validate 层把关——避免为未来扩列再做迁移。**settings 合法范围 ≤ 冻结上限（D10），只能调小**。

**community_boards**（0001 现有列 + 0002 新增列，总览）

| 列 | 类型 | 约束 | 来源 |
|---|---|---|---|
| board_id | uuid | PK | 0001 |
| slug | varchar(64) | UNIQUE NOT NULL | 0001（应用层限 30） |
| name | varchar(80) | NOT NULL；0002 新增 `uq_community_boards_lower_name` | 0001（应用层限 20） |
| description | varchar(500) | NOT NULL DEFAULT '' | 0001（应用层限 100；**内容允许空字符串**） |
| sort_order | integer | NOT NULL；0002 ALTER DEFAULT 100 | 0001（seed 显式 10/20/30/40） |
| status | text | NOT NULL DEFAULT 'active' CHECK IN ('active','hidden') | 0001（MVP 无设置 hidden 入口） |
| created_at | timestamptz | NOT NULL DEFAULT now() | 0001 |
| created_by | uuid | NULL，`ix_community_boards_created_by`；NULL=系统 seed | **0002 新增** |
| updated_at | timestamptz | NOT NULL DEFAULT now()；服务层维护（D50） | **0002 新增** |
| post_count | integer | NOT NULL DEFAULT 0，`ck_community_boards_post_count (>=0)` | **0002 新增** |

**community_posts**（0002 完全不动，0001 现有定义）

| 列 | 类型/说明 |
|---|---|
| post_id | uuid PK |
| user_id | uuid NOT NULL，普通索引（**作者列，代码核实**） |
| author_display_name | varchar(80) NOT NULL（写时快照） |
| board_id | uuid NOT NULL FK → community_boards |
| title | varchar(200) NOT NULL（应用层上限沿用现有 200） |
| body | text NOT NULL（正文） |
| content_hash | char(64) NOT NULL（= `sha256("标题：{title}\n正文：{body}")`，§7.5） |
| status | text CHECK IN ('active','hidden','deleted') |
| discussion_status | text CHECK IN ('open','closed')（closed 时拒绝新回复/resolve） |
| eligible_for_memory | boolean NOT NULL DEFAULT true |
| pinned | boolean NOT NULL DEFAULT false（MVP 无设置入口，恒 false） |
| solved_reply_id | uuid NULL FK → community_replies（必须属本帖） |
| solution_generation | integer NOT NULL DEFAULT 0（resolve 代际，通知 dedupe 用） |
| reply_count / like_count | integer NOT NULL DEFAULT 0 |
| created_at / updated_at / last_activity_at / deleted_at | timestamptz |
| 索引 | `ix_community_posts_list`、`ix_community_posts_user`、`ix_community_posts_board`（均 0001 现有） |

**community_replies**（0002 完全不动，0001 现有定义）：`reply_id PK / post_id FK / user_id（作者列）/ author_display_name varchar(80) / body text / content_hash char(64)=sha256(body) / status CHECK('active','hidden','deleted') / eligible_for_memory bool / created_at / updated_at / deleted_at`；索引 `ix_community_replies_post`、`ix_community_replies_user`（均现有）。

**community_attachments（0002 新建，完整 DDL 冻结）**：

```sql
CREATE TABLE community_attachments (
    attachment_id uuid PRIMARY KEY,
    uploader_id uuid NOT NULL,
    post_id uuid REFERENCES community_posts(post_id) ON DELETE RESTRICT,
    position smallint,
    storage_key varchar(128) NOT NULL,
    original_filename varchar(100) NOT NULL DEFAULT '',
    mime varchar(32) NOT NULL,
    size_bytes integer NOT NULL,
    width integer NOT NULL,
    height integer NOT NULL,
    status varchar(16) NOT NULL DEFAULT 'uploaded',
    delete_attempts integer NOT NULL DEFAULT 0,
    last_delete_error text,
    next_delete_attempt_at timestamptz,
    storage_deleted_at timestamptz,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT uq_community_attachments_storage_key UNIQUE (storage_key),
    CONSTRAINT ck_community_attachments_position CHECK (position BETWEEN 0 AND 2),
    CONSTRAINT ck_community_attachments_status CHECK (status IN ('uploaded','attached','deleted','orphaned')),
    CONSTRAINT ck_community_attachments_delete_attempts CHECK (delete_attempts >= 0),
    CONSTRAINT ck_community_attachments_uploaded CHECK (
        status <> 'uploaded' OR (post_id IS NULL AND position IS NULL)),
    CONSTRAINT ck_community_attachments_attached CHECK (
        status <> 'attached' OR (post_id IS NOT NULL AND position IS NOT NULL))
);
CREATE UNIQUE INDEX uq_community_attachments_post_position
    ON community_attachments (post_id, position) WHERE status = 'attached';
CREATE INDEX ix_community_attachments_uploader ON community_attachments (uploader_id);
CREATE INDEX ix_community_attachments_post ON community_attachments (post_id, position);
CREATE INDEX ix_community_attachments_cleanup ON community_attachments (status, next_delete_attempt_at)
    WHERE next_delete_attempt_at IS NOT NULL;
```

字段规则补充（冻结）：`storage_key` 格式 `community/{yyyy-mm}/{uuid}.{ext}`（年月 UTC）；`deleted/orphaned` 状态**保留原 post_id/position，为服务层约定，DB 层不强制**（CHECK 只约束 uploaded/attached 两态——组合由 §7.12 唯一写入路径保证）；`storage_deleted_at`/`next_delete_attempt_at` 组合不建 DB CHECK（服务层不变量：删除成功 → `storage_deleted_at=now() + next_delete_attempt_at=NULL`；待重试 → 相反）；**仅用于审计/展示，不参与 storage_key、URL、Content-Type 判定，不写入日志敏感路径**；multipart 缺 filename 或 Content-Type 时：filename 记 ''，Content-Type 缺失视为空字符串走 Pillow 检测（检测失败 → 422 `UPLOAD_INVALID_TYPE`）。

**`original_filename` 清洗函数（冻结伪代码，顺序即语义）**：

```python
def sanitize_original_filename(raw: str | None) -> str:
    if raw is None:
        return ""
    # 1) basename：同时按 / 与 \ 切割，取最后一段
    name = raw.replace("\\", "/").rsplit("/", 1)[-1]
    # 2) 删除控制字符（U+0000–U+001F、U+007F）与 Unicode 格式字符
    #    （unicodedata.category == "Cf"，如零宽字符）——删除而非替换、不拒绝
    name = "".join(
        ch for ch in name
        if unicodedata.category(ch) != "Cf"
        and not (ord(ch) < 0x20 or ord(ch) == 0x7F)
    )
    # 3) 删除后再 trim（去除首尾空白，含上一步删除后暴露的空白）
    name = name.strip()
    # 4) NFC 归一
    name = unicodedata.normalize("NFC", name)
    # 5) 按 Python 字符数截断 ≤100（不看 UTF-8 字节数）
    return name[:100]
```

输入 → 输出样例（冻结，逐条入测试）：`"a/b\\c.jpg"` → `"c.jpg"`；`"  photo .png "` → `"photo .png"`；`"pic​.jpg"`（含零宽空格 U+200B）→ `"pic.jpg"`；`"pic\n\t.jpg"` → `"pic.jpg"`（控制字符删除后无首尾空白可 trim）；`""` / `None` → `""`；200 字符超长名 → 前 100 字符。

**community_board_applications（0002 新建，完整 DDL 冻结）**：

```sql
CREATE TABLE community_board_applications (
    application_id uuid PRIMARY KEY,
    applicant_id uuid NOT NULL,
    name varchar(80) NOT NULL,
    slug varchar(64) NOT NULL,
    description varchar(500) NOT NULL DEFAULT '',
    reason varchar(500) NOT NULL,
    status varchar(16) NOT NULL DEFAULT 'pending',
    board_id uuid REFERENCES community_boards(board_id) ON DELETE RESTRICT,
    reviewer_id uuid,
    reviewed_at timestamptz,
    reject_reason varchar(500),
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT uq_community_board_applications_board UNIQUE (board_id),
    CONSTRAINT ck_community_board_applications_status CHECK (status IN ('pending','approved','rejected')),
    CONSTRAINT ck_community_board_applications_pending CHECK (
        status <> 'pending' OR (board_id IS NULL AND reviewer_id IS NULL
            AND reviewed_at IS NULL AND reject_reason IS NULL)),
    CONSTRAINT ck_community_board_applications_approved CHECK (
        status <> 'approved' OR (board_id IS NOT NULL AND reviewer_id IS NOT NULL
            AND reviewed_at IS NOT NULL AND reject_reason IS NULL)),
    CONSTRAINT ck_community_board_applications_rejected CHECK (
        status <> 'rejected' OR (board_id IS NULL AND reviewer_id IS NOT NULL
            AND reviewed_at IS NOT NULL AND reject_reason IS NOT NULL))
);
CREATE UNIQUE INDEX uq_community_board_applications_pending_name
    ON community_board_applications (lower(name)) WHERE status = 'pending';
CREATE UNIQUE INDEX uq_community_board_applications_pending_slug
    ON community_board_applications (slug) WHERE status = 'pending';
CREATE UNIQUE INDEX uq_community_board_applications_pending_applicant
    ON community_board_applications (applicant_id) WHERE status = 'pending';
CREATE INDEX ix_community_board_applications_applicant
    ON community_board_applications (applicant_id, created_at DESC, application_id DESC);
CREATE INDEX ix_community_board_applications_review
    ON community_board_applications (status, created_at ASC, application_id ASC);
```

行为规则（冻结）：审核人允许等于申请人（MVP 不限制）；拒绝后同 slug 可立即重新申请（部分唯一索引只覆盖 pending）；已审核记录不可改名/重审（409）；应用层上限 name 20 / slug 30 / description 100（**简介允许空字符串**）/ reason 500 / reject_reason 200（§7.5），列宽对齐 boards 留裕量。

### 7.4 建吧状态机与 slug/命名规则（D3/D21/D22/D25/D26/D47）

```text
community_board_applications: pending ──approve──▶ approved（事务顺序见 D38：锁申请 → INSERT boards(status='active', created_by=申请人, sort_order=100, post_count=0) → 单语句 UPDATE 申请 → INSERT 通知）
                             └──reject──▶ rejected（单语句 UPDATE 含 reject_reason；允许再次申请）
community_boards: active（MVP 唯一常态）
```

- **申请提交时冲突检查（D47，冻结）**：① 规范化（trim/NFC/小写）→ ② 保留字 → ③ **查 boards：lower(name) 或 slug 已被现有板块占用 → 409 `BOARD_NAME_CONFLICT` 立即反馈（不产生 pending）** → ④ 自己已有 pending → 409 `APPLICATION_DUPLICATE_PENDING` → ⑤ INSERT（并发下他人 pending 占名由部分唯一索引兜底 → 409 `BOARD_NAME_CONFLICT`，message 指明"该名称/标识已有待审核申请"）。申请与审核两处冲突语义一致：名字被占 = `BOARD_NAME_CONFLICT`（409）。
- 并发约束（DB 兜底）：同上节部分唯一索引；并发审核由 `SELECT ... FOR UPDATE` 行锁保证只有一事务推进（D35/D38）。
- 审核幂等：已审核再审核 → 409 `APPLICATION_ALREADY_REVIEWED`。
- 审核时名称冲突：approve 时 name/slug 已被 boards 占用（pending 期间他人申请先通过）→ INSERT 触发唯一约束 → 整体回滚 → 409 `BOARD_NAME_CONFLICT`，申请保持 pending，管理员 reject 后用户改名重新申请；MVP 不做修改/撤回接口。
- slug（D21）：trim + 转小写后校验：长度 2–30，首末 `a-z0-9`，中间单个 `-`，禁止连续 `--`；正则 `^[a-z0-9](?:[a-z0-9]|-(?=[a-z0-9])){0,28}[a-z0-9]$`；**保留 slug（冻结）**：`applications, admin, posts, uploads, new, mine, replies, notifications, local-uploads` → 422 `BOARD_SLUG_RESERVED`；**slug 路径参数大小写**：存储即小写，路由层对 `{slug}` 转小写后查询。
- 拒绝原因：trim 后 1–200 字符（应用层），空 → 422 `REJECT_REASON_INVALID`。
- 吧主 = `created_by`，不可转让；用户被删/封禁板块保留；审核通过后吧名/slug 不可修改。

### 7.5 纯文本规范与 hash 规则（D14/D33，按代码核实修正）

| 字段 | 最大长度（字符，`len()`） | 规则 |
|---|---|---|
| 帖子标题 | 200（沿用现有 `_TITLE_MAX_CHARS`） | trim 后非空 |
| 帖子正文 body | 19500（沿用现有 `community_post_body_max_length`） | trim 后非空（不允许只发图） |
| 回复 body | 19500（沿用现有 `community_reply_max_length`） | 同上 |
| 吧名 | 20（`community_board_name_max_chars`） | trim + NFC 后非空，全局唯一 |
| slug | 30（`community_board_slug_max_chars`） | §7.4 正则 |
| 板块简介 | 100（`community_board_description_max_chars`） | trim；**允许空字符串** |
| 申请理由 | 500（`community_application_reason_max_chars`） | trim 后非空 |
| 拒绝理由 | 200（`community_reject_reason_max_chars`） | trim 后非空；通知正文 = 模板 + 理由 < 300，通知 `body` 列不动 |

注：§7.15 中各 `*_max_chars` 的合法范围约束的是**上限配置值本身**（且只能 ≤ 此表冻结值）；内容允许为空与否以此表"规则"列为准。

- 统一 trim；保留换行（**不做连续换行折叠**——现有代码无此行为）；**接受任意字符串（含 `<`/`>`/HTML 片段），原样存储；前端永远纯文本转义渲染**（D14/D33 统一，= 现有 validate 实际行为）；emoji 允许；**URL 不自动识别为链接（第二阶段）**；按字符数计；
- **内容安全（D33）**：现有 `validate_post(title, body)` / `validate_reply(body)` = strip + 非空 + 长度 + 控制字符白名单（禁 U+0000–U+001F/U+007F，白名单 `\n \t \r`）+ SourceItem 契约（组合内容 ≤20000 字符、UTF-8 ≤80000 bytes）；新字段（吧名/slug/简介/理由/拒绝理由）复用同一控制字符与 trim 工具；命中 → 422 `COMMUNITY_CONTENT_INVALID`（field 为对应字段）；不引入敏感词库；
- **证据 hash 规则（冻结，逐字按 `content_safety.py`）**：帖子 `content_hash = sha256("标题：{title}\n正文：{body}")`（UTF-8；title/body 为 strip 后文本；**前缀与单个 `\n` 分隔符是公式的一部分**）；回复 `content_hash = sha256(body)`；`source_version = content_hash`；回复通知与 Memory payload 使用同一份 strip 后文本。

### 7.6 权限矩阵、可选认证与管理员判定（D15/D23/D46）

| 行为 | 未登录（匿名） | 登录用户 | 吧主(MVP 无特权) | 管理员 |
|---|---|---|---|---|
| 浏览板块/帖子/回复/图片 | ✅（可选认证，D46） | ✅ | ✅ | ✅ |
| 发帖/回复/点赞/resolve/删自己内容 | ❌ 401 | ✅ | ✅ | ✅ |
| 上传/申请建吧/查自己申请/permissions | ❌ 401 | ✅ | ✅ | ✅ |
| 查审核列表/审核 | ❌ 401 | ❌ 403 `ADMIN_REQUIRED` | ❌ 403 | ✅ |

- **可选认证依赖（D46，冻结）**：新增 `get_optional_auth_context`（与 `get_auth_context` 同模块 `backend/shared/auth_context.py`）：**无凭证 → 匿名**（viewer_liked/viewer_is_author/viewer_is_owner 一律 false）；**带了凭证但无效/过期 → 401**（不静默降级——前端此时以为已登录，降级会造成"操作丢失"困惑）；适用接口：§八 #1–#4 与 #19；写接口仍用 `get_auth_context`（必需认证）。**这是对现有读接口的行为变更（现为必需认证，代码核实），属 MVP 功能清单第 1 条的既定目标。**
- **匿名认证实现合同（D46a–d，按代码核实冻结）**：
  - **D46a 上下文类型**：**不改 `AuthContext.user_id` 为可空**（`backend/auth/context.py:96-107` 的不可空约束是全局不变量，改动面太大）；可选依赖返回类型 = **`AuthContext | None`**（None = 匿名），调用方按 None 分支处理，不引入新的上下文类。
  - **D46b 凭证优先级（按 `backend/auth/verifier.py:262-266` CompositeAuthVerifier 现行为准）**：**`Authorization` 头优先**——development + dev_auth 开启时，带 Authorization 头即走生产 JWT 适配器，否则才读 `X-Dev-User-Id`；非 development 永远只走 JWT。`get_optional_auth_context` 沿用同一顺序，不另立规则。**空白值规则（按 `verifier.py:149-151` 现状差异定稿）**：现有实现对 `"   "` 这类仅空白值会判为"头存在"并进 JWT 校验 → 401；**可选认证依赖在判定前先对 Authorization 值 `strip()`，空串/仅空白一律按"无凭证"处理 → 匿名**（继续检查 X-Dev-User-Id）；写接口的 `get_auth_context` **行为不变**（空白 → 401 `AUTH_REQUIRED`，与现状一致）。
  - **D46c 匿名限流**：`rate_limit` 依赖（`backend/community/api/dependencies.py:162-181`，现有要求必需认证）改造为接受 `AuthContext | None`：有认证 = 现有 user_id 桶 + IP 桶双桶不变；**匿名 = 仅 IP 桶**；`resolve_client_ip` 返回 None（request.client 无地址）时匿名请求使用**固定 fallback 桶 `ip:unknown`**（所有无 IP 匿名共享同一桶）——**不跳过、不拒绝**（跳过 = 无限流绕过；拒绝 = 正常内网/测试流量误伤）。
  - **D46d 读模型适配**：`PostReadService` 等读模型方法签名 `viewer_user_id: UUID` 改为 **`UUID | None`**（代码核实涉及 `post_service.py:64,103,150,170,190,210`）：None 时 `viewer_liked/viewer_is_author/viewer_is_owner` 恒 false，`liked_post_ids` 不发起查询直接返回空集；**匿名路径不初始化 Auth DB session**（无身份映射需求）。
- `COMMUNITY_ADMIN_USER_IDS`（逗号分隔 UUID）+ `require_community_admin`（社区域依赖：非管理员 → 403 **`ADMIN_REQUIRED`**，区别于 shared `require()` 的通用 `AUTH_FORBIDDEN`）；**生产启动强校验：必须 ≥1 个合法 UUID**；非生产允许空；trim、去重、修改需重启。
- 软删展示：帖子删除后 404 `COMMUNITY_NOT_FOUND`（hidden 同）；回复删除保留占位行（现有墓碑契约）；不做编辑/恢复；封禁用户内容正常显示。
- **hidden 板块读取行为（冻结，消除实现者自由裁量）**：MVP 无 hidden 设置入口，但语义必须冻结——**hidden 板块及其内容对外一律视为不存在**：① `GET /boards` 列表只返回 active（现有）；② `GET /boards/{slug}` 对 hidden → 404 `COMMUNITY_NOT_FOUND`（不是 409）；③ `GET /posts?board_id=<hidden 板块>` → 404 `COMMUNITY_NOT_FOUND`；④ **全局帖子流（不带 board_id）join `community_boards` 增加 `b.status='active'` 过滤**，hidden 板块的帖子不出现（代码核实：现有 `persistence/posts.py` 列表查询只按帖子状态过滤，需补该条件）；⑤ `GET /posts/{post_id}` 对 hidden 板块中的帖子 → 404（含按 post ID 直达）；⑥ hidden 板块内的回复/点赞/resolve/删除与读同口径——帖子不可见即 404；**唯一例外**是发帖接口对 hidden 板块返回 409 `COMMUNITY_BOARD_DISABLED`（现有语义保留，区分"无此板块"与"板块不可发帖"）。

### 7.7 Outbox 与 Memory 证据链（D19，payload 逐字段冻结）

- **事务语义**：帖子/回复创建 + Outbox 事件 + 通知记录 + 幂等记录**同一事务提交**；提交后 publisher 异步发布，失败可重试。
- **payload 唯一事实源 = 代码 `services/post_command_service.py`**，逐字段如下（新模型接入逐字段保持）：

`community.post_created`：
```json
{"source_ref":"community:post:{post_id}","source_version":"<content_hash>","activity_type":"forum_post","activity_ids":["post:{post_id}"],"content_ref":"community:post:{post_id}","aggregated_count":1,"topic_hints":["{board_slug}"],"graph_node_hints":[],"window_started_at":null,"window_ended_at":null}
```

`community.reply_created`（同构，差异冻结）：`source_ref="community:reply:{reply_id}"`、`activity_type="forum_reply"`、`activity_ids=["reply:{reply_id}"]`、`content_ref="community:reply:{reply_id}"`、`topic_hints=["{board_slug}"]`、`source_version=<回复 content_hash>`，其余字段同 post_created。

`community.source_deleted`：`{"source_ref":"...","source_version":null,"source_system":"activity","event_id":"<uuidv5>"}`（`source_deletion_id_for` 稳定派生；幂等键 `community:community.source_deleted:{id}`）。

- 帖子证据正文 = `"标题：{title}\n正文：{body}"`（与 content_hash 输入同一字符串，§7.5）、回复证据 = strip 后 body；**均不含图片 URL**；
- 附件上传成功但发帖失败 → 不产生事件；幂等键沿用 `community:{event_type}:{aggregate_id}`；
- 审核通知仅 notifications 表记录，无 Outbox 事件（D30）。

### 7.8 通知范围与契约（D16/D30/D44，0002 ALTER 后生效）

| 事件 | 对象 | event_type | post_id/reply_id | board_slug | dedupe_key |
|---|---|---|---|---|---|
| 帖子被回复 | 帖主 | `post_replied`（沿用） | 均非 NULL | 填帖子所属板块 slug | `post_replied:{post_id}:{reply_id}`（沿用现有公式） |
| 回复被标记解决 | 回复作者 | `reply_marked_solved`（沿用） | 均非 NULL | 填帖子所属板块 slug | `reply_marked_solved:{post_id}:{reply_id}:{solution_generation}`（沿用） |
| 建吧申请通过 | 申请人 | `application_approved`（新增） | **均 NULL** | 填申请 slug | `application_approved:{application_id}` |
| 建吧申请拒绝 | 申请人 | `application_rejected`（新增） | **均 NULL** | 填申请 slug | `application_rejected:{application_id}` |
| 新申请产生 | 管理员 | — | — | — | ❌ 不做 |

- 通知与业务写入同事务、异步投递、失败不阻塞主事务；`actor_user_id`：审核通知填**审核管理员** id（**仅 DB 列，DTO 按 §6.6 规则不返回**）；`CommunityNotification.event_type` Literal 扩展两个新类型 + 新增字段 `board_slug`；`unread_count` = **当前用户全部未读数**；
- 审核通知文案模板（冻结）：通过 = `你申请的板块「{name}」已通过审核`；拒绝 = `你申请的板块「{name}」未通过审核：{reject_reason}`（理由 ≤200，总量 < 300，不截断）；
- 两类既有通知填 `board_slug` 的取数点见 §7.17（创建时由帖子 join 板块取得）；通知 `board_slug` 列可空（DB 层），应用层四类通知均填；旧数据已清空，无历史 NULL。

### 7.9 Kodo 配置合同与依赖（D6/D17/D29/D49）

环境变量（settings 字段见 §7.15）：`COMMUNITY_STORAGE_BACKEND`（`local` 默认 / `kodo`）、`KODO_ACCESS_KEY`、`KODO_SECRET_KEY`、`KODO_BUCKET`、`KODO_REGION`、`KODO_CDN_DOMAIN`。

**启动校验（冻结）**：非生产默认 `local`；集成测试强制 `local`；生产必须 `kodo` 且 AK/SK/Bucket/CDN 域名四项非空、Region ∈ 合法列表，否则 Settings 构造抛错、进程不启动，严禁回退；SDK 初始化失败（生产）= 启动失败。
- `KODO_CDN_DOMAIN`：**只接受裸域名，逐 label RFC 校验（冻结）**——校验前先 `strip()` + 转小写（大写输入自动归一，不拒绝）；然后：按 `.` 切分，**至少 2 个 label**（单标签如 `localhost`/`img` 拒绝）；每个 label 匹配 `^[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?$`（首末必须字母数字，中间可含单个或多个 `-`）；总长度 ≤253；**仅 ASCII——punycode（`xn--` 开头）等国际化域名一律拒绝**；禁止 scheme（http/https）、端口（`:`）、路径、查询参数、末尾 `/`（逐 label 规则已天然排除这些字符）。任一不满足 → Settings 启动抛错。URL 拼接 `https://{domain}/{storage_key}`。
- `KODO_REGION`：合法列表**即最终冻结值** `{"z0","z1","z2","na0","as0","cn-east-2"}`；默认 `z0`（开发便利），生产由启动校验保证在列表内。**核对与偏离处理（冻结）**：P1 实施时以 `uv.lock` 锁定的 qiniu SDK 版本（`qiniu>=7,<8` 区间内的实际锁定版）的 Region 常量与七牛官方文档核对一次；**核对结果与本列表不一致 → 停工会审、更新本文档后方可实施，不允许实施者自行改列表**；合法列表同时写入配置校验测试（合法值逐个通过 + 非法值启动抛错 + 列表内容不可变断言），作为不可变验收项。
- 测试/生产分桶；AK/SK 走 compose `env_file`；无需 Kodo CORS；本地 compose（HTTP profile）用 `local`，**无独立 staging 环境**（真实部署前以本地 compose + local 演练）。

依赖（D49）：pyproject 新增 `qiniu>=7,<8`、`pillow>=11,<13`；**改 pyproject 必须同步 `uv.lock`；CI 与生产只允许 `uv sync --frozen` 按锁文件安装**；Pillow 大版本升级后图片安全测试矩阵（§十二）必须重跑。上传成功 = HTTP 200 且返回体含 key；**删除返回 612（对象不存在）视为成功**。

**超时与错误分类（冻结）**：连接超时 10s、读取超时 30s（settings 可调小/调大，范围 §7.15）；**应用层不重试**，失败即返回 `COMMUNITY_UPLOAD_FAILED`（HTTP 5xx/超时/连接异常 → retryable=true；4xx（非 612）/响应缺 key → retryable=false 并记 error 日志）；删除除 612 外错误一律进 maintenance 重试流水线。

### 7.10 图片安全与上传实现（D10/D28/D36）

**MIME 映射表（冻结）**：

| Pillow `Image.format` | 数据库 mime | 扩展名 |
|---|---|---|
| `JPEG` | `image/jpeg` | `.jpg` |
| `PNG` | `image/png` | `.png` |
| `WEBP` | `image/webp` | `.webp` |

**MVP 做**：① >5MiB（5,242,880 bytes）拒绝（422 `UPLOAD_TOO_LARGE`），流式读取超限即断（64KiB 块）；② Content-Type 解析（小写归一、去 `;` 参数；缺失视为空字符串）白名单 + Pillow 实际解码；**不一致但各自合法 → 接受按 Pillow 校正**；Content-Type 合法但 Pillow=GIF → 拒绝（422 `UPLOAD_INVALID_TYPE`）；③ **像素超限主动判断（D36 修正——Pillow `MAX_IMAGE_PIXELS` 的 warning/2x-error 原生语义不能保证"超过即拒"，故改为显式计算）**：读取 `width×height`，**> `community_image_max_pixels`（默认 40,000,000）→ 422 `UPLOAD_BOMB_REJECTED`**；同时应用启动时将 `Image.MAX_IMAGE_PIXELS` 设为同值作为底层双保险（其 `DecompressionBombError` 亦映射 `UPLOAD_BOMB_REJECTED`，warning 不拒绝）；④ 扩展名按映射表生成；⑤ 先校验后上传（SpooledTemporaryFile，1MiB 内存阈值）；⑥ multipart 字段名 `file`，单请求单文件，空文件 → 422。

**Pillow 调用顺序（冻结）**：流式读入 SpooledTemporaryFile（超 5MiB 即断）→ `Image.open()` → `.verify()`（完整性校验通过即视为有效图片，**不强制 `.load()` 完整解码**）→ **重新 `Image.open()`** → 读 `.format` 与 `.size` → 像素计算判断 → 写存储。

**nginx**：`client_max_body_size 8m`。**MVP 不做**：EXIF 清除、宽高限制、动画 WebP 处理、病毒扫描、CMYK 转换、内容审核。

### 7.11 失败补偿与幂等（D45/D48 冻结；幂等模式 = 现有"先抢键后写业务"，代码核实）

- **幂等机制（沿用并扩展）**：现有实现 = 业务事务内先 `INSERT community_idempotency_requests ... ON CONFLICT DO NOTHING` 抢占 `(user_id, operation, idempotency_key)`，赢家才写业务；败者重读幂等行：payload_hash 相同 → 重放返回原资源（含原资源 ID），不同 → **422 `COMMUNITY_IDEMPOTENCY_CONFLICT`（现有 HTTP 状态，D13 保留）**；记录保留 7 天由现有 maintenance 清理。0002 扩展 CHECK 后新接口使用新字符串：**上传 `operation='upload_attachment', resource_type='attachment'`；建吧申请 `operation='create_application', resource_type='application'`**；申请幂等命中 → 返回原申请完整对象。
- **payload hash 输入（D45，逐接口冻结）**：沿用现有 `_idempotency_payload_hash`（canonical_json 确定性键排序 → sha256）；输入一律为**规范化后（= 实际入库）的值**：

| operation | hash 输入 dict（键按 canonical_json 排序） |
|---|---|
| `create_post` | `{"attachment_ids": [...], "board_id": "<uuid>", "body": "<strip 后>", "title": "<strip 后>"}` |
| `create_reply` | `{"body": "<strip 后>", "post_id": "<uuid>"}` |
| `upload_attachment` | `{"file_sha256": "<文件内容 sha256>"}` |
| `create_application` | `{"description": "<trim>", "name": "<trim+NFC>", "reason": "<trim>", "slug": "<trim+小写>"}` |

  - `attachment_ids` **顺序敏感**：`["a","b"]` 与 `["b","a"]` 是不同 payload → 422 冲突（顺序 = position，必须区分）；**null / 缺省 / `[]` 统一规范化为 `[]`**（三者同 payload）；
- **幂等记录与附件同生共死（冻结，消除孤儿窗口）**：**orphan 清理物理删除附件行时，同事务 `DELETE FROM community_idempotency_requests WHERE resource_type='attachment' AND resource_id=:attachment_id`**——幂等记录随附件消失，此后同 key 重试 = 重新完整上传（正确行为）；防御分支：上传幂等命中但附件记录不存在（理论上被同事务保证不可能）→ 422 `COMMUNITY_IDEMPOTENCY_CONFLICT`（message 引导重新上传）。
- **上传失败与幂等记录**：幂等记录与业务写入同事务——上传/校验/绑定任一失败 → 事务回滚 → **幂等记录不存在** → 同 key 重试重新执行完整上传。
- **上传执行顺序与事务边界（D48 扩展，冻结）**：① Pillow 校验（事务外，未开事务）→ ② **BEGIN**：抢占幂等键 → ③ **事务保持打开**状态下执行 Kodo 上传（SDK 走 `anyio.to_thread`，只发 HTTP、不发任何 SQL；"事务外"仅指 Kodo 调用不在 SQL 事务内，**DB 连接与事务全程保持打开，不先提交幂等记录**——先提交会引入"上传中/失败"状态位需求，MVP 不做）→ ④ 同事务 INSERT attachments → ⑤ COMMIT。**连接占用时长（冻结，消除"最坏数十秒"旧表述）**：= 校验 + 上传耗时，受 settings 控制——**默认 连接 10s + 读取 30s = 最坏约 40s；合法范围上限 120s + 300s = 最坏约 420s（§7.15），该占用明确接受**（MVP 单实例低并发，不引入连接池专项调参；生产建议保持默认值）。配套超时链（冻结）：nginx 对 `/api/v1/community/uploads` 单独 location `proxy_read_timeout 480s`（≥ 420s 上限 + 余量）；uvicorn 不设请求级超时（默认）；浏览器 fetch 不设超时。**取消/断开语义（冻结）**：客户端断开或请求取消时，`anyio.to_thread` 中的 SDK 调用**不可取消、继续执行完**；请求结束后 DB 事务**回滚**（session 上下文管理器异常路径）；若 Kodo 已上传成功但事务回滚 → 走与"INSERT 失败"**同一补偿路径**（best-effort 删 Kodo 对象，失败 → error 日志 + metrics 转人工）；local 模式已写文件 → 事务回滚后 best-effort 删除本地文件，失败成为本地孤儿文件（D48 同口径接受，不清理）。**③成功④之前/之间崩溃 → Kodo 对象成为无记录孤儿，maintenance 无法发现——明确接受**；不做意图表/对象对账（第二阶段评估）。
- **并发同 key 的等待语义（冻结）**：后到事务的 `INSERT ... ON CONFLICT DO NOTHING` 在 PostgreSQL 唯一约束下**阻塞等待先到事务提交或回滚**（天然等待，非报错）：先到者 COMMIT → 后到者 INSERT 未插入 → 重读幂等行（此时必然可见）→ payload_hash 相同走重放、不同走 422 冲突；先到者 ROLLBACK → 后到者插入成功、继续正常执行。**不配置 `lock_timeout`**（等待无超时上限，由反向代理/应用请求超时兜底）。**"同 key 只产生一个 Kodo 对象"承诺范围（冻结）：仅覆盖正常并发执行场景**（两请求都完整走完全流程）；崩溃、客户端取消、代理超时等异常场景可能产生孤儿对象（D48 已接受），**不在该承诺范围内**。
- **local storage 严格采用同一事务边界**：文件写入同样发生在事务保持打开期间，崩溃/失败语义与 Kodo 路径一致（文件已写、事务回滚 → 本地孤儿文件，无记录、不清理，与 D48 同口径接受）。
- **Kodo 上传成功、INSERT 失败**：立即 best-effort 删 Kodo 对象；补偿再失败 → error 日志（含 storage_key）+ metrics 转人工；返回 `COMMUNITY_UPLOAD_FAILED`；幂等记录随事务回滚不存在，同 key 重试重新上传。
- **Kodo 删除 612 视为成功**；**CDN 延迟失效明确接受**（MVP 不主动刷新，上线公告注明）。
- **Idempotency-Key 二分（冻结，代码核实）**：Header 带 `pattern="^[\x21-\x7e]{1,200}$"`——**格式非法 → FastAPI 路由前拦截 → 422 `INVALID_PAYLOAD`**；**缺失 → `_require_idempotency_key` → 422 `COMMUNITY_CONTENT_INVALID`（field=Idempotency-Key）**；新增 uploads/applications 接口完全复用同一模式。

### 7.12 附件状态机与清理流水线（冻结）

```text
uploaded ──发帖事务绑定(写 post_id+position，从 0 连续)──▶ attached ──删帖(同事务)──▶ deleted
   └──maintenance 转换：created_at 超过 TTL──▶ orphaned
```

- **orphan 转换 SQL（冻结，含批量上限）**：`UPDATE community_attachments SET status='orphaned', next_delete_attempt_at=now(), updated_at=now() WHERE attachment_id IN (SELECT attachment_id FROM community_attachments WHERE status='uploaded' AND created_at < now() - make_interval(hours => :ttl) ORDER BY created_at ASC LIMIT :batch)`（`:ttl` = `community_orphan_ttl_hours`，`:batch` = `community_cleanup_batch_size` 默认 500；DB `now()` 即 UTC）。
- **删除流水线（deleted 与 orphaned 共用；事务边界冻结）**：**逐条处理**——① **事务外**调存储删除（Kodo/local 同一接口）；② **事务内**更新：成功 → deleted 写 `storage_deleted_at=now(), next_delete_attempt_at=NULL`；orphaned **立即 DELETE 附件行 + 同事务删除其幂等记录**（§7.11）。失败 → 按下表退避：
- **重试时序表（冻结）**：`delete_attempts` 从 0 起，每次失败 +1——

| 失败次序 | delete_attempts（失败后） | next_delete_attempt_at | 说明 |
|---|---|---|---|
| 初次失败 | 1 | now()+1h | |
| 第 2 次失败 | 2 | now()+4h | |
| 第 3 次失败 | 3 | now()+12h | |
| 第 4 次失败 | 4 | **NULL（终态）** | `community_attachment_delete_exhausted_total` +1、error 日志、保留 `last_delete_error`；**终态记录不再被扫描 SQL 命中** |

  共 4 次尝试（1 初次 + 3 次重试）；`community_attachment_delete_failures_total` 每次失败 +1。**`delete_attempts` 成功后不重置**。
- **deleted 记录 30 天物理删除 SQL（冻结，含批量上限）**：`DELETE FROM community_attachments WHERE attachment_id IN (SELECT attachment_id FROM community_attachments WHERE status='deleted' AND storage_deleted_at IS NOT NULL AND storage_deleted_at < now() - make_interval(days => :retention) LIMIT :batch)`（`:retention` = `community_attachment_deleted_retention_days`，`:batch` = `community_cleanup_batch_size`）；对象已在写 `storage_deleted_at` 时删除，物理删行不再调存储。
- **崩溃窗口说明（冻结）**：对象已删、DB 未提交时进程崩溃 → 下轮对该记录重新调删除 → Kodo 612 / local 文件不存在**视为成功** → 正常推进——**这是预期恢复方式，不设 processing 状态**。
- **人工恢复（runbook 冻结）**：终态记录人工确认存储侧已清理后可 `DELETE` 该行；或手工重置重试：`UPDATE community_attachments SET delete_attempts=0, next_delete_attempt_at=now(), last_delete_error=NULL WHERE attachment_id='<id>'`（写入 docs/ops/failure-runbook.md）。
- **LocalStorage 对齐**：local 模式走同一状态机与流水线；删除=删本地文件，**文件不存在视为成功**。本地上传根目录 = settings `community_local_upload_dir`（默认 `.local/uploads`，相对进程工作目录；开发=repo 根；集成测试用 tmp_path fixture 覆盖）；目录权限 0755。
- **local-uploads 路由（冻结，含数据库查找规则与 URL 生成）**：`GET /api/v1/community/local-uploads/{storage_key:path}`（FastAPI `:path` 多段参数）；**无认证依赖**（图片公开语义，D46 中唯一完全绕过认证的路由）；处理顺序：**① key 格式校验**（`^community/\d{4}-\d{2}/[0-9a-f-]{36}\.(jpg|png|webp)$`）→ ② **查 `community_attachments`，无记录 → 404 `COMMUNITY_NOT_FOUND`** → ③ 状态门槛：`uploaded/attached/deleted` 放行，**`orphaned` → 404** → ④ realpath 位于 uploads 根内 → ⑤ 文件不存在 → 404 `COMMUNITY_NOT_FOUND`；**Content-Type 一律以数据库 `mime` 为准**；**上传响应与帖子视图中的 `url` 字段：local 模式返回相对路径（D42）**；集成测试断言 200。
- **运行方式与事务边界（冻结，按 `services/maintenance.py:55-91` 现状改造）**：并入现有 `CommunityMaintenance`（memory-api lifespan asyncio 任务，间隔 `community_maintenance_interval_seconds` 默认 3600s），但 `_run_once` **拆为两个阶段**：**阶段一"旧清理"**（幂等记录 7 天 / delivered outbox 30 天 / dead_letter 90 天 / 通知 90 天）**保持现有单事务不变**；**阶段二"附件清理"在阶段一事务提交后独立执行**，顺序固定为：① orphan 转换（单事务一条 UPDATE，SQL 见上）→ ② 删除流水线逐条处理 → ③ deleted 物理删除（单事务一条 DELETE，SQL 见上）。**删除流水线扫描查询（冻结）**：`SELECT ... FROM community_attachments WHERE status IN ('deleted','orphaned') AND next_delete_attempt_at IS NOT NULL AND next_delete_attempt_at <= now() ORDER BY next_delete_attempt_at ASC LIMIT :batch`；**逐条处理时每条附件新建独立 session/事务**（事务外存储删除 → 事务内状态推进，见上"删除流水线"）；**单附件失败只记录该条（attempts+1/退避）并继续下一条，不影响同轮其他附件**。**批量口径**：orphan 转换、删除流水线扫描、deleted 物理删除**各自最多 `community_cleanup_batch_size` 条**（不是整轮合计）。**不使用 `FOR UPDATE SKIP LOCKED`**（P5 单实例部署，无并发 worker；多实例 advisory lock 列第二阶段）。**重入**：`run_forever` 循环内串行 `await`，同一进程天然不重入；单实例部署下无跨进程重叠。

### 7.13 限流合同（D34，冻结）

| 接口 | 限流组 | 配额（settings 字段，默认值） |
|---|---|---|
| GET 系列（boards/posts/详情/notifications/boards/{slug}/permissions/local-uploads） | `community.read`（现有） | 沿用现有 |
| POST /posts | `community.post.create`（现有） | 沿用现有（`community_rate_limit_post_per_hour`=10） |
| POST /replies | `community.reply.create.minute` + `.hour`（现有） | 沿用现有（5/分钟、60/小时） |
| like/unlike | `community.post.like`（现有） | 沿用现有（60/分钟） |
| POST /uploads | `community.upload`（新增） | `community_rate_limit_upload_per_hour`=20 |
| POST /boards/applications | `community.application.create`（新增） | `community_rate_limit_application_per_day`=5 |
| approve/reject | `community.admin.review`（新增） | `community_rate_limit_admin_review_per_hour`=60 |
| notifications read/read-all、resolve、delete | `community.read`/现有写组 | 沿用现有 |

超限 → 429 `COMMUNITY_RATE_LIMITED`（retryable=true，沿用现有格式）；限流维度 = 认证 user_id（匿名读接口按 IP，沿用现有实现）。

### 7.14 并发与竞态语义（D35/D38，冻结）

- **附件绑定**：发帖事务内逐条执行 `UPDATE community_attachments SET status='attached', post_id=:pid, position=:pos, updated_at=now() WHERE attachment_id=:id AND uploader_id=:uid AND status='uploaded'`；执行前先 SELECT 校验归属（属他人 → 403 `ATTACHMENT_FORBIDDEN`）；条件 UPDATE 影响 0 行（非 uploaded）→ 409 `ATTACHMENT_CONFLICT`；同一请求 `attachment_ids` 重复 → 422 `COMMUNITY_CONTENT_INVALID`；数量 >3 → 422 `ATTACHMENT_LIMIT_EXCEEDED`。
- **发帖与删帖并发 / 重复删除语义（冻结，计数原子性机制唯一）**：删帖/删回复 = 同事务内两段式：**① 读态判定**（`get_post_any_status` / `get_reply_any_status`）：不存在或 hidden → 404 `COMMUNITY_NOT_FOUND`；非作者 → 404（不泄露存在性）；**status='deleted' 且当前用户是作者 → 200 幂等成功**（不重扣计数、不重写 deleted_at、不重发事件）；**② 条件 UPDATE 原子转换**：删帖 `UPDATE community_posts SET status='deleted', discussion_status='closed', eligible_for_memory=false, deleted_at=now(), updated_at=now() WHERE post_id=:id AND status='active'`；删回复 `UPDATE community_replies SET status='deleted', deleted_at=now(), updated_at=now() WHERE reply_id=:id AND status='active'`。**影响 0 行（并发下已被另一删除抢先）→ 按"已删除+作者"返回 200 幂等成功；影响 1 行才继续后续写入**——这是计数只减一次的**唯一一致性机制**：只有状态转换成功者才执行 ③ 扣计数（删帖 `UPDATE community_boards SET post_count = post_count - 1, updated_at=now() WHERE board_id=:bid`；删回复 `UPDATE community_posts SET reply_count = reply_count - 1, updated_at=now() WHERE post_id=:pid`）+ ④ 关联写入（删帖：附件条件转换 deleted、boards 计数、source_deleted Outbox 事件；删回复：被删回复是当前 solved 回复时 `set_solution(reply_id=None, generation=当前值)` 清除）+ ⑤ COMMIT。**不使用 `GREATEST(count-1, 0)` 兜底**（会掩盖数据不一致）：计数直写 `count - 1`，CHECK(`>=0`) 违反 = 数据异常，抛错转人工（metrics + error 日志），不自动修复。**锁顺序（冻结，防死锁）**：所有事务按 `posts → replies/attachments → boards` 顺序取行锁（发帖 INSERT posts（新行）→ 锁 attachments → 锁 boards；删帖锁 posts → attachments → boards；删回复锁 replies → posts；resolve 只锁 posts；审核锁 applications → INSERT boards），禁止反序。**并发最终值**：resolve 与 solved 回复删除并发时，两者都 UPDATE 同一 posts 行，PG 行锁串行化（READ COMMITTED 下后到 UPDATE 等待后基于最新行版本重评估），**最终值 = 最后提交者**，两种结局（删除胜出→solved 清除；resolve 胜出→resolve 生效、随后删除再清除）均合法，不指定胜者；deletion 事件由确定性 UUIDv5 event_id + `ON CONFLICT (idempotency_key) DO NOTHING` 保证只落一条。**普通删除与 purge 并发**：purge 逐帖走同一服务函数（§7.17），同一条件 UPDATE 机制保证每帖计数只减一次。
- **建吧审核事务步骤（D38，冻结）**：
  - **approve**：① `SELECT ... FROM community_board_applications WHERE application_id=:id FOR UPDATE`（不存在 → 404；`status<>'pending'` → 409 `APPLICATION_ALREADY_REVIEWED`）→ ② `INSERT community_boards`（status='active'、created_by=applicant、sort_order=100、post_count=0；name/slug 唯一冲突 → **整体回滚** → 409 `BOARD_NAME_CONFLICT`，申请保持 pending）→ ③ **单语句** `UPDATE community_board_applications SET status='approved', reviewer_id=:admin, reviewed_at=now(), board_id=:new_board_id, updated_at=now() WHERE application_id=:id` → ④ `INSERT` 通知（按 §7.8 表）→ ⑤ COMMIT；
  - **reject**：① 同上锁 → ② 单语句 `UPDATE ... SET status='rejected', reviewer_id, reviewed_at=now(), reject_reason=:reason, updated_at=now() WHERE application_id=:id AND status='pending'`（0 行 → 409）→ ③ INSERT 通知 → ④ COMMIT；
  - 唯一约束异常 → PublicError 映射表（§八）统一转换（不允许泄漏 500）。
- **resolve**：`reply_id=null` = 取消解决；reply 必须属本帖；已删除回复 → 404；重复 resolve 同一 reply 幂等 200；post 已删 → `COMMUNITY_POST_CLOSED`（现有语义：deleted 帖子的解决操作 409）、已关闭 → `COMMUNITY_POST_CLOSED`；hidden 板块发帖 → `COMMUNITY_BOARD_DISABLED`（现有语义保留，MVP 无 hidden 入口）。

### 7.15 settings 配置合同（D39/D40/D10，全部冻结）

> 命名模式沿用 `backend/settings.py` 现有约定：snake_case 字段 + 大写环境变量 alias；pydantic-settings 读取，**修改需重启进程**；测试不改默认值，由 conftest/fixture 注入小值。
> **环境差异规则（D40，冻结）**：**默认值 = 开发/测试便利值**；生产由**启动强校验**强制显式覆盖（§7.9）；`community_v2_enabled=true` 是 P5 部署验收项（人工核对）。
> **上限保护规则（D10，冻结）**：带冻结上限的配置（图片数量/大小/像素、吧名/slug/简介/理由长度）**合法范围上界 = MVP 冻结值，settings 只能调小**，防止"配置 4 张图撑爆 position CHECK"类矛盾。

| settings 字段 | 环境变量 | 默认值 | 合法范围 | 用途 | 状态 |
|---|---|---|---|---|---|
| `community_v2_enabled` | `COMMUNITY_V2_ENABLED` | `false` | bool | 新功能路由 flag（D11） | 新增 |
| `community_storage_backend` | `COMMUNITY_STORAGE_BACKEND` | `local` | `local`/`kodo` | 存储后端 | 新增 |
| `community_local_upload_dir` | `COMMUNITY_LOCAL_UPLOAD_DIR` | `.local/uploads` | 非空路径 | local 存储根目录 | 新增 |
| `kodo_access_key` | `KODO_ACCESS_KEY` | 无 | 生产非空 | 七牛 AK | 新增 |
| `kodo_secret_key` | `KODO_SECRET_KEY` | 无 | 生产非空 | 七牛 SK | 新增 |
| `kodo_bucket` | `KODO_BUCKET` | 无 | 生产非空 | 桶名 | 新增 |
| `kodo_region` | `KODO_REGION` | `z0` | §7.9 合法列表 | 机房 | 新增 |
| `kodo_cdn_domain` | `KODO_CDN_DOMAIN` | 无 | 生产非空 + §7.9 裸域名校验 | CDN 域名 | 新增 |
| `kodo_connect_timeout_seconds` | `KODO_CONNECT_TIMEOUT_SECONDS` | `10` | 1–120 | SDK 连接超时 | 新增 |
| `kodo_read_timeout_seconds` | `KODO_READ_TIMEOUT_SECONDS` | `30` | 1–300 | SDK 读取超时 | 新增 |
| `community_attachment_max_per_post` | `COMMUNITY_ATTACHMENT_MAX_PER_POST` | `3` | **1–3** | 每帖图片上限（≤position CHECK 上界） | 新增 |
| `community_image_max_bytes` | `COMMUNITY_IMAGE_MAX_BYTES` | `5242880` | **1024–5242880** | 单图上限（≤5MiB 冻结值） | 新增 |
| `community_image_max_pixels` | `COMMUNITY_IMAGE_MAX_PIXELS` | `40000000` | **1000000–40000000** | 像素阈值（≤40MP 冻结值） | 新增 |
| `community_orphan_ttl_hours` | `COMMUNITY_ORPHAN_TTL_HOURS` | `24` | 1–168 | orphan 转换锚点 | 新增 |
| `community_attachment_deleted_retention_days` | `COMMUNITY_ATTACHMENT_DELETED_RETENTION_DAYS` | `30` | 1–365 | deleted 记录物理删除锚点 | 新增 |
| `community_board_name_max_chars` | `COMMUNITY_BOARD_NAME_MAX_CHARS` | `20` | **1–20** | 吧名上限 | 新增 |
| `community_board_slug_max_chars` | `COMMUNITY_BOARD_SLUG_MAX_CHARS` | `30` | **2–30** | slug 上限 | 新增 |
| `community_board_description_max_chars` | `COMMUNITY_BOARD_DESCRIPTION_MAX_CHARS` | `100` | **1–100** | 简介上限（**内容允许空**，范围约束上限配置值本身） | 新增 |
| `community_application_reason_max_chars` | `COMMUNITY_APPLICATION_REASON_MAX_CHARS` | `500` | 1–500 | 申请理由上限 | 新增 |
| `community_reject_reason_max_chars` | `COMMUNITY_REJECT_REASON_MAX_CHARS` | `200` | 1–200 | 拒绝理由上限 | 新增 |
| `community_rate_limit_upload_per_hour` | `COMMUNITY_RATE_LIMIT_UPLOAD_PER_HOUR` | `20` | 1–10000 | 上传限流 | 新增 |
| `community_rate_limit_application_per_day` | `COMMUNITY_RATE_LIMIT_APPLICATION_PER_DAY` | `5` | 1–100 | 建吧申请限流 | 新增 |
| `community_rate_limit_admin_review_per_hour` | `COMMUNITY_RATE_LIMIT_ADMIN_REVIEW_PER_HOUR` | `60` | 1–10000 | 审核限流 | 新增 |
| `community_admin_user_ids` | `COMMUNITY_ADMIN_USER_IDS` | 非生产可空 | 逗号分隔 UUID | 管理员名单（生产 ≥1） | 新增 |
| `community_maintenance_interval_seconds` | `COMMUNITY_MAINTENANCE_INTERVAL_SECONDS` | `3600` | 60–86400（0002 配套加 Field 约束） | maintenance 间隔 | **沿用现有** |
| `community_cleanup_batch_size` | `COMMUNITY_CLEANUP_BATCH_SIZE` | `500` | 1–5000（同上） | 清理批量上限 | **沿用现有** |
| `community_idempotency_retention_days` | `COMMUNITY_IDEMPOTENCY_RETENTION_DAYS` | `7` | 1–90（同上） | 幂等记录保留 | **沿用现有** |
| `community_post_body_max_length` / `community_reply_max_length` | 同名大写 | `19500` | 现有行为不变 | 正文/回复上限 | **沿用现有** |
| `community_rate_limit_post_per_hour` / `reply_per_minute` / `reply_per_hour` / `like_per_minute` / `read_per_minute` | 同名大写 | 现有默认 | 现有行为不变 | 现有限流 | **沿用现有** |

### 7.16 migrate 链清单（P5 migrate job 执行范围，冻结）

| 链 | Alembic ini | 数据库 | 执行条件 |
|---|---|---|---|
| memory | `alembic.ini` | memory | **总是执行** |
| auth | `auth_alembic.ini` | auth | **总是执行**（内嵌 auth_service 必须） |
| conversation | `conversation_alembic.ini` | conversation | 配置 `CONVERSATION_DATABASE_URL` 时执行（我们部署=执行） |
| community | `community_alembic.ini` | community | 配置 `COMMUNITY_DATABASE_URL` 时执行（我们部署=执行） |
| study | `study_alembic.ini` | study | 仅 `STUDY_DOMAIN_ENABLED=true`（**MVP 不启用=跳过**） |
| rag | `rag_alembic.ini`（`rag_migrations/`） | rag | 配置 `RAG_DATABASE_URL` 时执行（生产建议配置=执行） |

- memory 链迁移完成后执行 `python -m backend.memory.cli sync-knowledge-graph --apply`（否则 `/health/ready` 报 `knowledge_graph_registry_not_loaded`）；
- **失败语义（冻结）**：各链独立提交、无跨库事务；任一链失败 → migrate job 非零退出 → memory-api **不启动**；修复后重跑 job（Alembic 幂等跳过已完成版本）；
- **downgrade 语义（冻结）**：0002 downgrade 删新表新列，**已产生的附件/申请数据随之清除（接受）**；回滚顺序 = 先 downgrade → 再切回旧镜像启动。
- **migrate job 权限与命令（冻结）**：① **数据库用户**：迁移需要 DDL 权限，不用运行时最小权限角色——migrate job 使用各库**属主（owner）角色**，连接串通过 `.env.production` 提供 `MEMORY_MIGRATE_DATABASE_URL` / `AUTH_MIGRATE_DATABASE_URL` / `CONVERSATION_MIGRATE_DATABASE_URL` / `COMMUNITY_MIGRATE_DATABASE_URL` / `RAG_MIGRATE_DATABASE_URL`（study 未启用不提供）；② **wrapper 脚本**：新增 `scripts/migrate-all.sh`（入库），逐链执行 `alembic -c <ini> upgrade head`（环境变量注入对应连接串），**任一链非零退出 → 打印链名与版本到 stderr → 整个脚本 exit 1**；③ **sync 顺序**：`python -m backend.memory.cli sync-knowledge-graph --apply` **仅在 memory 链 upgrade 成功后执行**（其余链失败则 job 整体失败、sync 不执行）；④ **条件排除**：`STUDY_DOMAIN_ENABLED!=true` 时脚本跳过 study 链；`RAG_MIGRATE_DATABASE_URL` 未配置时跳过 rag 链；conversation/community 按对应 `*_MIGRATE_DATABASE_URL` 是否配置决定（我们部署 = 配置 = 执行）；⑤ **compose 挂载**：migrate 服务 `command: ["sh", "/app/scripts/migrate-all.sh"]`、`restart: "no"`，镜像复用后端镜像（含全部迁移链文件，现有根 Dockerfile 已 COPY 各 ini 与迁移目录）；⑥ 0002 破坏性门禁（§7.2）在全新空库天然通过。

### 7.17 既有调用路径改动清单与 updated_at 逐 SQL 写入点清单（D50 冻结）

**updated_at 逐 SQL 写入点清单（冻结，全部显式 `updated_at=now()`，不建 trigger；测试逐项断言）**：

| # | 写入路径 | SQL 更新点（同事务；字段级表达，每行显式含 `updated_at`） | 写 updated_at 的表 |
|---|---|---|---|
| 1 | 创建回复 | `community_replies` INSERT（`created_at/updated_at` DEFAULT now()）；`UPDATE community_posts SET reply_count = reply_count + 1, last_activity_at = now(), updated_at = now() WHERE post_id = :pid` | replies（DEFAULT）、posts |
| 2 | 删除回复 | `UPDATE community_replies SET status='deleted', deleted_at=now(), updated_at=now() WHERE reply_id=:rid AND status='active'`（影响 1 行才继续）；`UPDATE community_posts SET reply_count = reply_count - 1, updated_at = now() WHERE post_id = :pid`；**被删回复是当前 solved 回复时**追加：`UPDATE community_posts SET solved_reply_id=NULL, updated_at=now() WHERE post_id=:pid`（`solution_generation` 不递增，D34） | replies、posts |
| 3 | 点赞 / 取消点赞 | `community_post_likes` INSERT / DELETE；`UPDATE community_posts SET like_count = like_count + 1（或 - 1）, updated_at = now() WHERE post_id = :pid` | posts |
| 4 | resolve / 取消 resolve | `UPDATE community_posts SET solved_reply_id = :rid（或 NULL）, solution_generation = solution_generation + 1（取消不递增，沿用现有 `set_solution` 语义）, updated_at = now() WHERE post_id = :pid` | posts |
| 5 | 删帖（普通删除与 purge 逐帖路径同一服务函数） | `UPDATE community_posts SET status='deleted', discussion_status='closed', eligible_for_memory=false, deleted_at=now(), updated_at=now() WHERE post_id=:id AND status='active'`（影响 1 行才继续）；`UPDATE community_attachments SET status='deleted', next_delete_attempt_at=now(), updated_at=now() WHERE post_id=:id AND status='attached'`；`UPDATE community_boards SET post_count = post_count - 1, updated_at = now() WHERE board_id = :bid` | posts、boards、attachments |
| 6 | purge 中批量删除回复 | 逐回复同 #2（复用同一服务函数，禁止另写一套） | replies、posts |
| 7 | 建吧审核 approve / reject | approve：`community_boards` INSERT（DEFAULT now()）+ `UPDATE community_board_applications SET status='approved', reviewer_id=:admin, reviewed_at=now(), board_id=:new_board_id, updated_at=now() WHERE application_id=:id`；reject：`UPDATE community_board_applications SET status='rejected', reviewer_id=:admin, reviewed_at=now(), reject_reason=:reason, updated_at=now() WHERE application_id=:id AND status='pending'` | applications、boards（INSERT DEFAULT） |
| 8 | 附件生命周期 | 绑定：`UPDATE community_attachments SET status='attached', post_id=:pid, position=:pos, updated_at=now() WHERE attachment_id=:id AND uploader_id=:uid AND status='uploaded'`；orphan 转换 / 删除重试（`delete_attempts+1`、`next_delete_attempt_at`、`last_delete_error`、`updated_at=now()`）/ 删除成功（`storage_deleted_at=now(), next_delete_attempt_at=NULL, updated_at=now()`）：SQL 见 §7.12；**物理 DELETE 无 updated_at 可写** | attachments |
| 9 | 发帖 | `community_posts` INSERT（DEFAULT now()）；`UPDATE community_boards SET post_count = post_count + 1, updated_at = now() WHERE board_id = :bid`；附件绑定同 #8 | posts（DEFAULT）、boards |

除上表与 INSERT DEFAULT 外，**禁止任何对 `community_posts/community_replies/community_boards/community_attachments/community_board_applications` 的 UPDATE 遗漏 `updated_at=now()`**；实现 review 时以本清单逐行对照。

**既有调用路径改动清单（冻结）**：

| 调用路径（代码位置） | 需要的改动（全部显式写 `updated_at=now()`，不建 trigger） |
|---|---|
| 发帖 `services/post_command_service.py::create_post` | 同事务 `boards.post_count+1, updated_at=now()`；附件绑定（§7.14，写 attachments.updated_at） |
| 删帖 `post_command_service.py::delete_post` | 同事务 `post_count-1` + posts.updated_at + 附件转 deleted（写 updated_at） |
| 账号 purge `api/internal_accounts.py::purge_account` | 逐帖 `mark_post_deleted` 路径**补 `post_count-1` 与该帖附件转 deleted**（与普通删帖共用同一服务函数，禁止另写一套）；回复路径已有 `decrement_reply_count` 不动；purge 触发的附件清理 = 转 deleted 进统一流水线 |
| 回复创建 `services/reply_service.py`（post_replied 通知点） | 创建通知时由帖子 join 板块取 `slug` 填入 `board_slug` 列 |
| resolve `post_command_service.py::resolve`（reply_marked_solved 通知点） | 同上取板块 slug 填入 |
| boards 查询 `persistence/`（列表/详情） | 响应补 `post_count/sort_order/created_at` 字段（排序 D26） |
| 帖子读模型 `PostReadService` | Summary/Detail 组装新增 `attachments` 恒返回数组（D37） |
| `CommunityNotification` DTO | event_type Literal 扩两值 + `board_slug` 字段（§7.8）；reviewer_id/actor_user_id 不进任何 DTO（D44） |
| 读接口认证依赖（#1–#4、#19） | `get_auth_context` → `get_optional_auth_context`（D46） |
| 清理流水线（orphan 转换/删除重试/物理删除） | 各 UPDATE/DELETE 均写 `updated_at=now()`（物理 DELETE 无）；orphan 删附件同事务删幂等记录（§7.11） |
| 建吧审核（approve/reject） | 单语句 UPDATE 含 `updated_at=now()`（§7.14） |
| board 删除 | **无入口**（MVP 不删板块；created_by 永久保留） |

## 八、API 契约（精确 Schema，冻结）

> 兼容约定（D13）：保留接口路径/方法/状态码/错误码（含 **`COMMUNITY_IDEMPOTENCY_CONFLICT`=422**，代码核实）/游标/UUID 主键/**字段名（`body` 等）**不变；响应仅新增字段。写操作未认证 401、无权限 403。创建类接口（uploads/posts/replies/applications）必须 `Idempotency-Key`（缺失 → `COMMUNITY_CONTENT_INVALID`；格式非法 → `INVALID_PAYLOAD`，§7.11）；approve/reject 不用 key。路由注册顺序：静态路径先于 `/boards/{slug}`。flag 控制路由 = #2、#12–#18；#19 仅受 `backend=local` 控制。
> **响应模型逐字按代码核实**：所有 DTO `extra="forbid"`；作者视图 `CommunityAuthor={display_name}`；DTO 不暴露任何内部 user_id（D44）。
> **认证列说明**：可选 = `get_optional_auth_context`（D46：无凭证匿名、无效凭证 401）；登录 = `get_auth_context`（必需）。

| # | 方法 | 路径 | 请求 → 响应（成功码） | 认证 | 限流组 |
|---|---|---|---|---|---|
| 1 | GET | `/api/v1/community/boards` | → 200 `BoardListResponse{items:[{board_id,slug,name,description,post_count,sort_order}]}`（无分页；仅 active；新增字段恒非空；`sort_order ASC, created_at ASC, board_id ASC`） | 可选 | community.read |
| 2 | GET | `/api/v1/community/boards/{slug}` | → 200 `{board:{board_id,slug,name,description,post_count,created_at,viewer_is_owner}}`（viewer_is_owner 恒 bool，匿名 false） | 可选 | community.read |
| 3 | GET | `/api/v1/community/posts?board_id=&sort=&cursor=&limit=` | `board_id` 可选；sort 默认 `latest`；`unanswered` = `status='active' AND solved_reply_id IS NULL`；→ 200 `PostListResponse{items:[CommunityPostSummary+attachments],next_cursor,has_more}` | 可选 | community.read |
| 4 | GET | `/api/v1/community/posts/{post_id}` | → 200 `CommunityPostDetailResponse{post: CommunityPostDetail+attachments, replies: Page[CommunityReplyView]}`（信封结构；墓碑：`title/body=null,deleted=true`）；不存在/hidden/deleted → 404 `COMMUNITY_NOT_FOUND`；`viewer_liked` 匿名恒 false | 可选 | community.read |
| 5 | POST | `/api/v1/community/posts` | `{board_id,title,body,attachment_ids?≤3}` → 201 `CommunityPostDetail`（含恒返回 `attachments`） | 登录 | community.post.create |
| 6 | POST | `/api/v1/community/posts/{post_id}/replies` | `{body}` → 201 `CommunityReplyView`（`{reply_id,author:{display_name},body,deleted,viewer_is_author,solved,created_at}`；占位行 `body=null,deleted=true`） | 登录 | reply.create.minute/hour |
| 7 | POST/DELETE | `/api/v1/community/posts/{post_id}/like` | → 200 `{"status":"ok"}`（幂等） | 登录 | community.post.like |
| 8 | POST | `/api/v1/community/posts/{post_id}/resolve` | `{reply_id: UUID|null}`（null=取消解决）→ 200 `{"status":"ok"}` | 登录（作者） | community.read |
| 9 | DELETE | `/api/v1/community/posts/{post_id}` | → 200 `{"status":"ok"}`（重复删除幂等成功） | 登录（作者） | community.read |
| 10 | DELETE | `/api/v1/community/posts/{post_id}/replies/{reply_id}` | → 200 `{"status":"ok"}`（solved 回复清除解决标记） | 登录（作者） | community.read |
| 11a | GET | `/api/v1/community/notifications?unread_only=&cursor=&limit=` | → 200 `CommunityNotificationPage{items,next_cursor,has_more,unread_count}`（unread_count=当前用户全部未读数） | 登录 | community.read |
| 11b | POST | `/api/v1/community/notifications/{notification_id}/read` | → 200 `ReadAllResponse{unread_count}`（幂等） | 登录 | community.read |
| 11c | POST | `/api/v1/community/notifications/read-all` | → 200 `ReadAllResponse{unread_count}` | 登录 | community.read |
| 12 | POST | `/api/v1/community/uploads` | multipart `file` + Idempotency-Key → **201** `{attachment_id,url,mime,width,height,size_bytes}`（url 规则 D42） | 登录 | community.upload |
| 13 | POST | `/api/v1/community/boards/applications` | `{name,slug,description,reason}` + Idempotency-Key → **201** 完整申请对象 | 登录 | community.application.create |
| 14 | GET | `/api/v1/community/boards/applications/mine?cursor=&limit=` | → 200 Page 信封；排序 **`created_at DESC, application_id DESC`** | 登录 | community.read |
| 15 | GET | `/api/v1/community/admin/board-applications?status=&cursor=&limit=` | → 200 Page 信封；排序 **`created_at ASC, application_id ASC`**；status ∈ pending/approved/rejected/all，默认 pending | 管理员 | community.read |
| 16 | POST | `/api/v1/community/admin/board-applications/{id}/approve` | → 200 更新后申请对象（含 `board_id`） | 管理员 | community.admin.review |
| 17 | POST | `/api/v1/community/admin/board-applications/{id}/reject` | `{reason}` → 200 更新后申请对象 | 管理员 | community.admin.review |
| 18 | GET | `/api/v1/community/permissions` | → 200 `{is_community_admin: bool}` | 登录 | community.read |
| 19 | GET | `/api/v1/community/local-uploads/{storage_key:path}` | → 200 图片字节（仅 backend=local 挂载；查找规则 §7.12；**无认证依赖**） | 无 | community.read |

**游标规则（冻结，按 `backend/shared/cursor.py` 实际结构表述）**：游标 payload 实际结构 = `{cursor_version, route, normalized_filters, sort_key, expires_at[, principal_hash]}`（HMAC 签名，验签时 route / normalized_filters / 过期逐字段绑定）。#1/#3/#4 公共分页沿用现有**公共游标**（`bind_principal=False`，payload 无 principal_hash）；**#14/#15 使用私有游标（`bind_principal=True`，同 #11 通知的 `resolve_private_cursor`）**——mine/审核列表与用户身份相关，防止游标跨用户复用。**status 通过 `normalized_filters` 承载**：#14 固定 `normalized_filters = {"status": "mine"}`（标记值，仅保持两接口游标结构同构，路由不同已天然隔离）；#15 `normalized_filters = {"status": "<查询值>"}`，**status=all 时字面写入 `"all"`**。验签时 `normalized_filters` 整体比对：**切换 status（含 all ↔ 具体值）后携带旧游标 → 422 `COMMUNITY_CURSOR_INVALID`（现有 code），客户端必须丢弃游标重新分页**；无效签名/过期/principal 不匹配同样 `COMMUNITY_CURSOR_INVALID`。排序键：#14 `sort_key=[last_created_at(ISO8601 UTC), last_application_id]`（DESC）；#15 同构（ASC）。

**通用约定**：`attachments` = `[{attachment_id,url,width,height,mime,position}]` 按 `position ASC`，**恒返回，无附件为 `[]`**（D37）；**申请对象（D44，冻结）= `{application_id,name,slug,description,reason,status,board_id,reviewed_at,reject_reason,created_at}`——不含 `reviewer_id`**，状态 JSON 值 `"pending"/"approved"/"rejected"`，mine 与 admin 列表同一形状；通知 item = `{notification_id,event_type,title,body,read_at,created_at,post_id,reply_id,board_slug}`——`post_id/reply_id` 对审核通知为 null，`board_slug` 四类通知恒非空；event_type ∈ `post_replied/reply_marked_solved/application_approved/application_rejected`；分页沿用 `Page` 信封（limit 默认 20 ≤50）；回复 `created_at ASC` 沿用现有游标；软删数据不参与游标。

**422 三分（冻结，项目既定约定）**：额外字段 → `REQUEST_EXTRA_FIELD`（`extra="forbid"`）；类型错误/缺必填字段/Header pattern 不匹配 → `INVALID_PAYLOAD`；业务规则校验 → `COMMUNITY_CONTENT_INVALID` 或对应业务 code。

**错误码 → HTTP 映射表（冻结）**——现有 7 个 code 原样保留（含 HTTP 状态），新增 code 全部注册进 `COMMUNITY_ERROR_CODES` 并定义对应 `CommunityError` 子类：

| code | HTTP | retryable | field | 说明 | 来源 |
|---|---|---|---|---|---|
| `COMMUNITY_NOT_FOUND` | 404 | false | — | 不存在/无权（含 local-uploads 无记录/orphaned/文件缺失） | 现有 |
| `COMMUNITY_BOARD_DISABLED` | 409 | false | — | hidden 板块发帖（MVP 无 hidden 入口，语义保留） | 现有 |
| `COMMUNITY_POST_CLOSED` | 409 | false | — | 已关闭/已删除帖子的回复/resolve | 现有 |
| `COMMUNITY_CONTENT_INVALID` | 422 | false | 对应字段 | 业务校验（含缺 Idempotency-Key） | 现有 |
| `COMMUNITY_IDEMPOTENCY_CONFLICT` | **422** | false | — | 同 key 不同 payload（**现有状态，不改 409**） | 现有 |
| `COMMUNITY_CURSOR_INVALID` | 422 | false | — | 游标非法/绑定不匹配（含跨 status 复用） | 现有 |
| `COMMUNITY_RATE_LIMITED` | 429 | true | — | 超限 | 现有 |
| `UPLOAD_TOO_LARGE` | 422 | false | file | >5MiB | 新增 |
| `UPLOAD_INVALID_TYPE` | 422 | false | file | 类型白名单/解码失败/GIF | 新增 |
| `UPLOAD_BOMB_REJECTED` | 422 | false | file | 像素超阈值（显式计算） | 新增 |
| `COMMUNITY_UPLOAD_FAILED` | **502（唯一 HTTP 状态，不引入 503）** | 实例级（见下方实现冻结） | — | 通用文案，不暴露底层细节 | 新增 |
| `ATTACHMENT_LIMIT_EXCEEDED` | 422 | false | attachment_ids | >3 张 | 新增 |
| `ATTACHMENT_FORBIDDEN` | 403 | false | attachment_ids | 非本人附件 | 新增 |
| `ATTACHMENT_CONFLICT` | 409 | false | attachment_ids | 已绑定/不存在/已被清理/幂等命中但附件缺失 | 新增 |
| `APPLICATION_DUPLICATE_PENDING` | 409 | false | — | 自己已有 pending 申请 | 新增 |
| `APPLICATION_ALREADY_REVIEWED` | 409 | false | — | 重复审核 | 新增 |
| `BOARD_NAME_CONFLICT` | 409 | false | name/slug | 名字/标识被占（含"已有待审核申请"）；message 指明 | 新增 |
| `BOARD_SLUG_RESERVED` | 422 | false | slug | 保留字 | 新增 |
| `REJECT_REASON_INVALID` | 422 | false | reason | 空或超长（>200） | 新增 |
| `ADMIN_REQUIRED` | 403 | false | — | 非管理员访问管理接口（社区域 code，区别于 shared `AUTH_FORBIDDEN`） | 新增 |
| `REQUEST_EXTRA_FIELD` | 422 | false | 对应字段 | extra=forbid | 现有（全局） |
| `INVALID_PAYLOAD` | 422 | false | 对应字段 | 类型/缺字段/Header pattern | 现有（全局） |

**`COMMUNITY_UPLOAD_FAILED` 实现冻结**：现有错误类以类属性 `retryable: bool = False` 为主（`contracts/errors.py`）；`CommunityUploadFailedError` **新增构造参数 `retryable: bool`**（实例级覆盖类默认），PublicError 信封按实例值序列化。**SDK 异常 → retryable 完整映射表（冻结）**：Kodo 返回 HTTP 5xx → `true`；连接异常 / 连接超时 / 读取超时 → `true`；其他未知异常 → `true`（保守可重试）；Kodo 返回 HTTP 4xx（**含 612**——612 仅对删除视为成功，**上传路径收到 612 按 4xx 失败处理**）→ `false`；HTTP 200 但响应体缺 key → `false`。**前端只按 `code` 处理，不读取 `retryable`**（前端文案"服务繁忙，请稍后再试"对应唯一状态 502，不区分 503）。

**数据库唯一约束 → code 映射表（冻结）**：

| 约束 | 触发场景 | code |
|---|---|---|
| `uq_community_boards_lower_name` / boards.slug UNIQUE | approve INSERT 冲突 | `BOARD_NAME_CONFLICT`（409） |
| `uq_community_board_applications_pending_name/pending_slug` | 申请 INSERT 并发占名 | `BOARD_NAME_CONFLICT`（409，message"已有待审核申请"） |
| `uq_community_board_applications_pending_applicant` | 自己重复 pending | `APPLICATION_DUPLICATE_PENDING`（409） |
| `uq_community_board_applications_board` | 一申请一板块（理论不可达） | 500 前拦截：`APPLICATION_ALREADY_REVIEWED` |
| `uq_community_attachments_post_position` | 并发绑定同 position | `ATTACHMENT_CONFLICT`（409） |
| `community_idempotency_requests` UNIQUE | 幂等占位 | 非错误：重放/冲突分支（§7.11） |
| `community_notifications.dedupe_key` UNIQUE | 通知去重 | 非错误：`ON CONFLICT DO NOTHING` |

## 九、前端视图与行为清单（D43 冻结——前端为 PageKey 状态导航，无 URL 路由）

> 代码核实：`frontend/src/App.tsx` 用 `useState<PageKey>` 切换视图（home/chat/plan/map/summaries/community/profile），无 react-router、无 URL 路由；登录/注册表单在 `profile` 视图（未登录态）；AuthContext 统一处理会话失效。**社区新功能全部实现为 `page==="community"` 视图内的子视图状态（组件内 useState），不引入 URL 路由**；bbs-go 只抄布局与交互，不抄其路由结构。

| 子视图 | 说明 |
|---|---|
| 社区首页（默认） | 板块宫格 + 最新帖子流 |
| 板块详情 | 板块信息 + 帖子列表（游标分页） |
| 帖子详情 | 正文 + 配图（原图 `max-width:100%`，position 序）+ 评论区 |
| 发帖 | 标题 + textarea + 选图按钮（记住来源板块） |
| 申请建吧 | 上半申请表单；下半我的申请列表 |
| 管理员审核 | 入口按 `permissions` 结果显示（非管理员不渲染入口） |

**登录与权限行为（冻结）**：① 未登录触发写操作或进入需登录子视图 → `setPage("profile")`（登录表单），登录成功后切回原子视图（沿用现有"登录后回到此前页面"模式，子视图状态保留）；② 未登录直达管理员审核子视图 → 同跳 profile；③ 已登录非管理员直达管理员子视图 → 视图内显示 403 提示卡（不跳走）；④ access token 过期 → 沿用 AuthContext 现有统一处理（刷新失败 → 会话失效 → 回 profile 未登录态）；⑤ 匿名浏览社区只读内容正常展示（D46 可选认证）。

**上传交互精确行为（D24 扩展，冻结）**：选图即限 3 张（选择器阻止第 4 张）；已选图片本地 objectURL 预览（移除/组件卸载时 `revokeObjectURL`）、可删除、**可拖拽排序**；点"发布"并行上传（`AbortController` 支持取消）；**部分失败时：失败项可单独重试或移除，剩余全部成功即可发布**；发帖失败（含 422 幂等冲突）保留文本与 attachment_ids（预览改用返回 URL），重试不重传；移除已上传图不调后端删除（交 24h 孤儿清理）；离开子视图不存草稿；错误文案：网络异常"网络异常，请重试"、401 跳登录（行为①）、422 按 code 文案、429"操作太频繁，请稍后再试"、502"服务繁忙，请稍后再试"（**唯一 5xx 状态；前端只按 code 处理，不读 retryable**）。

**渲染安全（D14 落地，冻结）**：所有用户内容一律按纯文本转义渲染（React 默认转义），**禁止 `dangerouslySetInnerHTML`**；**URL 全部按普通文本显示，不自动识别为链接**（自动链接化为第二阶段内容）。

**页面验收方式（冻结）**：以**手工走查清单**为主（不强制新增 E2E；现有 Playwright 配置保留）；前端单测用 msw mock API；走查路径：匿名浏览 → 跳 profile 登录 → 回社区 → 申请建吧（含同名冲突即时反馈）→ 管理员审核通过/拒绝 → 发图文帖（含 3 图、部分失败重试）→ 看图 → 回复 → 点赞 → resolve → 删回复/删帖 → 通知；状态覆盖：空列表、加载失败、401/403/404/409/422。

## 十、源码版本固定与移植边界（D18/D2）

- Phase 0 clone 两仓库 release tag：**"最新 release" = Phase 0 开始执行当日在 GitHub releases 页获取的最新 tag**，tag + commit SHA 记录进附录 D 后冻结，后续不重取；
- 允许参考：FlaskBB `flaskbb/forum/`、`flaskbb/utils/`；bbs-go `web/`（布局）、`server/` 附件设计；
- 禁止复制：FlaskBB templates/插件/auth；bbs-go 后端 Go 代码与前端路由结构；
- LICENSE 收录 `docs/licenses/`；来源注释按文件类型（D2）。

## 十一、分阶段实施计划

> 每阶段验收含「十二、测试矩阵」相关项。门禁：`ruff/mypy`/单测/集成/契约（`scripts/ci-local.sh`）。

### Phase 0 · 准备（0.5 天）
0.1 clone FlaskBB/bbs-go release tag（D18）至 `.local/reference/`，记录 tag+SHA；0.2 产出《移植映射表》（**格式按附录 D 冻结表头**，含 §7.17 调用路径逐项落点），LICENSE 收录 `docs/licenses/`；0.3 注册七牛 + 实名 + 建公开读测试桶（所有者人工）。
**验收**：映射表评审通过；凭证到位。

### Phase 1 · 存储层（1 天）
1.1 `backend/community/storage/`：StorageService + KodoStorage（async，`anyio.to_thread`，超时/错误分类按 §7.9）+ LocalStorage（含 §7.12 local-uploads 路由、数据库查找规则、相对 URL）；1.2 §7.10 全部"MVP 做"校验（MIME 映射表、Pillow 调用顺序、**显式像素计算**、启动时设置 MAX_IMAGE_PIXELS）；1.3 §7.9/§7.15 配置合同落地 `backend/settings.py`（含合法范围 Field 约束与生产启动强校验）+ **同步更新 `.env.example`**（D49）；1.4 pyproject 增加 `qiniu>=7,<8`、`pillow>=11,<13` 并 **`uv lock` 同步 uv.lock**（D49）；1.5 §7.11 补偿与幂等。
**验收**：§十二存储项全过；`backend-lint backend-unit` 全绿。

### Phase 2 · 数据模型与迁移（1.5 天）
按 §7.2 精确清单实施 `0002_community_v2`：清库（含 boards）→ seed 纯 INSERT → boards/notifications/idempotency 三表 ALTER（约束名按 §7.2 规则解析）→ 按 §7.3 完整 DDL 建两张新表 → `downgrade()`。
**验收**：§7.2 验收口径 ①–⑦ 全过；`backend-integration` 全绿。

### Phase 3 · 服务层与 API（2 天）
实现 §八全部接口（flag 清单 D11）、§7.14 并发语义（含 D38）、§7.17 全部调用路径改动（含 `get_optional_auth_context` 与读接口切换）、附件绑定、§7.12 清理流水线并入 CommunityMaintenance、§7.7 证据链、§7.8 新通知（含既有两类补 board_slug）、§7.13 限流组、`require_community_admin`（`ADMIN_REQUIRED`）与 permissions、新错误码注册进 `COMMUNITY_ERROR_CODES` + 异常类、契约快照更新（`UPDATE_OPENAPI_SNAPSHOT=1` + review）。
**验收**：§十二全部后端项全过；`contracts` 全绿。

### Phase 4 · 前端（2 天）
按 §九实现 6 个子视图 + 上传交互精确行为 + 登录/权限行为 + 渲染安全规则；布局对照 bbs-go、视觉纸张风格、状态导航。
**验收**：§十二前端项全过；`npm run lint / test / build` 全绿；手工走查清单逐项通过。

### Phase 5 · 云服务器部署（1 天；交付物为配置与文档，真实部署为独立执行项）

| 任务 | 验收标准 |
|---|---|
| 5.0 **服务器升配** → 2核4G；加 2G swap；安全组只开 80/443/22 | 完成（所有者人工） |
| 5.1 `docker-compose.prod.yml`（**新增文件，不由开发 compose 改造生成；现有开发用 `Dockerfile` / `frontend/Dockerfile` / `docker-compose.yml` 一律不动**）**精确服务清单**：nginx、certbot、postgres、memory-api、memory-worker、memory-scheduler、memory-outbox-consumer、conversation-worker、**conversation-outbox-publisher（沿用开发 compose 实际服务名，不重命名）**、backup、**migrate（一次性 job，命令见 §7.16）**；各 1 实例；生产与开发服务清单允许不同（生产多 nginx/certbot/backup/migrate，无 vite preview 前端容器——**前端构建改为一次性 build job：`node:24` 系补丁版镜像内 `npm ci && npm run build` 产出 `frontend/dist`，挂载进 nginx 托管**）；自研镜像**服务器本地 `docker compose build`**（compose 工作目录 `/opt/xueshen`，git clone/pull 源码，MVP 不用 registry），tag=git short SHA；**生产构建复用现有根 `Dockerfile`，P5 执行时将 FROM 行 `python:3.13-slim` 改为抄录的补丁版 tag（如 `python:3.13.x-slim`）并作为独立 chore 提交**（开发环境同受益于该固定）；生产默认值（冻结）：库名 `memory/auth/conversation/community/rag`、最小权限角色 `memory_app/auth_app/conversation_app/community_app/rag_app`、env 文件 `.env.production`（chmod 600，不入库）；healthcheck：postgres `pg_isready`、memory-api `/health/ready`、nginx `nginx -t`。**镜像与工具版本冻结总规则（冻结）**：**禁止一切浮动 tag（latest/stable/lts/alpine 等滚动别名）；精确补丁版 tag 即可，digest 仅 certbot 强制**——postgres 抄录 `postgres:17.x` 补丁版（与开发 compose 大版本 17 一致）；nginx 抄录 stable 补丁版 tag（P5 执行时从 Docker Hub 抄录）；certbot 用 digest 固定；backup 镜像：若有现成镜像则固定补丁 tag，若自建（alpine + apk add dcron postgresql-client）则 alpine 固定补丁 tag、apk 包实际安装版本写入 5.6 表；前端构建镜像 `node:24.x-slim`（沿用开发 24 系，P5 抄录补丁版，**不降 22**）；服务器 Docker Engine ≥ 24、Docker Compose v2 ≥ 2.20（安装时核验 `docker version` / `docker compose version` 并记录）。以上"执行时抄录"的值确定后**全部写入 5.6 部署记录表**，写入后才允许进入 5.7 | 本地 compose（HTTP profile）拉起 |
| 5.2 **启动顺序（命令级冻结）**：① postgres healthy → ② migrate job（按 §7.16 链清单逐条 upgrade head + `sync-knowledge-graph --apply`，`restart: "no"`）→ ③ memory-api（`depends_on: migrate=service_completed_successfully`）→ ④ workers/publishers → ⑤ nginx/certbot → ⑥ 冒烟 | 文档可照做 |
| 5.3 **nginx 配置冻结**：`/api/` → memory-api:8000；`/` → 前端静态（`try_files $uri /index.html` 兜底）；**SSE 路由 `proxy_buffering off; proxy_cache off; proxy_read_timeout 3600s;`**；**`/api/v1/community/uploads` 单独 location `proxy_read_timeout 480s`（≥ 上传超时上限 420s + 余量，§7.11）**；`client_max_body_size 8m`；`/health/ready` 不对外暴露；证书切换 reload 断 SSE 长连接（前端自动重连，接受） | 配置就绪 |
| 5.4 **certbot**：webroot 模式；**staging→正式流程（冻结）：先以 `--staging` 跑通签发 → 移除 `--staging` 并加 `--force-renewal` 重新申请正式证书 → 验证 nginx reload 后 HTTPS 可达；回滚 = 恢复 HTTP-only 配置 + 删除问题证书目录**；首申失败保持 HTTP-only + 12h 重试 + 日志告警；**所有者外部输入清单：域名、CDN 域名（七牛 CNAME）、DNS A 记录、联系邮箱、接受 LE 条款** | 文档化 |
| 5.5 **backup 容器**：alpine + postgresql-client + dcron；**wrapper 脚本（冻结）：逐库 `pg_dump` 自定义格式到 `/backups`（`{db}-{yyyymmdd}.dump`），任一失败 → 错误日志 + 创建 `/backups/FAILED-{yyyymmdd}` 标记 + 脚本非零退出（cron 任务级；容器常驻不退出）；全部成功 → 删除当日 FAILED 标记 + `find -mtime +7 -delete`**；默认四库，study/rag 按启用加入；密码 env_file；备份 0600；失败发现 = runbook 巡检（MVP 不做主动告警）；**恢复演练 = pg_restore 到新建临时库，恢复后按需 alembic upgrade** | 含一次恢复演练 |
| 5.6 更新 docs/ops/startup.md：**部署记录表（格式冻结）**：`\| 项 \| 值 \| 确认人 \| 确认日期 \|`，行 = postgres tag、nginx tag、certbot digest、alpine tag + postgresql-client apk 版本、python/node 基础镜像补丁版、Docker Engine/Compose 版本、git commit、回滚用旧 commit、前端产物目录、域名/CDN 域名/DNS、Kodo 五项（脱敏：只记是否已配置）、管理员 UUID 名单（脱敏）、升配完成；**owner = 项目所有者本人，review = 所有者本人在该表签字（逐行填确认日期）；以上任一行未确认 → 禁止进入 5.7**；另含环境变量清单、启动顺序、readiness、回滚（先 downgrade 再切旧镜像） | 评审通过 |
| 5.7 真实部署（**独立执行项**，前提：5.6 记录表全部确认；无 staging） | 冒烟：注册登录 → 申请建吧 → 审核 → 发图文帖 → 图片 CDN 可见 → 证据落库（仅文本） |

### Phase 6 · 第二阶段增强（按需排期）
吧主治理（含 pinned/hidden 入口）、编辑与草稿、回复带图、TipTap、拖拽粘贴/灯箱/服务端缩略图/9 图、URL 自动链接化（仅 https 白名单）、EXIF 清除、新申请通知管理员、举报与敏感词库、图片内容审核、板块关注/热门排序/全文搜索、备份上传 Kodo 与主动告警、多实例 advisory lock、Kodo 孤儿对象对账、学习小组与打卡圈。

## 十二、测试与验收矩阵（冻结）

**存储**：合法 jpeg/png/webp 成功；**Content-Type=jpeg 实为 PNG 接受并按 PNG 校正**；Content-Type 缺失/不合法走 Pillow 检测；GIF 拒绝；无法解码拒绝；>5MiB 拒绝（流式即断）；恰好 5MiB 通过；**40MP 通过、>40MP 显式计算拒绝 `UPLOAD_BOMB_REJECTED`（不依赖 Pillow warning 语义）**；`Image.MAX_IMAGE_PIXELS` 启动时等于 settings 值；Kodo 5xx/超时 → `COMMUNITY_UPLOAD_FAILED` retryable=true、4xx/缺 key → retryable=false；SDK 调用不阻塞 event loop；校验失败不产生 Kodo 对象；Kodo 成功+DB 失败补偿删除被调用且幂等记录随回滚不存在；删除 612 按成功；**local 模式删除走同一流水线（文件不存在视为成功）**；**local-uploads：200、Content-Type=数据库 mime、无 DB 记录 404、orphaned 404、deleted 未清理仍可访问、路径穿越拒绝、文件不存在 404、无认证头可访问**；**url 字段 local=相对路径（不含 Host）、kodo=https 绝对 URL**；**original_filename 清洗按 §7.3 伪代码逐条断言（5 组冻结样例 + Cf 格式字符删除 + 删除后再 trim + 按字符截断）**；**KODO_CDN_DOMAIN：大写输入自动转小写通过；单标签 / label 首末连字符 / 带端口 / punycode（`xn--`）/ scheme / 路径 / 末尾斜杠 / 连续点 → 启动抛错；Region 合法值逐个通过、非法值启动抛错、合法列表内容不可变断言**。

**附件绑定**：3 张成功且 position 从 0 连续、顺序稳定；第 4 张 422；绑定他人 403；已绑定/不存在/已清理 409；**请求内重复 attachment_id 422**；同帖同 position 并发由部分唯一索引拒绝；发帖事务失败附件保持 uploaded；**orphan 转换 SQL 正确（TTL 锚点=created_at）**；**重试时序表逐项：初次失败 attempts=1 next=+1h、第 2 次 +4h、第 3 次 +12h、第 4 次进终态（next=NULL、exhausted+1、last_delete_error 保留、不再被扫描命中）**；删帖后附件 deleted、成功写 storage_deleted_at 且 delete_attempts 不重置、**30 天物理删除 SQL 命中且要求 storage_deleted_at 非空**；**orphaned 物理删除时同事务删除其幂等记录（此后同 key 重传成功）**；发帖失败复用已上传附件成功；**清理逐条事务边界（存储调用在事务外）；人工重置 SQL 可恢复终态记录**；**maintenance 两阶段断言：阶段一旧清理仍为单事务、阶段二附件清理在阶段一提交后执行；每附件独立 session/事务；单附件存储删除失败不中断同轮其他附件；扫描查询条件 = `status IN ('deleted','orphaned') AND next_delete_attempt_at IS NOT NULL AND next_delete_attempt_at <= now()` 且按 next_delete_attempt_at ASC LIMIT batch；orphan 转换 / 删除流水线 / deleted 物理删除各自最多 500 条（非整轮合计）**。

**建吧**：登录可提交、未登录 401；**申请时 boards 同名/同 slug → 409 `BOARD_NAME_CONFLICT` 立即返回（不产生 pending）**；第 2 个 pending 409 `APPLICATION_DUPLICATE_PENDING`；**他人 pending 占名（并发唯一索引兜底）→ 409 `BOARD_NAME_CONFLICT`**；重复/并发审核只成功一次（行锁）；已审核再审核 409；拒绝后可重新申请（同 slug 立即可用）；approve 时 boards 冲突 409（**整体回滚、申请保持 pending**）；保留 slug 全部 422；大小写/空白变体重名；**状态不变量 CHECK 生效且 approve 单语句转换不瞬时违反**；board_id UNIQUE；非管理员 403 `ADMIN_REQUIRED`；通过后板块出现在公开列表、created_by=申请人、post_count=0、sort_order=100、申请 board_id 已回填；通知正确（§7.8 全字段）；**申请 DTO 不含 reviewer_id**；pending/rejected 不在公开列表；新板块排在 seed 之后；**mine `created_at DESC, application_id DESC`、admin `created_at ASC, application_id ASC` 同时间戳无重复/漏项；跨 status 复用游标 → 422 `COMMUNITY_CURSOR_INVALID`**。

**权限与认证**：只能删自己内容；未登录写 401；**匿名读接口 200 且 viewer_liked/viewer_is_author/viewer_is_owner=false；无效/过期 token 读接口 → 401（不匿名降级）；匿名请求不产生 Auth DB 会话**；**匿名合同（D46a–d）逐项：带 Authorization 头时优先走 JWT（dev 环境同）、无 Authorization 才读 X-Dev-User-Id；**空串与仅空白 Authorization 头 = 匿名**（可选依赖 strip 后判定；写接口空白仍 401）；匿名限流仅 IP 桶、resolve_client_ip 为 None 时走固定 `ip:unknown` 共享桶（不跳过不拒绝）；读模型 viewer_user_id=None 时不发起 likes 查询**；**hidden 板块读取（§7.6）：hidden 板块不在列表、`GET /boards/{slug}` 404、按其 board_id 查帖子流 404、全局帖子流 join 过滤不出现 hidden 板块帖子、hidden 板块帖子按 ID 直达 404、hidden 板块内回复/点赞/resolve/删除 404、发帖 409 `COMMUNITY_BOARD_DISABLED`**；**重复删除语义（§7.14）：已删除+作者 → 200 幂等成功（计数不重扣、deletion 事件不重复）；已删除+非作者 → 404；并发双删均 200 且 Outbox 仅一条删除事件；回复删除同语义（含 solved 清除不递增 generation）**；软删帖 404、软删回复占位；点赞幂等；管理接口非管理员 403 `ADMIN_REQUIRED`；`COMMUNITY_ADMIN_USER_IDS` 解析/空值/非法 UUID/生产强校验；permissions 正确反映身份。

**证据链**：帖子证据=`"标题：{title}\n正文：{body}"` 逐字等式、回复证据=strip 后 body；图片 URL 不在 payload；payload 与 §7.7 逐字段一致；hash = sha256(上述字符串，UTF-8)；删除投递 deletion 事件；Outbox 与业务同事务（回滚则无事件）；重复消费幂等；发帖失败无事件；审核通知只写 notifications 表、无 Outbox 事件、不重复；**post_replied/reply_marked_solved 通知 board_slug 已填入**。

**计数一致性与删除原子性**：发帖/删帖/purge 后 `boards.post_count` 与实际 active 帖子数一致（含 purge 批量路径）；purge 后该用户帖子附件全部转 deleted 进清理流水线；**并发双删同帖：`post_count` 只减 1（条件 UPDATE 影响 1 行才扣计数）、两请求均 200、Outbox 仅一条删除事件；并发双删同回复：`reply_count` 只减 1；删 solved 回复与 resolve 并发：最终值为两种合法结局之一且 posts 行状态自洽；计数 CHECK(`>=0`) 违反时不兜底、抛错转人工**；**§7.17 逐 SQL 写入点清单 9 类路径 updated_at 逐项断言已更新（D50）**。

**契约与迁移**：OpenAPI 快照更新 + review（event_type 扩展、board_slug、attachments 恒返回、boards 新字段、申请对象无 reviewer_id、读接口认证可选化）；保留接口兼容（**字段名 body、like/resolve/delete `{"status":"ok"}`、详情 `{post,replies}` 信封、**`COMMUNITY_IDEMPOTENCY_CONFLICT`=422****）；空库迁移、**seed 断言 board_id=冻结 UUID**、重复迁移 head 幂等、`upgrade→downgrade→upgrade`；**旧业务数据清空断言（含 boards 只剩 4 行）**；0002 ALTER 断言（新幂等 operation/resource_type、四值 event_type、boards 新列、sort_order 默认 100）；约束名解析 0/多匹配 → RuntimeError；**0002 破坏性门禁：含非 seed 板块行 / 含任意业务行的库 → RuntimeError 拒绝执行；纯 seed 库与空库通过；重复 upgrade 幂等**；`backend-integration` 全量；flag=false/true 两组路由测试 + local-uploads 仅受 backend 控制；`resolve` 回归；**422 三分断言（REQUEST_EXTRA_FIELD / INVALID_PAYLOAD（含 Idempotency-Key 格式非法）/ COMMUNITY_CONTENT_INVALID（含缺失））**；**新错误码全部注册进 COMMUNITY_ERROR_CODES 且异常类 http_status 与映射表一致**。

**上传幂等**：同 key 同文件返回原 attachment_id（不产生第二个 Kodo 对象）；同 key 不同文件 **422** `COMMUNITY_IDEMPOTENCY_CONFLICT`；**同 key 并发只有一个执行体（后到事务阻塞等待先到者提交/回滚后走重放/冲突/继续分支）**；申请同 key 重放返回原申请对象；不同 operation 复用同 key 互不影响；**`attachment_ids` 顺序不同 → 422、null/缺省/[] 视为同 payload**；**orphan 清理后同 key 重传 = 重新上传成功（幂等记录已随附件删除）**；**事务边界断言：校验发生在事务开始前、Kodo/文件写入期间事务保持打开且不先提交幂等记录、INSERT attachments 与幂等记录同 COMMIT；local 模式同一边界**；**取消/断开语义：to_thread SDK 调用继续执行完、请求结束事务回滚、Kodo 已传成功走补偿删除（被调用断言）、local 孤儿文件接受**；**`COMMUNITY_UPLOAD_FAILED` retryable 映射逐项：5xx/连接异常/超时/未知异常=true，4xx（含上传路径 612）/200 缺 key=false；HTTP 唯一 502；信封按实例级 retryable 序列化**。

**限流与配置**：新增三组配额生效（20/5/60）；超限 429 格式沿用现有；**settings 越界启动抛错（抽样：image_max_bytes=5242881、attachment_max_per_post=4、board_name_max_chars=21、超时=0）**；`uv.lock` 与 pyproject 一致（`uv sync --frozen` 通过）；`.env.example` 含全部新增变量。

**前端**：选图第 4 张被阻止；部分失败可单独重试/移除后发布；发帖失败文本与附件保留；objectURL 正确 revoke；未登录触发写操作跳 profile、登录成功回原视图；**匿名浏览只读内容正常**；非管理员直达审核子视图显示 403 提示卡；管理员入口按 permissions 显示；用户内容纯文本渲染（`<script>` 原样显示）；URL 显示为纯文本不可点；`npm run lint / test / build` 全绿；手工走查清单（§九）逐项通过。

## 十三、风险与注意

1. 同步 → async 改写：DB 走 async 会话、Kodo SDK 走 `anyio.to_thread`，严禁同步调用进 event loop。
2. 凭证安全：AK/SK 只在服务器环境变量；`.env`/`.env.production` 不入库。
3. EXIF 隐私：MVP 不清除，第二阶段处理，上线公告提示。
4. 图片内容审核缺口：MVP 仅格式级校验。
5. 契约快照：新增接口必须更新快照并 review。
6. CDN 延迟失效：删帖后 CDN 缓存期图片可能仍可访问，MVP 接受。
7. 清空旧业务数据（含 boards）的 0002 迁移只能在"无生产数据"前提下执行；前提变化必须暂停并重新评审迁移方案。
8. 0002 中 CHECK 约束替换依赖 0001 自动约束名：按 §7.2 规则查询，恰好 1 条才 DROP，否则抛错转人工。
9. 清理流水线崩溃恢复 = 重删（612/文件不存在视为成功），属预期行为；终态记录靠 metrics + runbook 人工处理。
10. 前端无 URL 路由：子视图刷新后回到社区首页（状态不持久），MVP 接受；可分享链接需求第二阶段评估 hash 路由。
11. **Kodo 孤儿对象窗口（D48）**：上传成功+DB 写入前崩溃产生无记录孤儿对象，MVP 接受（成本可忽略）；第二阶段评估对账。
12. 读接口认证可选化是行为变更（原必需认证）：前端需同步处理匿名态（§九），契约快照 review 时重点确认。

## 十四、工作量估算

| 阶段 | 预估 |
|---|---|
| P0 准备 | 0.5 天 |
| P1 存储层 | 1 天 |
| P2 数据模型与迁移 | 1.5 天 |
| P3 服务层与 API | 2 天 |
| P4 前端 | 2 天 |
| P5 部署（配置与文档） | 1 天 |
| **MVP 合计** | **约 8 天** |

## 十五、附录

### 附录 A：七牛 Kodo 名词与准备清单（小白版）

- **AK/SK**：七牛账号密钥对，相当于账号密码，只放服务器环境变量。七牛控制台 → 密钥管理。
- **Bucket（桶）**：存图的仓库。公开读 = 知道 URL 就能看（社区图公开，选它最简单最快）。
- **CDN 加速域名**：全国缓存节点，看图快、省流量费。绑 `img.你的域名.com`。
- **Region**：机房位置，选华东（`z0`）。
- **准备清单**：① 注册七牛 + 实名（免费，早期月成本约几元）；② 建测试桶与生产桶各一个；③ 把 AK/SK、桶名、Region 给实施者。

### 附录 B：云服务器方案（小白版 + 升配测算）

**为什么建议升级到 2核4G**（当前 2核1.6G）——内存占用测算：

| 组件 | 估算内存 |
|---|---|
| PostgreSQL（含多库） | 400–600 MB |
| memory-api（FastAPI 主进程） | 300–400 MB |
| 5 个 worker/scheduler/publisher | 各 100–200 MB，共 0.6–1 GB |
| nginx + certbot + backup | ~80 MB |
| 系统本身 | ~400 MB |
| **合计** | **约 2.2–2.6 GB** |

1.6G 内存必然频繁 OOM，2C4G 是稳妥下限。阿里云控制台"升降配"在线变更（差价约每月十几到几十元）。磁盘 40G 足够（图片在 Kodo，数据库早期 <5G，备份保留 7 天）。另建议：① 加 2GB swap 兜底；② 带宽 1–5Mbps 即可（图片走 CDN）；③ 安全组只开 80/443/22；④ 系统用 Ubuntu 24.04 LTS。

- **docker-compose**：数据库、后端、前端、后台进程各为"集装箱"，一条命令启动互联。
- **域名**：约 ¥30-60/年，强烈建议购买并解析到服务器 IP。
- **HTTPS**：certbot 容器自动申请/续期免费证书；首次失败站点以 HTTP 运行、自动重试。
- **准备清单**：① 控制台升配到 2核4G；② 购买域名并解析；③ P5.7 前到位即可，不影响 P0–P4。

### 附录 C：来源

- FlaskBB：https://github.com/flaskbb/flaskbb （BSD-3；PyPI https://pypi.org/project/FlaskBB/ ）
- bbs-go：https://github.com/mlogclub/bbs-go （MIT；`web/package.json`；releases 页）
- 落选项目分析：`docs/community-oss-candidates.md`（调研留档）
- 代码核实：`contracts/api.py`（DTO 规则 §6.6、请求模型）、`api/community.py`（真实响应模型、读接口必需认证现状、Idempotency-Key Header pattern）、`contracts/errors.py`（**现有 7 个错误码与 HTTP 状态：含 IDEMPOTENCY_CONFLICT=422**）、`backend/shared/auth_context.py`（`get_auth_context` 必需认证、`require()` 抛 AUTH_FORBIDDEN）、`backend/shared/cursor.py`（**游标 payload 实际结构 = cursor_version/route/normalized_filters/sort_key/expires_at[/principal_hash]**）、`backend/auth/context.py`（AuthContext.user_id 不可空）、`backend/auth/verifier.py`（**凭证优先级：Authorization 优先于 X-Dev-User-Id**）、`backend/community/api/dependencies.py`（rate_limit 依赖现行必需认证、公共/私有游标解析）、`persistence/outbox.py`（insert_event `ON CONFLICT (idempotency_key) DO NOTHING`）、`persistence/posts.py`（mark_post_deleted/set_solution/decrement_reply_count）、`persistence/notifications.py`、`services/post_command_service.py`（幂等模式、canonical_json hash、Outbox payload、**delete_post 重复删除幂等成功现行为**、`_enqueue_source_deleted` 确定性 UUIDv5）、`services/reply_service.py`（delete_reply 同语义 + solved 清除）、`services/content_safety.py`（validate 不拒绝 HTML；hash 公式逐字）、`services/post_service.py`（viewer_user_id: UUID 签名点）、`api/internal_accounts.py`（purge 路径）、`services/maintenance.py`（**`_run_once` 现行为单事务四步清理，§7.12 两阶段改造基准**）、`community_migrations/versions/0001_community_core.py`（逐行核实）、`backend/settings.py`（现有 community_* 配置项）、`app.py`（lifespan、条件挂载）、根 `Dockerfile`（`python:3.13-slim` 浮动 tag 现状）、`frontend/Dockerfile`（`node:24-slim` + vite preview 现状）、`docker-compose.yml`（`postgres:17`、`conversation-outbox-publisher` 实际服务名）、`frontend/src/App.tsx`（PageKey 状态导航）、`frontend/src/pages/Profile.tsx` + `auth/AuthContext.tsx`（登录表单与会话失效处理）

### 附录 D：移植映射表（Phase 0 产出物，表头冻结）

> Phase 0.2 完成时按下表头填写（每行一个移植单元；tag/SHA 以 Phase 0 执行当日获取的 release 为准）：

| 来源项目 | tag | commit SHA | 来源文件 | 来源函数/模型 | 移植方式（直接复制/参考改写/仅参考） | 目标文件 | 目标函数/组件 | 依赖改写点 | LICENSE/来源注释位置 | owner | review 状态 |
|---|---|---|---|---|---|---|---|---|---|---|---|
| （示例）FlaskBB | vX.Y.Z | `<sha>` | `flaskbb/forum/models.py` | `Topic` | 参考改写 | `backend/community/persistence/posts.py` | `insert_post` | sync→async、计数同事务 | 文件头 `#` 注释 + docs/licenses/ | | 待评审 |

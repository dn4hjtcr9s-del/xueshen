# Community 社区功能设计与实施方案

> 版本：v1.6 · 2026-08-14
> 状态：关键决策已冻结（D1–D28，v1.6 增补 D29–D47 见 §19），执行级冻结物已补齐（§6.6 公共 DTO 与文案、§7.1 板块 seed 文案、§7.5 Outbox 列集与 payload schema、§13.1 readiness 集成、§19 Page 信封与错误码分级），可按 Phase C0–C4 实施
> 适用分支：`main`
> 关联模块：Auth、Conversation、Memory、前端 `CommunityPage`

## 1. 方案摘要

当前前端已经有社区入口和视觉雏形，但 `CommunityPage` 仍使用静态 Mock 数据：讨论区只能展示帖子，学习小组和打卡圈也没有真实后端行为。本方案先把讨论区做成一个可用的社区闭环，并为学习小组、打卡圈保留扩展边界。

第一阶段（MVP）实现：

- 登录用户查看讨论区、板块和帖子详情；
- 发帖、回复、点赞、标记已解决；
- 删除自己的帖子或回复，使用软删除；
- 游标分页、幂等创建、基础限流和内容安全校验；
- 所有业务数据从认证上下文取得 `user_id`，不信任浏览器提交的用户 ID；
- 发帖和回复异步提交为 `ActivityEvidence`，由 Memory 总结为发帖人的私有学习记忆；
- 社区内容删除后，向 Memory 投递 activity source deletion，阻止已删除内容继续作为记忆来源；
- 保持 Conversation 的私有会话模型不变，社区帖子不直接写入 Conversation thread。

暂不在 MVP 中实现：完整学习小组管理、私信、关注关系、实时 WebSocket、图片上传、复杂推荐、声望体系、自动化审核后台和嵌套楼中楼回复。

## 2. 当前项目基线

### 2.1 前端现状

`frontend/src/pages/Community.tsx` 已接入 `App.tsx` 的侧边导航，页面包含三个 Tab：

| Tab | 当前状态 | MVP 处理 |
|---|---|---|
| 讨论区 | 使用 `communityPosts` 静态数组，展示标题、板块、作者、时间、回复数和点赞数 | 接入真实 API，补充发帖、详情、回复、点赞和删除 |
| 学习小组 | 使用 `studyGroups` 静态数组，加入按钮无后端行为 | 保留展示占位，第二阶段实现 |
| 打卡圈 | 使用静态月历和排行榜 | 复用已有 `check_in` 行为证据契约，第二阶段实现 |

现有视觉约束应继续保留：纸张色、编辑/社论风格、红色强调色、窄侧边导航和当前 `styles.css` 的按钮/卡片体系。

### 2.2 认证与 `user_id`

认证服务已经提供统一内部身份：

- `auth.users.user_id` 是全局 UUID；
- JWT 的 `sub` 是外部身份，后端通过身份映射解析为内部 `user_id`；
- FastAPI 通过 `AuthContext.user_id` 获取当前用户；
- 浏览器不得提交或覆盖 `user_id`；
- 普通用户 actor 为 `user`，Memory 证据提交仅允许受信任的 `activity_agent` 或 `conversation_agent`。

社区 API 必须复用现有 `get_auth_context`/认证依赖。请求体中的 `user_id`、`actor_type`、`memory_operation_id` 等字段一律不接受。

### 2.3 Conversation 与 Memory 现状

Conversation 已有成熟的用户隔离、Outbox、证据提交、SourceReader 和删除语义。Memory 已定义：

```text
ActivityEvidence.activity_type:
forum_post / forum_reply / wrong_question_upload / exercise_attempt /
review_result / page_view / bookmark / check_in
```

但是当前运行时仍使用 `_UnavailableActivityReader`，无法从真实社区数据读取正文。社区功能需要补上与 `HttpConversationReader` 对称的 `HttpActivityReader` 以及 Community 内部 Source Reader API。

Memory 对 `forum_post` 和 `forum_reply` 会进入总结记忆分支，而不是仅更新活动曝光计数。因此社区行为可以复用现有契约：

```text
Community post/reply
    → Community Outbox
    → ActivityPublisher
    → ActivityEvidence(forum_post/forum_reply)
    → Memory Summary Graph
    → 发帖人自己的 learner/mastery memory
```

## 3. 目标、原则与非目标

### 3.1 目标

1. 让当前社区页面从 Mock 数据升级为真实的讨论交流页面。
2. 让用户可以安全地发布、阅读和删除自己的社区内容。
3. 让社区内容成为发帖人自己的学习证据，而不是把公共内容直接当作所有人的事实。
4. 让 Memory 写入不阻塞发帖请求，并且具备重试、幂等和删除一致性。
5. 保持 Auth、Conversation、Memory 的领域边界，不因社区高频读写污染私有会话和记忆表。

### 3.2 设计原则

- **身份来自服务端上下文**：所有归属判断使用认证后的 `AuthContext.user_id`。
- **公共内容与私有记忆分离**：社区内容可以被其他用户阅读，但总结后的 Memory 只属于作者。
- **先保存，再异步总结**：社区交互优先保证可用，Memory 暂时不可用时不回滚已发布帖子。
- **删除优先于总结**：删除竞态时不再投递新的证据；已投递的来源记录 deletion fact。
- **默认少暴露数据**：公开作者只展示用户名/显示名，不返回邮箱和内部身份映射。
- **MVP 保持简单**：纯文本、平面回复、固定板块，避免第一版引入富文本和复杂权限。

### 3.3 非目标

- 社区不是 Conversation 的共享线程；
- 不把其他用户的帖子自动写进当前用户的 Memory；
- 不把点赞、浏览、加入小组直接总结成长期学习事实；
- 不在 MVP 中实现大规模内容推荐和实时同步；
- 不让浏览器直接调用 `/api/v1/memory/events`。

## 4. 总体架构

### 4.1 领域边界

新增独立 Community 域和独立 `community` 数据库，理由是社区内容是高频公共读写数据，与 Conversation 的用户私有线程和 Memory 的总结版本生命周期不同。

```text
┌──────────────┐       ┌────────────────────┐
│ Browser      │──────▶│ FastAPI App        │
│ Community UI │       │ Auth + Community   │
└──────────────┘       └─────────┬──────────┘
                                  │
              ┌───────────────────┼────────────────────┐
              │                   │                    │
       ┌──────▼──────┐     ┌──────▼──────┐      ┌──────▼──────┐
       │ Auth DB     │     │ Community DB│      │ Conversation │
       │ user_id      │     │ posts/...   │      │ DB          │
       └─────────────┘     │ outbox      │      │ private     │
                           └──────┬──────┘      └─────────────┘
                                  │
                           ┌──────▼──────┐
                           │ Embedded    │
                           │ Publisher   │
                           └──────┬──────┘
                                  │ ActivityEvidence
                           ┌──────▼──────┐
                           │ Memory API  │
                           │ Summary     │
                           └─────────────┘
```

### 4.2 数据库选择

MVP 唯一采用独立 Community database，不再保留“Conversation DB 独立 schema”作为实施备选。目标部署增加：

- `community` database；
- `community` 非超级用户账号，只能连接和管理 Community database；
- `community_migrations/` 独立迁移链；
- `COMMUNITY_DATABASE_URL` 配置项。

Community 表只保存 `user_id`，不跨库创建 Auth 外键。用户合法性由认证服务和业务层保证。这样可以保持 Auth、Conversation、Memory、Community 四个应用账号的最小权限边界，避免社区查询获得密码、refresh token 或私有会话数据。

部署改造必须同时覆盖：

1. `docker-compose.yml` 的 Community 连接配置和 health check；
2. 首次初始化 PostgreSQL 的 initdb 脚本；
3. `scripts/postgres_roles_upgrade.sh` 的存量 volume 幂等升级，新增 `community` role/database、撤销 PUBLIC CONNECT 并收敛授权；
4. 本地和生产环境示例配置。

若环境暂时不能创建第四个应用数据库，则该环境不启用 Community 写路径，而不是把表临时落入 Conversation。这样避免临时方案固化为长期耦合。

### 4.3 进程与模块规划

建议新增或调整：

```text
backend/community/
├── api/
│   ├── community.py              # 公共社区 REST API
│   ├── internal_sources.py       # Memory 读取社区来源的内部 API
│   └── dependencies.py           # Community runtime、分页和权限依赖
├── contracts/
│   ├── api.py                    # 请求/响应 DTO
│   ├── domain.py                 # 帖子、回复、板块状态
│   └── errors.py                 # 社区错误码
├── persistence/
│   ├── database.py
│   ├── boards.py
│   ├── posts.py
│   ├── replies.py
│   ├── likes.py
│   ├── notifications.py
│   └── outbox.py
└── services/
    ├── post_service.py
    ├── reply_service.py
    ├── public_user_profile_reader.py
    ├── source_read_service.py
    └── activity_publisher.py

backend/integrations/activity_reader.py # Memory → Community 的 HTTP Reader
community_migrations/versions/0001_community_core.py
frontend/src/api/community.ts

backend/shared/                    # 新增：跨域共享组件（D24，PR-A）
├── cursor.py                      # issue_cursor/resolve_cursor/verify_cursor 及 HMAC 底层
│                                  # （从 backend/memory/api/dependencies.py、
│                                  #   backend/memory/contracts/common.py 提取）
├── ratelimit.py                   # FixedWindowRateLimiter
│                                  # （从 backend/memory/api/dependencies.py:281 提取）
└── client_ip.py                   # 可信代理 IP resolver
                                   # （从 backend/auth_service/ratelimit.py::client_ip 提取）
```

三个原位置保留薄 re-export 以兼容现有 30+ 处引用点（`backend/memory/api/dependencies.py`、`backend/memory/contracts/common.py`、`backend/auth_service/ratelimit.py`、`backend/conversation/api/__init__.py:20`、`backend/app.py:41` 等），Community 只依赖 `backend/shared`，不反向依赖 `backend/memory/api`。

`backend/community` 以内嵌模块挂载到现有 FastAPI App。MVP 不新增独立 Community Worker 容器；`ActivityPublisher` 由 FastAPI lifespan 创建 background task，并使用数据库 lease、lease generation fencing 和可恢复 claim 支持进程重启及多 API 副本竞争。

当前 `issue_agent_token()` 只允许同一受信后端进程调用，因此 Publisher 可以按事件 `user_id` 即时签发短期 delegated `activity_agent` token，而无需把 Auth 私钥或长期 agent 凭证挂载到新容器。只有在提供受控 token broker/workload identity 后，才允许将 Publisher 拆成独立进程。

进程内 Publisher 是 MVP 起点而非长期终态（D23）。出现以下任一信号且持续超过一个观察周期（24 小时）时，必须启动独立 Publisher 容器专项（依赖 token broker/workload identity 落地）：

- `community_outbox_oldest_age_seconds` 持续超过 600 秒，或 pending 总数持续超过 5,000 条；
- 发帖/回复 p95 延迟出现可归因于 Publisher 轮询抢占的劣化（对比关闭 Publisher 的基线）；
- API 副本数 ≥ 3 且 claim 冲突率（CAS 失败 / claim 总数）持续超过 10%。

判定数据来自 §12.3 指标；专项立项前可在短窗口内先提高 `COMMUNITY_OUTBOX_BATCH_SIZE` 或缩短轮询周期缓解。

## 5. MVP 用户流程

### 5.1 查看讨论区

1. 用户进入社区页，前端请求 `GET /api/v1/community/posts`。
2. 后端验证 JWT，使用 `AuthContext.user_id`，不从查询参数读取当前用户身份。
3. 返回按 `pinned DESC, last_activity_at DESC, post_id DESC` 排序的帖子列表。
4. 前端将 `communityPosts` Mock 替换为 API 数据；加载失败显示可重试提示。

### 5.2 发帖

1. 用户点击“发起讨论”。
2. 前端填写板块、标题和正文，提交 `Idempotency-Key`。
3. 后端验证板块状态、文本长度、敏感格式和频率限制。
4. 在一个 Community DB 事务中写入帖子与 `community_outbox` 事件。
5. API 返回 201 和帖子详情；Memory 尚未完成不影响发帖成功。
6. FastAPI lifespan 中的 `ActivityPublisher` 后续读取帖子并提交 `forum_post` 证据。

### 5.3 回复

1. 用户打开帖子详情，按游标加载回复。
2. 提交回复时服务端检查帖子仍为可回复状态。
3. 事务写入回复、更新帖子 `reply_count/last_activity_at`、写入 `forum_reply` Outbox，并在回复者不是帖子作者时写入去重的社区通知。
4. 前端刷新回复列表和计数。

### 5.4 删除

1. 作者只能删除自己的帖子或回复；管理员能力后置。
2. 删除采用软删除，公共 API 不再返回原正文，保留墓碑、计数和审计所需状态。
3. 删除帖子时，同一事务将帖子标记为 `deleted`、关闭讨论，并仅为帖子来源写 activity source deletion；其他用户的 active 回复保留在墓碑详情中。
4. 删除回复时，仅处理该回复，并为该回复来源写 activity source deletion。
5. 内嵌 `ActivityPublisher` 将 deletion 事件投递到 Memory 的 `/api/v1/internal/source-deletions`。
6. Memory 的 `DeletionAwareActivityReader` 后续不会再返回这些来源。

## 6. 前端页面设计

### 6.1 讨论区 Tab

保留现有 `CommunityPage` 的列表风格，新增：

- 顶部“发起讨论”按钮；
- 板块筛选：全部、线性代数、微积分、概率论、学习方法；
- 排序：最新、未解决；“最热”作为后续扩展，避免在真实数据不足时引入不稳定的热度算法；
- 游标分页或“加载更多”；
- 加载骨架、空状态、错误重试；
- 帖子行显示置顶、已解决、板块、作者、相对时间、回复数和点赞数。

列表行点击后进入同一页面内的详情状态，不强制引入新的路由库。后续如果需要深链接，再将 `post_id` 映射到路由。

### 6.2 发帖面板

第一版使用卡片/抽屉式表单，与现有编辑器样式一致：

| 字段 | 规则 |
|---|---|
| 板块 | 必选，来自后端 `boards` 列表 |
| 标题 | 必选，1–200 个 Unicode 字符 |
| 正文 | 必选，1–19,500 个 Unicode 字符；纯文本，保留换行 |
| 记忆提示 | 不提供“记住这条”开关；作者自己的有效内容默认成为候选证据，由 Memory 决定是否形成长期记忆 |

长度校验不能只依赖正文常量。服务端创建帖子时必须先按 Reader 的真实格式构造 `标题：{title}\n正文：{body}`，同时满足：

- `len(content) <= 20_000`；
- `len(content.encode("utf-8")) <= 80_000`；
- metadata 不超过 4,096 bytes。

回复正文同样默认限制为 19,500 个 Unicode 字符，并在写入和 Reader 返回前重复执行 SourceItem 字符数、UTF-8 字节数校验。前端不显示或提交 `user_id`，作者信息来自当前登录上下文和 API 响应。

### 6.3 帖子详情页

详情页包括：

- 帖子标题、板块、作者、时间、正文；
- 置顶/已解决标识；
- 点赞按钮和当前用户是否已点赞；
- 作者操作：删除；
- 讨论回复列表；
- 回复编辑框和提交按钮；
- 主题作者可以将某条回复标记为解决答案；
- 删除后的回复显示“内容已删除”，不显示原正文；已删除帖子从默认列表移除，详情页仅显示墓碑和仍保留的回复。

第一版回复采用平面列表，不支持 `parent_reply_id`，减少排序、权限和递归加载复杂度。

### 6.4 学习小组和打卡圈

第一阶段保留现有 Tab，但不伪装成真实可用功能：

- 可以显示“即将开放”或继续显示只读 Mock，但不能让“加入”按钮产生假成功；
- 后端不为未实现的 Tab 设计临时写接口；
- 第二阶段再分别设计 `study_groups`、membership、check-in 聚合和排行榜。

### 6.5 前端 API 层

新增 `frontend/src/api/community.ts`，复用 `frontend/src/api/client.ts` 的 Bearer token 注入、401 single-flight refresh、`PublicError` 信封和 `idempotencyKey()`。

建议定义帖子/回复公共 DTO；公共 DTO 不持有内部 `user_id`，作者操作只依赖 `viewer_is_author`。列表响应只包含 `active` 帖子；详情可返回 `deleted` 墓碑，此时帖子 `title/body=null`，回复墓碑 `body=null`。`hidden` 仅为内部预留状态，不通过公共 DTO 暴露。完整字段集与文案冻结见 §6.6。

全局通知面板在 `App.tsx` 使用统一展示模型：

```ts
type UnifiedNotification = {
  source: "memory" | "community";
  notification_id: string;
  event_type: string;
  title: string;
  body: string;
  read_at: string | null;
  created_at: string;
  post_id?: string;
  reply_id?: string;
};
```

具体交互：

1. 使用 `Promise.allSettled()` 并行读取 Memory 与 Community 通知；任一域失败时仍展示另一域结果，并显示局部错误提示；
2. 每条记录补充 `source`，按 `created_at DESC` 稳定排序，React key 使用 `${source}:${notification_id}`；
3. 未读红点使用两个 API 返回的 `unread_count` 之和，而不是只扫描当前加载页；
4. 为避免只标记当前页，Community 和 Memory 都提供各自的 `read-all` endpoint；“全部已读”按 source 并发调用两个域的批量接口，部分失败后重新刷新两域并提示“部分通知标记失败”；
5. `App.tsx` 提升 `communityTargetPostId` 状态：点击 Community 通知时先切换到 Community Tab，再把 `post_id` 传给 `CommunityPage` 打开详情；详情成功打开或用户关闭后清空 target，避免返回社区页时重复弹出。MVP 不引入路由库；Memory 通知保持现有行为。

Vitest 覆盖合并、局部失败、未读红点和已读路由；Playwright 覆盖真实双用户通知流程，不新增测试框架。

### 6.6 公共 DTO 与文案冻结（v1.5 执行级冻结物）

前后端并行开发以本节为契约基线；§8 的 JSON 示例仅为示意，最终实现以本节 DTO 为准。

```ts
type CommunityBoard = {
  board_id: string;
  slug: string;
  name: string;
  description: string;
};

type CommunityAuthor = { display_name: string };

type CommunityPostSummary = {
  post_id: string;
  board: CommunityBoard;
  author: CommunityAuthor;
  title: string;                  // Summary 恒非 null（列表只含 active）
  pinned: boolean;
  solved: boolean;
  reply_count: number;
  like_count: number;
  viewer_liked: boolean;
  created_at: string;
  last_activity_at: string;
};

type CommunityPostDetail = CommunityPostSummary & {
  body: string | null;            // deleted 时为 null
  deleted: boolean;               // 墓碑；title/body 为 null，前端不展示 pinned
  discussion_status: "open" | "closed";
  viewer_is_author: boolean;
  solved_reply_id: string | null;
  deleted_at: string | null;
};

type CommunityReplyView = {
  reply_id: string;
  author: CommunityAuthor;
  body: string | null;            // deleted 时为 null（墓碑占位行）
  deleted: boolean;
  viewer_is_author: boolean;
  solved: boolean;                // solved_reply_id === reply_id
  created_at: string;
};

type CommunityNotification = {
  notification_id: string;
  event_type: "post_replied" | "reply_marked_solved";
  title: string;
  body: string;
  read_at: string | null;
  created_at: string;
  post_id: string | null;
  reply_id: string | null;
};
```

DTO 规则：

- 所有 DTO 不持有内部 `user_id`、email、JWT subject；作者操作只依赖 `viewer_is_author`；
- `hidden` 不通过公共 DTO 暴露，任何读取表现为 `COMMUNITY_NOT_FOUND`；
- 帖子墓碑：`deleted=true`、`title/body=null`，其余字段保留，前端不展示置顶标识；回复墓碑保留占位行（`body=null`、`deleted=true`）以维持讨论结构；
- 帖子墓碑的 `reply_count` 为当前仍保留的 active 回复数（回复删除时已递减，见 §11.1）；
- 通知 DTO 不返回 `actor_user_id`/`recipient_user_id`。

UI 文案冻结（C2 直接引用，执行期不得自行改写）：

| 位置 | 文案 |
|---|---|
| 讨论区空态 | 还没有帖子，来发起第一个讨论吧 |
| 回复列表空态 | 暂无回复，来写下第一条吧 |
| 发起讨论按钮 | 发起讨论 |
| 发布按钮（发帖/回复提交） | 发布 |
| 回复按钮 | 回复 |
| 标记解决 / 取消解决 | 标记为解决 / 取消解决 |
| 删除按钮（帖子/回复） | 删除 |
| 删除确认标题 | 删除这条帖子？ / 删除这条回复？ |
| 删除确认正文 | 删除后正文不再展示，且不可恢复。 |
| 确认 / 取消 | 确认删除 / 取消 |
| 加载更多 / 重试 | 加载更多 / 重试 |
| 帖子墓碑 | 该帖子已被作者删除 |
| 回复墓碑 | 内容已删除 |
| 学习小组/打卡圈占位 | 即将开放 |
| 记忆提示 | 你的发言可能用于更新你的个人学习记忆；记忆仅对你可见，可在个人中心管理。 |

通知标题/正文模板（写入 `community_notifications` 时渲染，截断规则固定）：

| event_type | title | body |
|---|---|---|
| `post_replied` | `{actor_display_name} 回复了你的帖子` | 回复正文截断至 100 个 Unicode 字符，超出加 `…` |
| `reply_marked_solved` | `你的回复被标记为解决` | 帖子标题截断至 100 个 Unicode 字符，超出加 `…` |

通知点击行为：两个 event_type 均打开对应帖子详情；帖子已删除则打开墓碑详情。`post_replied` 通知在回复被删除后保留（点击落入墓碑回复占位）；`reply_marked_solved` 通知在解决状态被取消或切换后保留（历史通知不撤回，见 §8.5）。已渲染文案不随作者改名追溯（已知限制，改名同步为 follow-up）。

## 7. Community 数据模型

### 7.1 `community_boards`

| 字段 | 类型 | 说明 |
|---|---|---|
| `board_id` | uuid PK | 板块 ID |
| `slug` | varchar(64) UNIQUE | 稳定业务标识，如 `linear-algebra` |
| `name` | varchar(80) | 展示名 |
| `description` | varchar(500) | 简介 |
| `sort_order` | integer | 展示顺序 |
| `status` | text | `active/hidden` |
| `created_at` | timestamptz | 创建时间 |

MVP 初始板块由迁移以固定 UUID 幂等写入，禁止在迁移时随机生成；name/description/sort_order 为 v1.5 冻结文案，迁移直接内联，不另建文案表：

| slug | board_id | name | description | sort_order |
|---|---|---|---|---|
| `linear-algebra` | `da38ecb6-6f37-5724-be95-10e496b5f3dd` | 线性代数 | 矩阵、向量空间、特征值与线性变换 | 10 |
| `calculus` | `dcd2a3a5-7e06-5b7e-891f-e065765dcde0` | 微积分 | 极限、导数、积分与级数 | 20 |
| `probability` | `d6559df9-da74-51ca-9526-a77229c19237` | 概率论 | 概率模型、随机变量与统计推断 | 30 |
| `study-methods` | `768737cb-a6a8-527d-a7f1-153bb8841872` | 学习方法 | 学习方法、复习策略与学习习惯交流 | 40 |

这些值由固定 namespace `8f0db4c4-0b5c-4f6d-a2b3-c86ef29a8d4a` 和名称 `community-board:{slug}` 按 UUIDv5 派生。迁移使用 `INSERT ... ON CONFLICT (slug) DO UPDATE` 并固定 `sort_order`；测试夹具复用同一常量定义。前端仍从 boards API 获取 ID，不硬编码 UUID。

### 7.2 `community_posts`

| 字段 | 类型 | 说明 |
|---|---|---|
| `post_id` | uuid PK | 帖子 ID |
| `user_id` | uuid NOT NULL | 作者内部身份，不跨库 FK |
| `author_display_name` | varchar(80) | 创建时从公开资料读取的展示名快照 |
| `board_id` | uuid FK | 所属板块 |
| `title` | varchar(200) | 标题 |
| `body` | text | 纯文本正文 |
| `content_hash` | char(64) | 当前标题/正文版本哈希 |
| `status` | text | `active/hidden/deleted` |
| `discussion_status` | text | `open/closed`；删除原帖后关闭讨论 |
| `eligible_for_memory` | boolean | 默认 true；隐藏/删除后为 false |
| `pinned` | boolean | 是否置顶，MVP 仅预留管理员写入 |
| `solved_reply_id` | uuid NULL | 已解决答案 |
| `solution_generation` | integer | 每次从未解决变为已解决或切换答案时递增，用于通知去重 |
| `reply_count` | integer | 事务内维护的冗余计数 |
| `like_count` | integer | 事务内维护的冗余计数 |
| `created_at` | timestamptz | 创建时间 |
| `updated_at` | timestamptz | 编辑/状态更新时间 |
| `last_activity_at` | timestamptz | 帖子或回复最后活动时间 |
| `deleted_at` | timestamptz NULL | 软删除时间 |

约束和索引：

- `reply_count >= 0`、`like_count >= 0`、`solution_generation >= 0`；
- `(status, pinned, last_activity_at DESC, post_id DESC)` 列表索引；
- `(user_id, created_at DESC)` 用户内容索引；
- `status=active` 且 `discussion_status=open` 的帖子才允许新回复；`deleted` 帖子只返回墓碑详情，`hidden` 帖子不出现在公共列表；
- 删除后不物理清除正文，直到未来的合规清理任务明确处理；
- MVP 不支持编辑，避免同一 source_ref 多版本的产品歧义；后续编辑必须增加 `content_version` 和版本删除策略。

`content_hash` 输入格式冻结（v1.5）：帖子 = `sha256("标题：{title}\n正文：{body}")`，即 §10.4 Reader 返回给 Memory 的 `SourceItem.content` 同一字符串；回复 = `sha256(body)`。`source_version = content_hash`，Reader 归属校验与 deletion 均依赖这一等式。

### 7.3 `community_replies`

| 字段 | 类型 | 说明 |
|---|---|---|
| `reply_id` | uuid PK | 回复 ID |
| `post_id` | uuid FK | 所属帖子 |
| `user_id` | uuid NOT NULL | 作者内部身份 |
| `author_display_name` | varchar(80) | 创建时的展示名快照 |
| `body` | text | 纯文本正文 |
| `content_hash` | char(64) | 正文哈希 |
| `status` | text | `active/hidden/deleted` |
| `eligible_for_memory` | boolean | 默认 true；隐藏/删除后为 false |
| `created_at` | timestamptz | 创建时间 |
| `updated_at` | timestamptz | 更新时间 |
| `deleted_at` | timestamptz NULL | 软删除时间 |

索引：`(post_id, status, created_at ASC, reply_id ASC)` 和 `(user_id, created_at DESC)`。

回复默认按时间正序展示。帖子删除后，帖子不再出现在默认列表且不可回复，但 active 回复仍可在帖子墓碑详情中展示；回复本身删除后只保留“内容已删除”占位。`content_hash` 计算方式同 §7.2 冻结规则（回复为 `sha256(body)`）。

### 7.4 `community_post_likes`

| 字段 | 类型 | 说明 |
|---|---|---|
| `post_id` | uuid | 帖子 ID |
| `user_id` | uuid | 点赞用户 |
| `created_at` | timestamptz | 创建时间 |

主键为 `(post_id, user_id)`，保证同一用户只能点赞一次。取消点赞采用物理删除关系行并在同一事务中递减计数。点赞不是 Memory 证据。

### 7.5 `community_outbox`

Outbox 与业务写入同事务，列集冻结为（v1.5）：

| 字段 | 说明 |
|---|---|
| `event_id` | 稳定 UUID，主键 |
| `event_type` | `community.post_created`、`community.reply_created`、`community.source_deleted` |
| `aggregate_type` | `post` 或 `reply` |
| `aggregate_id` | 聚合 ID |
| `user_id` | 事件所属用户 |
| `payload` | jsonb，schema 见下文 |
| `idempotency_key` | 下游投递幂等键，`UNIQUE`；重复插入 `ON CONFLICT DO NOTHING` |
| `status` | `pending/processing/delivered/retry_wait/dead_letter` |
| `attempt_count` | 重试次数 |
| `next_attempt_at` | retry_wait 的下次尝试时间 |
| `lease_owner/lease_generation/lease_expires_at` | Publisher 租约与代际 |
| `last_error_code` | 最近错误 |
| `delivery_result` | `published/skipped_source_deleted`；MVP 不新增 `skipped` 状态 |
| `created_at/updated_at/delivered_at` | 生命周期字段 |

`payload` JSON schema 按 event_type 冻结（Publisher 据此构造 `ActivityEvidence`，字段必须与 `backend/memory/contracts/evidence.py::ActivityEvidence` 逐字段对齐，含 `window_started_at/window_ended_at` 可空字段）：

```text
community.post_created:
  {"source_ref": "community:post:{post_id}", "source_version": "{content_hash}",
   "activity_type": "forum_post", "activity_ids": ["post:{post_id}"],
   "content_ref": "community:post:{post_id}", "aggregated_count": 1,
   "topic_hints": ["{board_slug}"], "graph_node_hints": []}

community.reply_created:
  同上，替换为 community:reply:{reply_id} / reply:{reply_id}，activity_type=forum_reply，
  topic_hints 取所属帖子的 board_slug

community.source_deleted:
  {"source_ref": "community:post:{id}" | "community:reply:{id}", "source_version": null,
   "source_system": "activity", "event_id": "<稳定 UUID>"}
```

Outbox 的 claim、退避和 dead-letter 语义参考现有 Conversation Outbox（`backend/conversation/persistence/outbox.py`、`backend/conversation/publisher/outbox_publisher.py`），但注意两点差异，不要照搬：

- Conversation Outbox 没有 `payload`/`delivery_result` 列，它用类型化列（thread_id/message_ids 等）承载数据；Community 用 payload jsonb 是独立设计选择；
- Conversation 的 fencing 现状是：claim CAS 携带 `lease_generation`（outbox.py:115-131），写回用 `lease_owner + status='processing'` 双条件（outbox.py:155-217）。Community 沿用同一语义，不发明更强的 fencing。

不得直接复用 Conversation 表或 Repository。

### 7.6 `community_idempotency_requests`

发帖和回复的创建幂等不能只依赖 Outbox，因为 API 事务在 Outbox 写入前就可能被客户端重试。新增幂等表：

| 字段 | 说明 |
|---|---|
| `user_id` | 认证上下文中的用户 |
| `operation` | `create_post/create_reply` |
| `idempotency_key` | 请求头中的键 |
| `payload_hash` | 规范化请求体哈希 |
| `resource_type/resource_id` | 已创建资源 |
| `created_at/expires_at` | 记录保留时间，建议至少 7 天 |

唯一键为 `(user_id, operation, idempotency_key)`。同键同 payload 返回原资源；同键不同 payload 返回 `COMMUNITY_IDEMPOTENCY_CONFLICT`。点赞不需要这张表，因为 `(post_id, user_id)` 唯一约束和幂等 API 已覆盖重试。

### 7.7 `community_notifications`

社区通知不写入 Memory 的 `memory_user_notifications`，避免 Memory 数据库承担公共社交域状态。MVP 支持“有人回复我的帖子”和“我的回复被标记为解决”：

| 字段 | 说明 |
|---|---|
| `notification_id` | uuid PK |
| `recipient_user_id` | 接收人 |
| `actor_user_id` | 触发人 |
| `event_type` | `post_replied/reply_marked_solved` |
| `post_id/reply_id` | 关联资源 |
| `title/body` | 已渲染的短文案 |
| `dedupe_key` | 事件幂等键，UNIQUE |
| `read_at` | 已读时间，可空 |
| `created_at` | 创建时间 |

通知与触发操作在同一 Community DB 事务中写入，去重公式固定为：

```text
post_replied:{post_id}:{reply_id}
reply_marked_solved:{post_id}:{reply_id}:{solution_generation}
```

`reply_id` 已唯一标识回复及其 actor，因此 `post_replied` 不再重复拼接 actor。帖子作者回复自己的帖子不产生 `post_replied`；帖子作者把自己的回复标记为解决时也不向自己发通知。点赞通知后置。公共响应只返回展示名和资源引用，不返回 `actor_user_id`、`recipient_user_id`。

## 8. API 契约

API 前缀统一为：`/api/v1/community`。公共端点均要求登录用户；Source Reader 和账号 purge 使用独立 system principal，不接受普通用户 token。

### 8.1 板块

```http
GET /api/v1/community/boards
```

响应：

```json
{
  "items": [
    {"board_id": "...", "slug": "linear-algebra", "name": "线性代数", "description": "..."}
  ]
}
```

只返回 `status=active` 板块。

### 8.2 帖子列表

```http
GET /api/v1/community/posts?board_id=&sort=latest&cursor=&limit=20
```

参数：`board_id` 可选；`sort` 为 `latest | unanswered`；`limit` 为 1–50，默认 20；`cursor` 是带签名和过期时间的不透明游标。

排序契约：

- 列表只查询 `status=active` 的帖子；
- `latest`：`pinned DESC, last_activity_at DESC, post_id DESC`；
- `unanswered`：过滤 `solved_reply_id IS NULL`，再按最新排序；
- `hidden` 和 `deleted` 不进入公共列表；详情可为 deleted 帖子返回不含正文的墓碑。

Community 不复制 Conversation 只绑定 `(updated_at, thread_id)` 的专用游标实现，而是扩展并复用 Memory 的通用 HMAC cursor helper，继续使用现有 `CURSOR_HMAC_KEY`，不新增 Community 专用签名密钥。现有 helper 强制绑定 `user_id`；本次将 `issue_cursor()/resolve_cursor()/verify_cursor()` 增加向后兼容的可选 principal binding 参数，默认仍绑定用户。当前 helper 位于 `backend/memory/api/dependencies.py:505-545` 与 `backend/memory/contracts/common.py:287-372`，本次同时提取为共享模块 `backend/shared/cursor.py`（D24），原位置保留 re-export，Community 不反向依赖 `backend/memory/api`：

- Memory 私有接口和 Community notifications：`bind_principal=true`；
- Community 帖子公共列表和公开详情内回复分页：`bind_principal=false`，payload 不写 `principal_hash`，验签时也要求该字段不存在；
- route 和 filters 仍强制绑定，因此无 principal 的公共游标不能跨到任何私有接口使用。

游标签名载荷必须绑定：

- route；
- sort；
- board filter 与其他状态过滤器；
- 最后一条记录的完整排序 key；
- expiry。

任何绑定项变化、签名错误或过期都返回统一游标错误，防止游标跨接口、跨筛选条件复用。`hot` 在有真实数据和明确热度指标后再开启。

`unanswered` 排序在分页过程中若帖子被标记解决，后续页可能跳过/重复个别帖子（keyset 分页通病）；MVP 接受该行为（D27），C4 测试不得把它判为 bug。

### 8.3 创建帖子

```http
POST /api/v1/community/posts
Idempotency-Key: <uuid>
Content-Type: application/json
```

```json
{
  "board_id": "...",
  "title": "大家都是怎么建立特征值的直觉的？",
  "body": "我能记住定义，但还不能把它和线性变换联系起来。"
}
```

服务端从 AuthContext 获取 `user_id`，并通过认证服务的最小公开资料端口读取 `username` 后写入作者显示名快照。响应 `201 Created`，返回 `CommunityPostDetail`。

同一 `Idempotency-Key` 重试时：

- 请求体相同：返回第一次创建结果；
- 请求体不同：返回 `COMMUNITY_IDEMPOTENCY_CONFLICT`；
- 不允许客户端通过幂等键指定其他用户归属。

### 8.4 帖子详情和回复

```http
GET /api/v1/community/posts/{post_id}?reply_cursor=&reply_limit=20
```

`reply_limit` 范围为 1–50，默认 20；回复按 `created_at ASC, reply_id ASC` 分页。详情遇到 `hidden` 帖子统一返回 `COMMUNITY_NOT_FOUND`，包括作者本人；MVP 没有产生 hidden 的公共路径。`deleted` 帖子则按墓碑契约返回，并保留其他用户仍为 active 的回复。

响应包含帖子和一页回复；以下 JSON 仅展示核心字段，最终实现以 §6.6 的完整 DTO 为准：

```json
{
  "post": {"post_id": "...", "title": "...", "reply_count": 2},
  "replies": {
    "items": [
      {
        "reply_id": "...",
        "body": "可以先从线性变换的伸缩方向理解。",
        "author": {"display_name": "..."},
        "viewer_is_author": false,
        "created_at": "..."
      }
    ],
    "next_cursor": null,
    "has_more": false
  }
}
```

```http
POST /api/v1/community/posts/{post_id}/replies
Idempotency-Key: <uuid>
```

```json
{"body": "我也遇到过这个问题，建议先画二维例子。"}
```

### 8.5 点赞、解决和删除

```http
POST   /api/v1/community/posts/{post_id}/like
DELETE /api/v1/community/posts/{post_id}/like
POST   /api/v1/community/posts/{post_id}/resolve
DELETE /api/v1/community/posts/{post_id}
DELETE /api/v1/community/posts/{post_id}/replies/{reply_id}
```

`resolve` 请求体为 `{"reply_id": "..."}`；传 `{"reply_id": null}` 表示取消解决。只有帖子作者可以操作，非空 `reply_id` 必须属于该帖子且为 active。

解决状态规则：

- 从未解决变为已解决，或从答案 A 切换到答案 B：`solution_generation += 1`，并按新 generation 写通知；
- 对当前同一 active reply 的幂等重试：不增加 generation，不重复通知；
- 取消解决：只清空 `solved_reply_id`，不产生通知，不删除、撤回或改写历史 `reply_marked_solved` 通知；
- 取消后再次选择同一 reply：generation 增加，可产生新的通知，反映一次新的产品动作；
- 帖子 `discussion_status=closed` 或 `status=deleted` 后，作者也不能再标记解决、切换答案或取消解决，统一返回 `COMMUNITY_POST_CLOSED`；非作者或不可见帖子仍优先返回 `COMMUNITY_NOT_FOUND`，避免泄露对象状态。

点赞和取消点赞采用幂等语义；两者共用 `community.post.like` 限流 bucket（§9.3）。对 `deleted`/`hidden` 帖子的点赞和取消点赞统一返回 `COMMUNITY_NOT_FOUND`。删除仅允许作者本人；管理员能力后置。删除不存在或无权访问统一返回 `COMMUNITY_NOT_FOUND`。同一作者重复删除已删除对象按幂等成功返回，不重复生成 deletion event。

### 8.6 社区通知

```http
GET  /api/v1/community/notifications?unread_only=false&cursor=&limit=20
POST /api/v1/community/notifications/{notification_id}/read
POST /api/v1/community/notifications/read-all
POST /api/v1/memory/notifications/read-all       # 对现有 Memory 通知 API 的对称扩展
```

通知 API 只返回当前认证用户的记录。响应包含当前查询条件下的 `items/next_cursor`，并单独返回当前用户全部 Community 通知的 `unread_count`；游标继续绑定 route、过滤条件、排序键和 expiry。

Memory 列表契约和前端 `MemoryNotificationPage` 已有 `unread_count`，本次不重复增加字段；Memory 域需要新增 persistence `mark_all_read()`、`POST /read-all` endpoint、幂等测试，以及前端 `markAllMemoryNotificationsRead()` client。Community 域提供对称能力。两个 `read-all` 响应统一为：

```json
{"unread_count": 0}
```

两个 endpoint 都只更新当前认证用户的未读记录；重复调用返回 200 和当前计数。前端并行读取并按来源路由写操作，不把社区事件伪装成 Memory 事件。后端扩展归入 Phase C1，前端 client 和通知面板改造归入 Phase C2。

### 8.7 错误码

| 错误码 | HTTP | 语义 |
|---|---:|---|
| `COMMUNITY_NOT_FOUND` | 404 | 帖子、回复或板块不存在/无权访问 |
| `COMMUNITY_BOARD_DISABLED` | 409 | 板块不可发帖 |
| `COMMUNITY_POST_CLOSED` | 409 | 帖子已关闭，不能回复或修改解决状态 |
| `COMMUNITY_CONTENT_INVALID` | 422 | 标题/正文为空、过长或格式非法 |
| `COMMUNITY_IDEMPOTENCY_CONFLICT` | 422 | 幂等键对应不同请求体 |
| `COMMUNITY_CURSOR_INVALID` | 422 | 游标签名、绑定或有效期非法 |
| `COMMUNITY_RATE_LIMITED` | 429 | 超过发帖/回复/点赞频率限制 |

错误响应继续使用项目已有的 `PublicError` 信封和 `trace_id`。Memory 投递状态不是公共错误码；发帖成功后不因异步 Memory pending 返回失败。

### 8.8 内部账号清理 API

Community 提供幂等内部能力：

```http
POST /api/v1/internal/community-accounts/purge
```

该接口仅允许 `actor_type=system` 且具有 `community:account_purge` scope 的独立 principal 调用。统一 orchestrator 必须先把 Auth 用户状态改为非 active，使 Community 的创建前 profile 校验立即阻止新写，再调用 purge：

1. 对该用户的帖子执行普通删除语义：`status=deleted`、`discussion_status=closed`、`eligible_for_memory=false`，不级联删除其他用户回复；
2. 对该用户的回复执行普通删除语义：`status=deleted`、`eligible_for_memory=false`，维护 `reply_count`，必要时清除 `solved_reply_id`；purge 不使用 `hidden`；
3. 为每个属于该用户的 activity source 写稳定 deletion Outbox；
4. 返回可重试的 purge operation/result，不因重复调用产生重复 deletion fact。

MVP 不新增面向用户的 Auth 删除账号入口，也不假设 Auth 已经发布 lifecycle event。若某个发布范围包含删除账号入口，则“Auth 先禁写 → Memory purge + Community purge → 聚合结果/补偿”的统一 account deletion orchestrator 是发布阻塞条件。

## 9. 认证、隐私和权限

### 9.1 `user_id` 约束

以下字段不能由浏览器提交：

- 帖子作者 `user_id`；
- 回复作者 `user_id`；
- 点赞用户 `user_id`；
- Memory evidence 的 `user_id`；
- Outbox 的投递身份和 actor 类型。

写请求只携带业务数据，服务端按认证上下文落库。公共响应不返回内部 `user_id`，而是返回显示名和 `viewer_is_author`；内部 Repository、Outbox、Memory 证据仍使用认证上下文中的 `user_id`。不返回 email、JWT subject、issuer 或 refresh token 信息。

### 9.2 作者展示名

MVP 使用同进程 AuthRuntime 的最小只读端口，不新增 Auth HTTP profile API。新增 `PublicUserProfileReader` adapter：

- 内部使用 `AuthRuntime.session_factory` 和现有 `get_user_by_id()`；
- 只投影 `user_id/username/status`，禁止返回 email、password hash、refresh token 等字段；
- Community Repository 不接触 Auth session factory；
- 创建帖子/回复前要求用户 `status=active`；
- profile 暂时不可用时创建请求失败并可安全重试，不把 UUID 当昵称。

帖子/回复保存 `author_display_name=username` 快照，列表只读 Community DB。改名同步和服务拆分后的 Auth 内部 profile API 均为后续能力。

### 9.3 基础限流

MVP 复用并向后兼容地扩展现有 `app.state.rate_limiter` / `FixedWindowRateLimiter`（类位于 `backend/memory/api/dependencies.py:281`，`auth_service/ratelimit.py` 只有 `client_ip()` 和阈值常量）。当前实现把窗口写死为 `time.time() // 60`，无法表达小时限制；本次给 `_current()`、`hit()`、`is_limited()` 增加 `window_seconds: int = 60` 参数（默认值保持所有现有 Memory/Auth/Conversation 调用语义不变，新增参数不得破坏现有位置参数调用点，包括 `auth_service/api.py:198/240/308` 与 `conversation/api/__init__.py:20` 的引用），并同步扩展 `clear()` 与过期清理逻辑。该类和 `client_ip()` 的可信代理算法同时提取为共享模块 `backend/shared/ratelimit.py`、`backend/shared/client_ip.py`（D24），原位置保留 re-export。

窗口语义明确为按 Unix 时间戳对齐墙钟的固定窗口：`window_id = floor(time.time() / window_seconds)`，不是滑动窗口、漏桶或令牌桶。因此边界附近允许短时间内先用完上一窗口额度、再使用下一窗口额度，例如 10:59 和 11:00 各发 10 条。MVP 接受该突发特性；测试和运维说明不得把它描述为“任意连续一小时最多 10 条”。需要更平滑的限制时再迁移到 Redis 滑动窗口或网关策略。

计数器 key 必须包含 `(bucket, principal, window_seconds)`，避免同名 bucket 的分钟/小时计数碰撞；过期清理按每个 counter 自己的窗口编号判断，不能继续用单一 60 秒窗口推断。

Community 分别按 `user_id` 和 IP 建 bucket，任一 bucket 命中即拒绝。IP 不直接使用未经校验的 `X-Forwarded-For`，也不能无条件把 `request.client.host` 当最终客户端：

- 将现有 `backend/auth_service/ratelimit.py::client_ip()` 的可信代理算法提取为共享 resolver，Auth 保持兼容，Community 使用同一实现；
- Community 通过 `COMMUNITY_TRUSTED_PROXY_CIDRS` 配置最后一跳负载均衡器/反向代理的 CIDR；默认空表示不信任任何转发头，只使用直连对端；
- 只有直连对端命中可信代理 CIDR 时才读取 `X-Forwarded-For`，从右向左剥离可信代理并选择第一个不可信地址；非法链路 fail closed 回退直连对端；
- 生产 Uvicorn 必须关闭通用 proxy-header 重写，或保证其信任范围不宽于应用配置；推荐显式使用 `--no-proxy-headers`，由共享 resolver 作为唯一 IP 信任边界，避免 `request.client.host` 在进入应用前已被伪造转发头改写；
- `request.client` 缺失时跳过 IP bucket、保留 `user_id` bucket 并记录指标，禁止使用 `unknown` 等共享常量作为 IP key，以免形成全局误伤桶；
- Compose 直连场景无需配置代理 CIDR；生产发布必须把实际网关 CIDR 写入环境配置并通过可信/不可信代理测试。

限流规则为：

| bucket | limit | window_seconds |
|---|---|---:|---:|
| `community.post.create` | 10 | 3600 |
| `community.reply.create.minute` | 5 | 60 |
| `community.reply.create.hour` | 60 | 3600 |
| `community.post.like` | 60 | 60 |
| `community.read` | 120 | 60 |

点赞与取消点赞共用 `community.post.like` 单一 bucket（快速来回切换同样受 60/分钟约束），不另设 `unlike` 桶。`community.read` 仅覆盖帖子列表与详情 GET，回复分页与通知读取复用同一桶。

限流命中通过现有异常处理返回 429、`COMMUNITY_RATE_LIMITED` 和 `Retry-After`；`Retry-After` 按当前窗口剩余秒数计算，而不是固定 60。该实现仍是单进程/单实例 best-effort，不宣称多副本全局精确；生产水平扩容前必须迁移到 Redis 或 API 网关级共享限流。

### 9.4 内容安全

MVP 只做确定性输入安全校验：

- 标题/正文去除首尾空白后不能为空；
- 校验字符长度、最终 SourceItem UTF-8 字节数和请求 payload 大小；
- 拒绝不允许的 Unicode 控制字符；
- 不接受 HTML，不解析 Markdown，不渲染原始链接为富文本；
- 前端按文本节点和安全换行展示。

MVP 不建立关键词库，不调用模型审核，不自动把 `active` 转成 `hidden`，也不创建 `community_reports` 表、举报按钮或审核 API。`hidden` 仅作为未来管理员/合规处置的内部状态预留，当前没有自动触发规则。任何公共列表、帖子详情或回复读取遇到 hidden 均返回/表现为 `COMMUNITY_NOT_FOUND`，作者本人也不能通过公共 API 查看；内部 ActivityReader 同样拒绝读取 hidden 来源。举报、管理员审核、隐藏后的 source deletion 编排作为独立 follow-up 设计和实现。

## 10. Memory 集成设计

### 10.1 进入 Memory 的内容范围

产品语义固定为：发帖/回复表单不增加“记住这条”开关；用户自己编写且 `status=active`、`eligible_for_memory=true` 的帖子和回复默认产生候选 evidence，由 Memory Graph 判断是否形成长期记忆。

不直接作为该用户事实写入：其他用户内容、点赞/浏览、教材引用、系统通知、排序分数和审核标签。前端提示：“你的发言可能用于更新你的个人学习记忆；记忆仅对你可见，可在个人中心管理。”

部署语义与产品语义分离：`COMMUNITY_MEMORY_SUBMIT_ENABLED` 初始默认 `false`，用于灰度控制 evidence 下游提交。关闭时 Publisher 不 claim evidence 事件，或 claim 后保持可恢复 pending，绝不能将其标记为 delivered/skipped。source deletion 使用独立 `COMMUNITY_SOURCE_DELETION_ENABLED`，不能因 evidence 投递关闭而丢失删除事实。

### 10.2 证据与幂等键

帖子创建事件：

```json
{
  "kind": "activity_evidence",
  "activity_type": "forum_post",
  "activity_ids": ["post:<post_id>"],
  "content_ref": "community:post:<post_id>",
  "aggregated_count": 1,
  "topic_hints": ["linear-algebra"],
  "graph_node_hints": []
}
```

回复创建事件：

```json
{
  "kind": "activity_evidence",
  "activity_type": "forum_reply",
  "activity_ids": ["reply:<reply_id>"],
  "content_ref": "community:reply:<reply_id>",
  "aggregated_count": 1,
  "topic_hints": ["linear-algebra"],
  "graph_node_hints": []
}
```

Memory 投递幂等键固定为：

```text
community-activity:{activity_type}:{activity_id}:v1
```

`activity_id` 不使用标题或正文，避免内容变化造成重复投递。MVP 不支持编辑，因此 `v1` 足够；未来编辑时应改为 `content_version` 参与幂等键。

`topic_hints` 的来源固定为 Community board slug：帖子使用自身 `board.slug`，回复使用所属帖子的 `board.slug`，每个事件 MVP 只传一个 hint，例如 `linear-algebra`。Community 不维护第二套 topic 映射、不从标题抽取 topic；Memory 侧负责把稳定 slug 归一化到其 topic/knowledge-node 体系。

Publisher 处理 evidence 时若所属板块记录缺失或非 active，不调用 Memory，也不进入可自动恢复的 `retry_wait`：当前 claim 从 `processing` 转为 `dead_letter`，分别写 `last_error_code=community_board_missing` 或 `community_board_inactive`，保留事件、trace 和板块标识并立即告警。这类情况视为 Community 数据完整性异常，需要人工修复板块/引用并显式 requeue，不能静默跳过或无限重试。

### 10.3 Agent 权限

浏览器不能调用 Memory evidence 接口。内嵌 `ActivityPublisher` 使用受信任进程中的 `issue_agent_token()` 为每个事件签发短期 delegated token：

- `actor_type=activity_agent`；
- `delegated_sub=<事件所属 user_id>`；
- scope 至少为 `memory:submit_evidence`；
- token 不返回浏览器、不落 Community DB、不写日志。

Reader 与 deletion 使用不同的预签发 system token：

- `COMMUNITY_READER_SERVICE_TOKEN`：`actor_type=system`、scope=`community:source_read`；
- `COMMUNITY_SOURCE_DELETE_SERVICE_TOKEN`：`actor_type=system`、scope=`memory:source_delete`；
- `COMMUNITY_ACCOUNT_PURGE_SERVICE_TOKEN`：`actor_type=system`、scope=`community:account_purge`。

三个 token 不复用；`community:source_read` 加入 `ALL_SCOPES`，但不加入 `AGENT_ALLOWED_SCOPES`。独立 Publisher 进程必须等 token broker/workload identity 落地后再拆分。

服务 token 的签发与轮换（v1.5 补充）：verifier 对所有生产 JWT 强制 `exp - iat ≤ auth_token_max_lifetime_seconds`（默认 300 秒），静态长寿命 token 会被直接拒绝，因此"预签发 token 注入 secret store"必须以短时轮换方式实施。PR-D 附带 `backend/auth_service/service_tokens.py` 签发工具：使用 `AUTH_PRIVATE_KEY_FILE` 为指定 system principal + scope 签发短时 token，输出供部署注入；轮换周期与运维操作说明随 PR-D 一并交付。Conversation 存量 `CONVERSATION_*_SERVICE_TOKEN` 的签发缺口一并记录为 follow-up（§18.2）。

### 10.4 ActivityReader

新增 `HttpActivityReader`，结构对齐 `backend/integrations/conversation_reader.py`：

```python
async def read(
    *,
    user_id: UUID,
    activity_type: str,
    activity_ids: list[str],
    content_ref: str | None,
) -> SourceBundle: ...
```

Memory 侧请求 Community 内部接口：

```http
POST /api/v1/internal/community-sources/read
```

请求体包含 `user_id`、`activity_type`、`activity_ids`、`content_ref`。虽然请求体有 `user_id`，Community 服务必须把它当作“待读取目标”，执行完整归属校验，不能仅凭 system principal 放行所有用户数据。

Reader 通过 `Authorization: Bearer <COMMUNITY_READER_SERVICE_TOKEN>` 携带预签发 JWT，并复用现有认证解码与 scope 校验。接口只允许独立 `system` principal 和 `community:source_read` scope；普通用户、`activity_agent` 和浏览器均不可调用。该 scope 不加入 `AGENT_ALLOWED_SCOPES`。token 由部署 secret store 注入，不写入数据库、日志或前端配置。

Reader 返回的 `SourceItem` 约定：

| 行为 | `source_ref` | `role` | 内容规则 |
|---|---|---|---|
| 帖子 | `community:post:{post_id}` | `activity` | `content` 使用 `标题：...\n正文：...`，仅返回作者自己的内容 |
| 回复 | `community:reply:{reply_id}` | `activity` | `content` 放回复正文，不拼入其他用户的原帖正文 |

Memory 当前 `_source_payload()` 不把 `SourceItem.metadata` 发送给总结模型，因此帖子标题不能只放在 metadata 中。现有契约限制为单 item 20,000 个字符、整个 bundle 80,000 UTF-8 bytes、metadata 4,096 bytes/50 keys。Community 每次只返回一个 item，但仍必须在创建时和 Reader 返回前校验最终组合内容；不能只依赖 `COMMUNITY_*_MAX_LENGTH`。metadata 建议包含：`source_version=content_hash`、`post_id/reply_id`、`board_slug`、`author_user_id`、`sequence`，用于归属、版本和排障；不把其他用户的原帖标题或正文拼进回复的 SourceItem。

Reader 必须校验：

1. `activity_type` 与 activity ID 前缀和数据库记录一致；
2. 记录的 `user_id` 等于请求目标 `user_id`；
3. 状态为 active；
4. `content_ref` 与稳定 `source_ref` 一致；
5. SourceBundle 字节数、单 item 长度和 metadata 限制符合 Memory 既有契约；
6. 删除事实通过 `DeletionAwareActivityReader` 过滤。

### 10.5 Memory 状态与用户体验

社区接口不等待 Memory 总结。内部派生状态 `memory_delivery_status` 由 `community_outbox.status + delivery_result` 计算，可取：

```text
pending / processing / delivered / dead_letter / skipped_source_deleted
```

它只用于指标、结构化日志、管理查询和后续个人中心解释，不作为公共错误码，也不要求落在帖子表。MVP 不在帖子详情展示 Memory operation ID；Memory 永久失败进入 dead-letter 并支持受控重放，但不把已成功发布的社区内容改成失败。

## 11. 删除与一致性

### 11.1 社区删除事务

删除帖子时：

1. 锁定帖子，确认作者归属和当前状态；
2. 将帖子标记为 `deleted`，设置 `discussion_status=closed`、`eligible_for_memory=false`；
3. 不级联删除其他用户的 active 回复；回复继续保留在墓碑详情中，但不能新增回复；
4. 仅为帖子来源生成稳定的 source deletion Outbox，Outbox 的 `user_id` 必须是帖子原作者；
5. 提交事务后，公共列表不再返回帖子，详情只返回墓碑信息和仍保留的回复。

删除回复时只处理该回复：标记为 `deleted`、设置 `eligible_for_memory=false`、递减 active `reply_count`；如果它是 `solved_reply_id`，同时清除解决标记，并为该回复原作者生成 source deletion Outbox。同一作者重复删除已经 `deleted` 的对象按幂等成功返回；其他用户查询或删除该对象仍返回 `COMMUNITY_NOT_FOUND`。

`reply_count` 恒为"当前仍保留的 active 回复数"（回复删除即递减），帖子墓碑详情同样展示该当前值，不回滚为删除前快照（D26）。

“删除整个线程”不是 MVP 行为。若未来需要该能力，必须由批处理任务逐条隐藏回复，并按每条回复作者生成 deletion event，不能在请求中用帖子作者身份批量删除。

### 11.2 Memory source deletion

现有 `MemoryClient.submit_source_deletion()` 默认写入 `source_system="conversation"`，需要抽象成支持：

```python
source_system: Literal["conversation", "activity"]
```

Conversation 调用继续传 `conversation`；Community `ActivityPublisher` 传 `activity`。不能复制一份语义不一致的删除接口，也不能让社区删除误写到 Conversation 来源表。

社区 deletion 事件应使用：

- `source_system=activity`；
- `source_ref=community:post:{id}` 或 `community:reply:{id}`；
- `source_version=null`，表示该 source_ref 的所有版本都不可再读；
- MVP 不增加 `deletion_generation` 字段；无编辑、无恢复时每个 source 只有一次逻辑删除，generation 固定视为 `1`；
- `idempotency_key=community-source-deleted:{user_id}:{source_ref}`，稳定 `event_id` 由同一元组派生：`UUIDv5(namespace=COMMUNITY_UUID_NAMESPACE, name="community-source-deleted:{user_id}:{source_ref}")`。`COMMUNITY_UUID_NAMESPACE = UUID("8f0db4c4-0b5c-4f6d-a2b3-c86ef29a8d4a")` 为 Community 域统一 namespace 常量，与 §7.1 板块 seed 共用（板块名称为 `community-board:{slug}`，名称空间不同故无碰撞风险）；`backend/community/contracts/domain.py` 定义该常量，测试夹具与迁移复用。

重复删除和 account purge 重放复用同一键，不产生第二条 deletion fact。未来支持编辑、恢复或多次删除时，再把显式 `deletion_generation` 加入数据模型和幂等键。这样可兼容现有 `source_deletions` 的“`source_version IS NULL` 匹配全部版本”语义。

### 11.3 删除竞态

ActivityPublisher 投递 evidence 前必须重新读取 Community 记录：

- 记录不存在或非 active：标记该 evidence Outbox 为 `delivered`，并写 `delivery_result=skipped_source_deleted`，不再提交 Memory；
- 记录仍 active：提交 evidence；
- evidence 已提交后才删除：继续投递 source deletion；
- source deletion 重试：使用相同幂等键，不重复产生删除事实。

“skipped”只代表该投递因源已删除而无需继续，不代表 Memory 已重新计算受影响的历史总结。现有 Memory 设计第一阶段也只是记录 deletion fact；因此删除社区内容后，已经形成的私有总结记忆可能仍保留，前端不能承诺立即撤回，用户可在个人中心手动删除。跨证据总结重算应作为后续增强。

### 11.4 账号禁用和删除

Auth 当前没有面向用户的删除账号 endpoint 或 lifecycle event，因此 C1–C3 不实现完整跨域账号删除编排，也不能宣称产品已经具备完整联动。

MVP 边界：

- `disabled` 用户不能新发帖/回复，既有公共内容按当前产品策略保留；禁用不等于删除；
- Community 实现 §8.8 的内部幂等 purge 能力及测试，作为数据合规基础；
- Auth 真正推出删除账号功能前，统一 account deletion orchestrator 必须同时调用 Memory account purge 与 Community purge；
- 若 MVP 上线范围包含删除账号入口，则 Community purge 与跨域编排自动升级为发布阻塞项；
- purge 不通过跨库级联或同步扫描 Auth DB 实现。

完整 Auth lifecycle event、operation 状态聚合和失败补偿属于账号删除专项，不混入 Community C1–C3。

## 12. Outbox Publisher 设计

### 12.1 运行形态与事件处理

`ActivityPublisher` 是现有 FastAPI 进程的 lifespan background task，不新增独立容器。每轮最多 claim `COMMUNITY_OUTBOX_BATCH_SIZE` 条，默认 50：

1. 根据事件类型和 feature flag claim `pending/retry_wait` 事件并设置 lease；
2. evidence flag 关闭时不 claim `post_created/reply_created`，使其保持 pending；deletion 由独立 flag 控制；
3. 对 evidence 重新读取当前 Community 状态，非 active 时写 `status=delivered`、`delivery_result=skipped_source_deleted`；
4. 校验所属 board：缺失或非 active 时不调用 Memory，直接写 `status=dead_letter`、稳定 `last_error_code` 并告警；
5. 在同一受信进程为事件用户生成短期 `activity_agent` token 并调用 `MemoryClient.submit_activity_evidence()`；
6. deletion 使用独立 system token 和 `source_system=activity`；
7. 根据 HTTP 结果写 delivered、retry_wait 或 dead_letter；
8. 更新状态时沿用 Conversation 的 fencing 语义（§7.5）：claim CAS 携带 `lease_generation`，写回以 `lease_owner + status='processing'` 双条件防跨租约写入。

多 API 副本可同时运行 Publisher，但同一事件只能由有效 lease owner 完成写回。应用 shutdown 时停止 claim、等待当前批次到超时上限并释放/自然过期 lease。

### 12.2 重试分类

- 5xx、网络超时、连接错误：指数退避，最大退避参考 Conversation publisher 的 1,800 秒；
- 408、425、429：可重试；
- 401/403、schema 422、来源归属失败、board 缺失/非 active：永久失败并告警；
- Memory 返回 `needs_review`：业务成功，不重试；
- 达到最大尝试次数：dead-letter，保留错误码和最后一次 trace；
- feature flag 关闭：不是失败、不是 delivered，不消耗 attempt_count。

### 12.3 可观测性

至少增加：

- `community_outbox_pending_total{event_type}`；
- `community_outbox_oldest_age_seconds{event_type}`；
- `community_activity_publish_total{activity_type,status}`；
- `community_activity_publish_latency_seconds`；
- `community_memory_source_read_total{result}`；
- `community_source_deletion_lag_seconds`；
- `community_post_created_total{board}`、`community_reply_created_total{board}`；
- `community_api_requests_total{route,status}`。

日志包含 `trace_id`、`event_id`、哈希化用户标识、`activity_id`、`memory_operation_id`（若有）和错误码；不记录 token、Authorization header 或不必要的完整正文。

### 12.4 保留与清理策略

- `community_idempotency_requests`：保留 7 天，过期后按 `COMMUNITY_CLEANUP_BATCH_SIZE` 分批删除；
- delivered 或 `skipped_source_deleted` Outbox：保留 30 天；
- dead-letter Outbox：保留 90 天，删除前要求指标/告警系统已留存必要摘要；
- pending、processing、retry_wait 事件不得按年龄自动删除；过期 lease 只能重新 claim；
- Community notifications 默认保留 90 天，与 Memory 通知清理周期一致。

清理由 lifespan 中的低频 maintenance task 执行，使用小批量和数据库 lease，不能阻塞 Publisher 主循环。所有保留天数和 batch size 均可配置。

## 13. 路由和配置接入

### 13.1 FastAPI

在 `backend/app.py` 中新增 Community runtime、公共/内部路由和 lifespan `ActivityPublisher`：

- 普通路由：`/api/v1/community/...`；
- 内部 Reader：`/api/v1/internal/community-sources/read`；
- 内部账号 purge：`/api/v1/internal/community-accounts/purge`；
- 复用 PublicError、trace middleware、CORS、认证依赖和 `app.state.rate_limiter`；
- readiness 增加 Community 检查（D25）：镜像现有 Conversation 检查（app.py:592-619）——未配置 `COMMUNITY_DATABASE_URL` 时不挂载 Community 路由（含写路径），readiness 不报错；已配置但 ping 失败或 `community_alembic_version` 不等于 head 时，报 `community_database_unavailable` / `community_migration_version_mismatch`，fail-closed。

  > 执行期扩展（2026-08-14 残余修复，超出 §13.1 冻结清单的合理补充）：链路依赖校验错误串
  > `community_reader_not_configured` / `community_reader_token_missing`（submit 链路）、
  > `community_source_delete_token_missing` / `memory_api_not_configured`（deletion 链路，
  > 与 §13.2 显式 bool 与 token presence 分离语义一致）；`community_migration_head_unresolved`
  > 同 conversation 先例。错误串仅用于 readiness 诊断，不影响公共 API 契约。

Memory 的两个 runtime 装配点 `backend/app.py` 与 `backend/memory/worker/main.py` 都注册 `HttpActivityReader`，替换 `_UnavailableActivityReader`；这里的 `backend/memory/worker/main.py` 是现有 Memory Worker，不代表新增 Community Worker。配置不完整时明确记录 disabled/fail-closed 状态。

### 13.2 Settings

新增：

```text
COMMUNITY_DATABASE_URL
COMMUNITY_READER_BASE_URL
COMMUNITY_READER_SERVICE_TOKEN
COMMUNITY_SOURCE_DELETE_SERVICE_TOKEN
COMMUNITY_ACCOUNT_PURGE_SERVICE_TOKEN
COMMUNITY_PUBLISHER_ENABLED=false
COMMUNITY_MEMORY_SUBMIT_ENABLED=false
COMMUNITY_SOURCE_DELETION_ENABLED=false
COMMUNITY_OUTBOX_POLL_SECONDS
COMMUNITY_OUTBOX_LEASE_SECONDS
COMMUNITY_OUTBOX_MAX_ATTEMPTS
COMMUNITY_OUTBOX_BATCH_SIZE=50
COMMUNITY_IDEMPOTENCY_RETENTION_DAYS=7
COMMUNITY_OUTBOX_DELIVERED_RETENTION_DAYS=30
COMMUNITY_OUTBOX_DEAD_LETTER_RETENTION_DAYS=90
COMMUNITY_NOTIFICATION_RETENTION_DAYS=90
COMMUNITY_CLEANUP_BATCH_SIZE=500
COMMUNITY_POST_BODY_MAX_LENGTH=19500
COMMUNITY_REPLY_MAX_LENGTH=19500
COMMUNITY_RATE_LIMIT_POST_PER_HOUR=10
COMMUNITY_RATE_LIMIT_REPLY_PER_MINUTE=5
COMMUNITY_RATE_LIMIT_REPLY_PER_HOUR=60
COMMUNITY_RATE_LIMIT_LIKE_PER_MINUTE=60
COMMUNITY_RATE_LIMIT_READ_PER_MINUTE=120
COMMUNITY_TRUSTED_PROXY_CIDRS=[]
```

`COMMUNITY_TRUSTED_PROXY_CIDRS` 是 CIDR 列表，只填写应用直连的受控代理/LB 网段，不填写普通客户端、CDN 出口全集或 `0.0.0.0/0`。

显式 bool 与 token presence 分离：存在 token 不代表已批准启用链路。生产 fail closed：`COMMUNITY_DATABASE_URL` 缺失时不挂载 Community 路由（含写路径与内部 Reader/purge）、readiness 不报错、进程不启动失败（D25），便于本地开发环境无社区库时仍可运行其余域；Reader/deletion token、base URL、scope 任一缺失时对应链路保持 disabled 并告警。灰度顺序先启 Publisher，再启 Reader/deletion，最后启 evidence submit。

Compose 中 Community API 内嵌在 `memory-api` 服务，因此 `memory-api` 自身和独立 `memory-worker` 都配置 `COMMUNITY_READER_BASE_URL=http://memory-api:8000`；非 Compose 本地运行按实际监听地址配置。两个 Memory runtime 必须使用同一内部 URL 和 reader token，不能让 worker 留在 `_UnavailableActivityReader`。

### 13.3 Scope

现有 scope 增加：

```text
community:source_read
community:account_purge
```

二者都加入 `ALL_SCOPES`，只授予对应内部 system principal，不加入 `AGENT_ALLOWED_SCOPES`，也不授予普通用户。Community 公共读写继续使用普通 `user` actor 的认证，而不是增加新的浏览器 actor。

## 14. 数据迁移、合并和部署顺序

### 14.1 合并与 PR 粒度

本节的“发布”编号表示环境 rollout 顺序，不等于机械地每一步一个 PR，也不能把全部功能塞进一个不可审阅的大 PR。默认采用 5 个可堆叠、可独立测试和回滚的 PR：

1. **PR-A · Foundation**：Community role/database、迁移与 seed、Repository 骨架、测试库 provision（`scripts/ci-local.sh` 增加 `ensure_test_database community_test community`、CI 与 `.env.example` 同步）、共享可信代理 IP resolver、可配置窗口 limiter 与公共 cursor helper 的共享化提取（`backend/shared/`，原位置保留 re-export，D24）、readiness 的 Community 检查（D25）；对应部署步骤 1–2。
2. **PR-B · Read-only vertical slice**：boards/posts/detail 只读 API、公开资料 adapter、只读前端列表/详情和相关测试；对应部署步骤 3。
3. **PR-C · Write vertical slice**：发帖、回复、点赞、解决/取消、删除、通知、purge、幂等、Memory read-all 扩展及前端交互；对应部署步骤 4。
4. **PR-D · Memory dark launch**：Community Source Reader、两个 Memory runtime 装配、lifespan Publisher、scope/token、服务 token 签发工具与轮换说明（§10.3）、source deletion 参数和可观测性；合并时所有下游提交 flag 保持关闭，对应部署步骤 5–6。
5. **PR-E · Rollout and cleanup**：灰度配置、删除先行验证、端到端/评测结果、清理已不再引用的 Mock 和假交互；对应部署步骤 7–8。该 PR 不应再引入新的核心领域模型。

每个 PR 只包含完成该纵切所需的跨层修改，必须通过对应单元、集成和前端测试；不得借机重构无关模块。feature flag 用于环境启用和回滚，不替代代码评审边界。因 CI 或评审容量需要可继续拆小某个 PR，但未经重新冻结设计，不得把 PR-A 至 PR-D 合并为一个全量实现 PR。

### 14.2 环境发布顺序

1. 扩展 PostgreSQL initdb、`scripts/postgres_roles_upgrade.sh`、compose 和环境示例，创建独立 `community` role/database；修正文案为“1 个管理员 + 4 个应用账号”。
2. 增加独立迁移链，创建 boards、posts、replies、likes、Outbox、幂等请求、通知和索引；以 §7.1 固定 UUID 幂等 seed 板块。
3. 发布只读 API、公开资料 adapter 和前端列表，验证真实数据读取。
4. 发布发帖/回复/点赞/解决/删除事务、通知和 Community purge API。
5. 启用内嵌 lifespan `ActivityPublisher`，但保持 `COMMUNITY_MEMORY_SUBMIT_ENABLED=false`，观察 Outbox 产出且不消费 evidence。
6. 接入 Community 内部 Source Reader 与两个 Memory runtime 的 `HttpActivityReader`，小流量验证读取归属。
7. 先启 `COMMUNITY_SOURCE_DELETION_ENABLED` 并验证删除事实，再灰度启 `COMMUNITY_MEMORY_SUBMIT_ENABLED`。
8. 清理已不再引用的讨论区 Mock 数据/代码和“加入”假按钮；学习小组/打卡圈保留明确未开放状态。

迁移必须可重复执行，不修改现有 Auth、Conversation、Memory 业务表结构；允许扩展 `MemoryClient.submit_source_deletion()` 的 `source_system` 参数和认证 scope。无需回填既有 Memory 数据。独立 Worker/container 不属于本次部署。

## 15. 测试计划

### 15.1 Community 后端单元测试

- 帖子/回复 DTO 的长度、空白、非法控制字符校验；
- Community 集成测试参照 `tests/conversation/conftest.py`：使用独立 `community_test(_\w+)?` 数据库、独立迁移 fixture、每测试 TRUNCATE Community 表，并拒绝连接任何非测试库；
- 所有 Repository 查询都带用户归属或状态条件；
- 游标编码、解码、排序稳定性；
- 相同幂等键返回相同结果，不同 payload 返回冲突，幂等记录与业务资源同事务提交；
- 社区通知收件人正确、`dedupe_key` 去重、已读操作幂等且不能跨用户读取；
- 唯一点赞、重复点赞、取消点赞计数不漂移；
- 标记解决只能作用于当前帖子回复；
- 作者只能删除自己的内容；
- 删除帖子关闭讨论、保留其他用户回复，并为帖子来源产生 deletion Outbox；删除回复只为该回复来源产生 deletion Outbox；
- 删除与 evidence 投递竞态；
- `source_ref` 和 `source_version` 生成稳定；
- `community_outbox` 状态机本身：claim/过期 lease 回收/写回 fencing（owner+status 双条件）/指数退避分类/永久失败转 dead-letter/feature flag 关闭时不消耗 `attempt_count`，对照 `backend/conversation/persistence/outbox.py` 的实现语义。

### 15.2 Memory 集成测试

- `ActivityEvidence(forum_post/forum_reply)` 能路由到 summary；
- activity reader 能返回用户自己的来源；
- 读取其他用户 activity 返回统一 not found/denied；
- SourceBundle 超限被拒绝；
- 删除事实能被 `DeletionAwareActivityReader` 过滤；
- source deletion 使用 `source_system=activity`，不误写 conversation；
- Memory 暂时不可用时，社区发帖仍成功，Outbox 可重试；
- board 缺失或非 active 的 evidence 事件进入 dead-letter、写稳定错误码并触发告警，不调用 Memory；
- Memory 返回 `needs_review` 不会无限重试。

### 15.3 API 和前端测试

后端/API：

- 登录隔离，所有写入归属来自 `AuthContext.user_id`；
- 发帖、回复、点赞、解决/取消解决、删除和 purge 的成功、幂等与权限错误；closed/deleted 帖子的 resolve 和取消 resolve 返回 `COMMUNITY_POST_CLOSED`；
- 60/3600 秒双窗口、墙钟边界突发语义、user/IP 双 bucket、默认 60 秒向后兼容和准确的 `Retry-After`；
- 可信代理 IP 解析覆盖直连、可信/不可信代理、多级 XFF、非法 XFF 和 `request.client` 缺失，验证不会形成网关全局桶或 `unknown` 全局桶；
- HMAC cursor 的 route/filter/sort/expiry 绑定，以及公共游标不绑定 principal、私有通知游标仍绑定 principal；
- system reader token 与 source-delete token 的 scope 隔离；
- feature flag 关闭时 evidence 保持 pending，deletion 不被错误阻断。

前端不新增框架，使用已有 Vitest + Testing Library + MSW 覆盖：

- 列表、空态、错误重试、游标加载和表单输入保留；
- 401 refresh 后幂等重试不重复发帖；
- 墓碑渲染不泄露原正文；
- Community/Memory 通知合并、局部失败、`unread_count` 红点和两个域的 `read-all` 部分失败；
- 学习小组按钮不假装持久化成功。

Playwright 覆盖：注册/登录→发帖→另一用户回复→通知→点赞→解决→取消解决→删除的完整流程，以及双用户权限隔离。

### 15.4 验收指标与评测责任

Phase C4 前将当前 31 条评测样本扩充到 50–100 条。角色责任固定为：Memory/评测 owner 负责样本集、指标脚本和最终签字；Community owner 提供场景和失败案例；至少采用“双人标注”或“一人标注 + 另一人复核”。具体人员姓名由项目负责人在 C4 开始前指定，文档不虚构负责人。

样本建议包含：20–30 条帖子、20–30 条回复、10–20 条不应形成 Memory 的闲聊/引用/他人观点，以及删除、归属攻击和重复投递样本。每条至少标注：

```text
should_create_memory
expected_memory_type
expected_topic
supported_claims
forbidden_claims
privacy_owner_user_id
deletion_expected
review_reason
```

发布门槛至少记录：

- memory precision；
- unsupported-claim rate；
- cross-user leakage rate = 0；
- deletion read suppression = 100%；
- duplicate memory/evidence creation = 0；
- API 成功率、p95 延迟、Outbox 投递/needs-review/dead-letter 率；
- 点赞计数与关系表一致率 = 100%。

阈值由 Memory owner 在样本冻结时写入评测配置；未记录数值结果不得完成 C4。

## 16. 分阶段实施计划

### Phase C0：契约冻结

- 本文档 D1–D28 作为实现基线；
- 固定 Community DB、公开资料 adapter、游标、token、feature flag 和板块 seed；
- 建立举报/审核、独立 Worker、改名同步、账号删除 orchestrator 的 follow-up。

### Phase C1：Community Domain

- 独立数据库、迁移、Repository、错误码和 `community_test` provision；
- 板块、帖子、回复、点赞、解决、软删除；
- Outbox、幂等、通知、公开资料 adapter、可信代理 IP resolver、可配置窗口限流和 purge；
- 共享化 cursor helper（`backend/shared/cursor.py`）并扩展可选 principal binding；
- Memory 域新增通知 `mark_all_read()`、`POST /api/v1/memory/notifications/read-all` 和后端测试；
- 后端单元/集成测试。

### Phase C2：前端真实接入

- `frontend/src/api/community.ts`，并在 `frontend/src/api/memory.ts` 现有 `listNotifications()`/`markNotificationRead()` 基础上增加 `markAllMemoryNotificationsRead()`；
- 列表、筛选、分页、发帖和详情；
- 回复、点赞、解决、取消解决和删除；
- UnifiedNotification 合并（`NotifPanel` 目前没有单条点击处理，Community 通知跳转属于新增交互，见 §6.6）；
- 移除讨论区 Mock，其他两个 Tab 显示未开放（文案"即将开放"）。

### Phase C3：Memory Activity Reader

- Community 内部 Source Reader；
- `HttpActivityReader` 同时装配到 `backend/app.py` 与现有 `backend/memory/worker/main.py`；
- system scope/token、activity source deletion；
- 内嵌 lifespan Publisher、重试和指标；
- 按 deletion→evidence 顺序灰度开启。

### Phase C4：上线前质量

- 完整后端、Vitest、Playwright、并发/竞态测试；
- 权限、内容输入安全和 token 隔离检查；
- 扩充评测样本至 50–100 条并记录数值指标；
- 由 Memory/评测 owner 完成 gate 签字。

## 17. 关键决策记录

### D1：社区不复用 Conversation Thread

**决定**：社区帖子和回复使用独立 Community 模型。

**原因**：公共多人内容与私有 Conversation 的访问控制、生命周期和删除语义不同。

### D2：Memory 只接收作者自己的社区内容

**决定**：ActivityReader 按 `user_id` 严格校验，只返回证据所属用户编写的正文。

**原因**：他人观点不能未经确认写成当前用户的学习事实。

### D3：社区发布与 Memory 总结解耦

**决定**：Community transaction 成功即发布成功，Memory 通过 Outbox 异步处理。

**原因**：Memory 和外部基础设施故障不应阻塞用户交流。

### D4：MVP 不支持编辑

**决定**：第一版只支持删除，不支持修改已发布正文。

**原因**：保持 source_ref/version 稳定，避免旧版本撤回和重复总结。

### D5：学习小组和打卡圈后置

**决定**：先完成讨论区闭环，另外两个 Tab 不做假后端。

**原因**：成员关系、排行榜聚合和通知语义尚未冻结，混入第一批会扩大权限和数据风险。

### D6：独立 Community database

**决定**：使用独立 database、非超级用户账号和迁移链，不采用 Conversation schema 临时方案。

**原因**：社区是公共高频读写域；独立数据库能保持账号最小权限和迁移边界，也避免临时 schema 方案长期固化。

### D7：产品默认产生候选证据，部署默认关闭投递

**决定**：用户无“记住这条”开关；有效作者内容默认进入 Outbox。`COMMUNITY_MEMORY_SUBMIT_ENABLED=false` 作为初始灰度值，关闭时 evidence 保持 pending；deletion 独立控制。

**原因**：产品体验保持一致，同时让运维可以在 Reader、指标和删除链路验证完成前安全灰度。

### D8：MVP 使用 lifespan ActivityPublisher

**决定**：Publisher 运行在现有受信 FastAPI 进程，不新增 Community Worker 容器。独立进程需先具备 token broker/workload identity。

**原因**：现有 `issue_agent_token()` 只允许受信同进程调用；直接拆容器会迫使系统复制签名私钥或长期 token。

### D9：公开资料使用同进程 AuthRuntime adapter

**决定**：`PublicUserProfileReader` 只读 `username/status`，不新增 Auth HTTP profile API，不向 Repository 暴露 Auth session。

**原因**：Auth 当前没有按 `user_id` 的内部 profile endpoint；最小 adapter 改造量小且能限制敏感字段暴露。

### D10：账号删除编排后置，Community purge 能力前置

**决定**：C1–C3 提供内部幂等 purge；Auth 删除入口、lifecycle event 和跨域 orchestrator 属于账号删除专项。若发布包含删除账号入口，则跨域编排成为发布阻塞项。

**原因**：当前 Auth 没有删除入口或 lifecycle event，但 Community 仍需先具备可被未来编排器调用的数据合规能力。

### D11：MVP 不实现举报或自动隐藏

**决定**：不建 `community_reports`，不提供举报 API，不使用关键词/模型自动审核；只做确定性输入安全校验，`hidden` 留作未来内部状态。

**原因**：未定义规则库、审核责任和申诉流程时实现自动隐藏会制造不可解释误伤；应由独立内容治理项目闭环。

### D12：限流器支持可配置窗口

**决定**：扩展共享 `FixedWindowRateLimiter`，增加默认 60 秒的 `window_seconds`，Community 小时级规则传 3,600 秒。

**原因**：当前实现只支持分钟窗口；默认值和现有调用保持兼容比另建一套 Community 限流器更简单。

### D13：公共列表游标不绑定用户 principal

**决定**：通用 cursor helper 增加默认开启的可选 principal binding；帖子列表/详情回复分页关闭绑定，通知等私有游标继续绑定。

**原因**：公共内容的分页位置与查看者无关，但 route/filter/expiry 仍必须防篡改和防跨接口复用。

### D14：Memory read-all 后端归入 C1、前端归入 C2

**决定**：C1 修改 Memory persistence/API 和测试；C2 修改 `frontend/src/api/memory.ts` 及统一通知面板。现有列表 `unread_count` 直接复用。

**原因**：这是统一通知“全部已读”真实语义所需的跨域最小扩展，不能只标记当前加载页。

### D15：Community 删除 generation 在 MVP 固定为 1

**决定**：不新增 `deletion_generation` 列，删除幂等键只绑定 `user_id + source_ref`；编辑/恢复上线时再引入显式 generation。

**原因**：MVP 无编辑、无恢复，同一 source 只有一次逻辑删除，引入可变 generation 没有业务收益。

### D16：purge 只使用 deleted 语义

**决定**：账号 purge 对帖子/回复执行与作者删除相同的 `deleted + eligible_for_memory=false` 语义，帖子同时 closed；不使用含糊的“合规隐藏”。

**原因**：复用同一墓碑、计数和 source deletion 契约，避免 hidden 与删除在合规路径中混用。

### D17：board slug 直接作为 topic hint

**决定**：帖子使用自身 board slug，回复使用所属帖子 board slug；Community 不维护额外映射，Memory 负责归一化。

**原因**：slug 已是稳定业务标识，直接传递可避免 Community 与 Memory 的 topic taxonomy 双写漂移。

### D18：IP 桶只信任显式配置的代理链

**决定**：复用并共享 Auth 已有的从右向左剥离可信代理算法；Community 使用独立 `COMMUNITY_TRUSTED_PROXY_CIDRS`，默认忽略转发头。生产推荐关闭 Uvicorn 的通用 proxy-header 重写，由应用 resolver 作为唯一信任边界。

**原因**：直接使用网关地址会把所有用户合并进全局桶，无条件信任 XFF 又允许攻击者伪造地址绕过限流。

### D19：MVP 限流采用墙钟固定窗口

**决定**：窗口按 `floor(time.time() / window_seconds)` 对齐，不承诺任意连续时间段限制；接受边界处最多两段额度的短时突发。

**原因**：该语义与现有 in-memory limiter 一致，改造成本最低；多副本和更平滑限流统一留给 Redis/网关实现。

### D20：默认按 5 个纵切 PR 实施

**决定**：Foundation、只读纵切、写纵切、Memory dark launch、rollout/cleanup 分成 5 个默认 PR；§14 的 8 步是环境发布顺序，不是单个全量 PR 内的操作清单。

**原因**：这样每个变更都可审阅、测试、部署和回滚，同时保留 feature flag 的灰度价值。

### D21：closed 帖子禁止修改解决状态

**决定**：作者对 closed/deleted 帖子的 resolve、切换答案和取消 resolve 均返回 `COMMUNITY_POST_CLOSED`；非作者仍返回 `COMMUNITY_NOT_FOUND`。

**原因**：删除已经关闭讨论并冻结业务状态，继续修改解决标记会破坏墓碑和通知语义。

### D22：无效板块 evidence 进入 dead-letter

**决定**：Publisher 发现板块缺失或非 active 时不调用 Memory，直接记录稳定错误码、进入 `dead_letter` 并告警，修复后只能显式 requeue。

**原因**：这是数据完整性异常，不是瞬时下游故障；自动重试或静默跳过都会掩盖问题。

### D23：进程内 Publisher 是起点，设拆分退出标准

**决定**：MVP 保持 lifespan 内嵌 Publisher；当 backlog/延迟/claim 竞争三个信号任一持续 24 小时超过 §4.3 阈值时，启动独立 Publisher 容器专项（依赖 token broker/workload identity）。

**原因**：避免"临时同进程方案"长期固化（与 §4.2 反对临时 schema 固化的理由一致），同时不为一期引入拆进程成本。

### D24：共享模块统一提取为 `backend/shared/`

**决定**：cursor helper、`FixedWindowRateLimiter`、可信代理 IP resolver 提取为 `backend/shared/{cursor,ratelimit,client_ip}.py`；原位置保留 re-export，现有 30+ 引用点不改动；Community 只依赖 `backend/shared`，不依赖 `backend/memory/api` 或 `backend/auth_service` 内部模块。

**原因**：Community 若直接 import `memory/api/dependencies.py` 会形成社区域对 Memory API 层的反向耦合；统一共享化比单独为 cursor 开例外更一致。

### D25：Community DB 缺失时不挂载路由、不启动失败

**决定**：`COMMUNITY_DATABASE_URL` 缺失 → 不挂载 Community 路由（含内部 Reader/purge），readiness 不报错，进程正常启动；已配置但不可用/迁移不一致 → readiness fail-closed（`community_database_unavailable` / `community_migration_version_mismatch`）。

**原因**：与 Conversation 挂载语义一致，本地开发不被强杀，生产依赖 readiness 把关。

### D26：`reply_count` 恒为当前 active 回复数

**决定**：回复删除即递减 `reply_count`；帖子墓碑详情展示当前值，不保留删除前快照。

**原因**：避免引入第二个计数口径；墓碑语义是"关闭讨论 + 保留其余回复"，当前值即可满足。

### D27：接受 `unanswered` 分页漂移

**决定**：分页过程中帖子被解决导致后续页可能跳过/重复个别帖子，MVP 接受，不修复。

**原因**：keyset 分页通病，修复需要物化快照或双游标，超出 MVP 收益。

### D28：公共 DTO 与文案冻结于 §6.6

**决定**：§6.6 的 DTO 字段集、UI 文案、通知标题/正文模板为 C2/C1 契约基线；执行期不得自行改写文案，DTO 变更需回到本冻结物评审。

**原因**：此前"以 §6.5 完整 DTO 为准"但 §6.5 未给出完整字段表，前后端并行开发缺少契约锚点；文案不冻结会导致迁移（seed）与前端反复返工。

## 18. 剩余执行项（非架构阻塞）

1. 项目负责人在 Phase C4 开始前指定 Memory/评测 owner、Community 场景提供者和复核人；未指定不阻塞 C1–C3，但阻塞 C4 完成。
2. 建立 follow-up：举报与管理员审核、公开用户名改名同步、独立 Publisher 进程/token broker、统一账号删除 orchestrator、Conversation/Community 服务 token 的统一签发与轮换自动化。
3. C0 将 D1–D28 转成 issue/验收清单；后续实现不得重新引入 Conversation schema、独立 Community Worker 容器或前端 Memory 开关等备选路径。
4. 当前容器构建仍需在 GHCR `ghcr.io/astral-sh/uv:0.9.26` 元数据访问恢复后重试；该网络问题不改变本文设计决策。

## 19. v1.6 增补决策（D29–D47，执行级冻结）

> 本节为 2026-08-14 对执行细节的逐条定案，是对上文各节的补充与修订。与上文冲突时，以本节为准。执行期不得自行改写；如需变更必须回到冻结评审。

### D29：认证依赖共享化（修订 D24 范围）

`get_auth_context` 与 `require()`（含权限矩阵依赖工厂）从 `backend/memory/api/dependencies.py` 提取到 `backend/shared/auth_context.py`，原位置保留 re-export。理由：D24 的动机是禁止 Community → memory/api 反向耦合，认证依赖同样属于该禁令范围；该依赖只碰 `app.state`，与 memory settings 无绑定，提取成本低。Conversation 现有 5 处跨域 import（如 `conversation/api/conversations.py:28`）是存量债，不是新域应复制的模式。

### D30：`last_activity_at` 更新语义

仅发帖创建、回复创建更新 `last_activity_at`。点赞、取消点赞、解决/取消解决、回复删除一律不更新。理由：防止刷排序；删除不是"活动"。

### D31：deleted/hidden 帖子的回复错误码分级

对 deleted/hidden 帖子发回复统一分级：

- `hidden` → `COMMUNITY_NOT_FOUND`（所有人含作者，不泄露状态）；
- `deleted` → `COMMUNITY_POST_CLOSED`（所有人含作者）。

理由：deleted 帖子的 closed 状态已通过墓碑公开，返回 `POST_CLOSED` 不泄露任何信息，且与 D21 语义对齐（同一墓碑上"回复 not found、解决 closed"会产生两套语义）。§8.5 中"对 deleted/hidden 帖子的点赞/取消点赞统一返回 `COMMUNITY_NOT_FOUND`"保持不变；resolve 相关操作仍遵循 D21（作者 → `POST_CLOSED`，非作者/不可见 → `NOT_FOUND`）。

### D32：`community_outbox.idempotency_key` 公式

冻结为 `community:{event_type}:{aggregate_id}`（事件天然唯一，防重复入队）。与 Memory 侧删除幂等键 `community-source-deleted:{user_id}:{source_ref}`（§11.2）是两层独立键，互不替代；purge 重放时由 outbox 层 `ON CONFLICT DO NOTHING` 天然去重。

### D33：通知 `dedupe_key` 冲突处理

通知插入使用 `ON CONFLICT (dedupe_key) DO NOTHING`，与 outbox 插入语义一致，必须在业务事务内执行。

### D34：删除 solved reply 的副作用

清除 `solved_reply_id` 时仅清空，不递增 `solution_generation`、不产生通知。语义等同"取消解决"（§8.5：取消不产生通知）；包括"回复作者删除自己被标记解决的回复"场景，帖子作者也不收通知，保持 §8.5 的"取消无通知"统一规则。

### D35：purge 响应 DTO

`CommunityPurgeResult`：镜像 Memory 的 `MemoryOperationResult` 同构，字段为 `operation_id / status / completed_at / error`。Community 自建命名，不复用 Memory DTO。

### D36：system principal 命名

三个服务 token 的 principal 名（sub claim）冻结为：

| token 环境变量 | principal | scope |
|---|---|---|
| `COMMUNITY_READER_SERVICE_TOKEN` | `system:community-reader` | `community:source_read` |
| `COMMUNITY_SOURCE_DELETE_SERVICE_TOKEN` | `system:community-source-delete` | `memory:source_delete` |
| `COMMUNITY_ACCOUNT_PURGE_SERVICE_TOKEN` | `system:community-purge` | `community:account_purge` |

`service_tokens.py` 的 `--principal` 参数直接使用以上名称。

### D37：内容安全控制字符清单

标题/正文拒绝 U+0000–U+001F 与 U+007F，白名单仅 `\n \t \r`。经核对，Conversation 现有消息校验（`conversation/contracts/api.py:129`，仅 min/max 长度）无现成控制字符校验，Community 自建校验函数并补测试，不扩展现有 Conversation 行为。

### D38：Publisher 与维护任务默认值

`COMMUNITY_OUTBOX_POLL_SECONDS=1.0`、`COMMUNITY_OUTBOX_LEASE_SECONDS=60`、`COMMUNITY_OUTBOX_MAX_ATTEMPTS=10`（与 conversation `.env.example` 一致）；新增 `COMMUNITY_MAINTENANCE_INTERVAL_SECONDS=3600` 作为维护清理任务的运行间隔。

### D39：回复分页游标绑定具体 post_id

回复分页游标的 route 归一化必须代入实际路径参数（`GET /posts/{post_id}` → 具体值），payload 同时写入 `post_id`，防跨帖子复用。

### D40：幂等表 `payload_hash` 实现

复用 `backend/memory/contracts/common.py::canonical_json()`（确定性键排序，D24 共享化后无依赖问题），对规范化（trim 之后）的请求模型 `model_dump(mode="json")` 结果做 sha256。

### D41：`GET /boards` 限流

计入 `community.read` 桶（120/60），避免出现完全不限流的枚举端点。

### D42：Community 指标模块

`backend/community/metrics.py` 使用与 `backend/memory/metrics.py` 相同的 prometheus-client 注册方式（同一 REGISTRY），`/metrics` 出口不动。§12.3 指标名均为 `community_` 前缀，与 `memory_` 无冲突。

### D43：服务 token 签发工具交付形态

`python -m backend.auth_service.service_tokens issue --principal <name> --scope <scope> --lifetime-seconds 300`；token 明文只写 stdout 或 `--out` 文件，issuer/exp 元信息走 stderr；默认 300s，超出 `auth_token_max_lifetime_seconds` 时告警但允许（运维可能同步调上限）。轮换说明放 `docs/` 下新建简短运维文档，不塞进本文档。

### D44：Community DB 账号约定

`community` / `community`（账号与数据库同名，对齐现有 memory/auth/conversation 三账号），initdb 与 roles 升级脚本同口径。

### D45：Page 信封冻结

统一列表信封 `{items, next_cursor: string|null, has_more: bool}`；通知页额外顶层 `unread_count`；`GET /boards` 无分页仅 `{items}`。与 §8.4 草图及 Memory 现有列表命名一致。此信封为公共契约，前后端并行开发以此为准。

### D46：迁移链装配与生产 validator

- 新增 `community_alembic.ini` 与 `community_migrations/env.py`，镜像 conversation：`version_table="community_alembic_version"`，`COMMUNITY_DATABASE_URL` 缺失即 raise；
- `scripts/ci-local.sh` 增加第四条迁移命令（`uv run alembic -c community_alembic.ini upgrade head`）；
- `backend/settings.py` 的 model_validator 镜像 conversation 规则：配置了 Community 且 submit/reader 链路开启时，要求 reader base url 与 token 齐备；`COMMUNITY_DATABASE_URL` 缺失不强制（D25 不挂载语义，由 readiness 兜底）。

### D47：DTO 时间格式

时间字段为 ISO8601 UTC（pydantic datetime 默认序列化，如 `2026-08-14T09:30:00Z`），与现有 memory API 先例一致，前端按字符串展示。

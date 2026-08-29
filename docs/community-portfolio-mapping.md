<!-- 移植映射表（Phase 0 产出物，表头按 community-rebuild-plan.md 附录 D 冻结） -->
<!-- 来源项目：FlaskBB (BSD-3) / bbs-go (GPL-3.0，仅参考不复制代码) -->

# 社区重建移植映射表

> 本文档记录社区重建 MVP（P0–P4）中各实现单元与开源参考来源的对应关系。
> 更新日期：2026-08-28 · 与 community-rebuild-plan.md v3.9 对齐。

## 参考来源固定版本

| 来源项目 | 仓库地址 | tag | commit SHA | 协议 | 使用方式 |
|---|---|---|---|---|---|
| FlaskBB | https://github.com/flaskbb/flaskbb | v2.2.1 | `9f68ee7b5f9e08c0472d3998457d0f5c6870ce29` | BSD-3 | 后端模型与论坛规则参考改写 |
| bbs-go | https://github.com/mlogclub/bbs-go | v4.4.5 | `7343253cbc9703f427e9b994bdafd8d58896b57f` | GPL-3.0 | **仅参考布局与交互骨架，不复制代码** |

## 映射表

| 来源项目 | tag | commit SHA | 来源文件 | 来源函数/模型 | 移植方式 | 目标文件 | 目标函数/组件 | 依赖改写点 | LICENSE/来源注释位置 | owner | review 状态 |
|---|---|---|---|---|---|---|---|---|---|---|---|
| FlaskBB | v2.2.1 | 9f68ee7b | `flaskbb/forum/models.py` | `Forum/Category/Topic/Post` 模型与冗余计数思路 | 参考改写 | `backend/community/persistence/posts.py` 等 | `insert_post` / `mark_post_deleted` / 计数更新 | sync SQLAlchemy → async SQLAlchemy；适配现有表名/列名 | Python 文件头 `#` 注释 + `docs/licenses/FLASKBB_LICENSE` | | 待评审 |
| FlaskBB | v2.2.1 | 9f68ee7b | `flaskbb/forum/models.py` | `Attachment` 附件模型思路 | 参考改写 | `backend/community/persistence/attachments.py` | 附件 CRUD / 状态转换 | sync → async；新增 `status/delete_attempts` 等 MVP 字段 | 同上 | | 待评审 |
| FlaskBB | v2.2.1 | 9f68ee7b | `flaskbb/forum/views.py` | 发帖/回帖/删除基本规则 | 逻辑改写 | `backend/community/services/post_command_service.py` / `reply_service.py` | `create_post` / `delete_post` / `create_reply` / `delete_reply` | 异步化；保留现有幂等/Outbox/通知集成 | 同上 | | 待评审 |
| bbs-go | v4.4.5 | 7343253c | `web/`（React 前端布局与交互） | 首页板块宫格、帖子流、帖子详情、发帖表单布局 | 仅参考设计 | `frontend/src/pages/community/` 等新增组件 | `CommunityHome` / `BoardDetail` / `PostDetail` / `CreatePost` / `BoardApplication` / `AdminApplications` | 状态导航（PageKey）重写；不引入路由；不复制代码 | TS/TSX 文件头 `/* */` 注释 + `docs/licenses/BBSGO_LICENSE` | | 待评审 |
| bbs-go | v4.4.5 | 7343253c | `server/`（后端附件服务设计） | 附件存储抽象、多存储设计思路 | 仅参考设计 | `backend/community/storage/` | `StorageService` / `KodoStorage` / `LocalStorage` | FastAPI/async 重写；七牛 SDK 用 `anyio.to_thread` 包裹 | 同上 | | 待评审 |
| 自研 | — | — | — | — | 新建 | `backend/community/persistence/board_applications.py` | 申请/审核持久化 | — | — | | 待评审 |
| 自研 | — | — | — | — | 新建 | `backend/community/services/board_application_service.py` | 建吧申请/审核业务 | — | — | | 待评审 |
| 自研 | — | — | — | — | 新建 | `backend/community/api/applications.py` / `admin.py` / `uploads.py` | 申请/管理/上传路由 | — | — | | 待评审 |
| 自研 | — | — | — | — | 新建 | `backend/community/services/maintenance.py`（改造） | 附件清理流水线（orphan / deleted） | 并入现有 `CommunityMaintenance`，拆两阶段 | — | | 待评审 |
| 自研 | — | — | — | — | 新建 | `community_migrations/versions/0002_community_v2.py` | 扩展迁移 | — | — | | 待评审 |

## 注释规范

- Python 源文件头：BSD-3 来源加 `# Adapted from FlaskBB v2.2.1 (BSD-3)`；自研不加。
- TS/TSX/CSS 源文件头：bbs-go 参考加 `/* Layout/interaction inspiration from bbs-go v4.4.5 (GPL-3.0); reimplemented, not copied. */`。
- Markdown 注释使用 `<!-- -->`。
- 两份 LICENSE 已收录至 `docs/licenses/`：
  - `docs/licenses/FLASKBB_LICENSE`（BSD-3）
  - `docs/licenses/BBSGO_LICENSE`（GPL-3.0）

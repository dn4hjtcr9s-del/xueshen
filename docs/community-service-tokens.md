# Community 服务 token 签发与轮换（运维文档，方案 §10.3/D36/D43）

> 随 PR-D 交付。本文档只说明部署注入与轮换操作；签发工具用法见
> `python -m backend.auth_service.service_tokens --help`。

## 1. 三个服务 token（§10.3/D36）

| 环境变量 | principal（sub claim） | scope | 用途 |
|---|---|---|---|
| `COMMUNITY_READER_SERVICE_TOKEN` | `system:community-reader` | `community:source_read` | Memory 读社区来源（内部 Reader） |
| `COMMUNITY_SOURCE_DELETE_SERVICE_TOKEN` | `system:community-source-delete` | `memory:source_delete` | 社区删除事实投递 |
| `COMMUNITY_ACCOUNT_PURGE_SERVICE_TOKEN` | `system:community-purge` | `community:account_purge` | 账号数据合规清理 |

三个 token 互不复用。`community:source_read` / `community:account_purge` 已加入
`ALL_SCOPES`，但不加入 `AGENT_ALLOWED_SCOPES`（§13.3），不授予普通用户。

## 2. 签发

```bash
# 签发 300s（verifier 硬上限默认 300s；超过会告警）
uv run python -m backend.auth_service.service_tokens issue \
  --principal system:community-reader --scope community:source_read \
  --lifetime-seconds 300 --out .local/tokens/community-reader.jwt
```

- token 明文只写 stdout 或 `--out` 文件；issuer/exp 元信息走 stderr；
- 工具需要 `AUTH_PRIVATE_KEY_FILE` 指向签名私钥（与认证服务同一私钥）；
- 生产注入 secret store，不写入数据库、日志或前端配置（§10.4）。

## 3. identity 映射注册（必须，否则 401）

生产 verifier 对 `actor_type=system` 的 token 同样执行
`account_identity_mappings` 解析（`external_subject` 即 sub claim 字符串）。
每个 system principal 部署时必须注册映射：

```sql
INSERT INTO account_identity_mappings (internal_user_id, issuer, external_subject)
VALUES ('<固定内部账号 UUID>', 'gewu-auth', 'system:community-reader');
-- 以及 system:community-source-delete、system:community-purge
```

内部账号 UUID 可为专用系统账号（不可登录），与 Conversation 服务账号同模式。

## 4. 轮换

verifier 强制 `exp - iat ≤ auth_token_max_lifetime_seconds`（默认 300 秒），
**静态长寿命 token 会被直接拒绝**（§13.1 v1.5 补充），因此必须短时轮换：

1. 用工具签发新 token（300s），立即注入 secret store；
2. 在旧 token 到期前（建议 ≤ 2 分钟窗口）滚动重启/重载目标进程；
3. 每 5 分钟或随发布节奏重复；建议由 CI/CD 在部署前统一签发。

`COMMUNITY_*` 链路是否启用的判定是显式 bool + token presence 分离（§13.2）：
存在 token 不代表已批准启用，按灰度顺序（§14.2 步骤 7）先启
`COMMUNITY_SOURCE_DELETION_ENABLED`，再启 `COMMUNITY_MEMORY_SUBMIT_ENABLED`。

## 5. 关联配置（§13.2）

- `COMMUNITY_READER_BASE_URL`：两个 Memory runtime（memory-api 与 memory-worker）
  必须使用同一内部 URL 和 reader token（Compose 内为 `http://memory-api:8000`）；
- 任一 token/base URL 缺失时对应链路保持 disabled 并告警（fail-closed）。

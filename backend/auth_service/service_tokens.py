"""服务 token 签发工具（方案 community §10.3/§13.1，D43/D36 冻结）。

用法：
    python -m backend.auth_service.service_tokens issue \\
        --principal system:community-reader --scope community:source_read \\
        [--lifetime-seconds 300] [--out <文件>]

- token 明文只写 stdout 或 --out 文件；issuer/exp 元信息走 stderr；
- 默认 300s（verifier 硬上限 auth_token_max_lifetime_seconds），超出上限时
  告警但允许（运维可能同步调高 verifier 上限）；
- **sub claim 即 principal 名**（D36 冻结，如 system:community-reader），
  不适用普通用户的 UUID sub 契约；verifier 的 system 分支经
  account_identity_mappings 解析 sub（external_subject 列即 principal 名），
  部署时必须为每个 system principal 注册映射，否则 401。

部署注入与轮换说明见 docs/community-service-tokens.md（随 PR-D 交付）。
"""

from __future__ import annotations

import argparse
import sys
import time
from datetime import UTC, datetime
from uuid import uuid4

import jwt

from backend.auth.context import ALL_SCOPES
from backend.auth_service.tokens import AccessTokenIssuer
from backend.settings import Settings


def _warn(message: str) -> None:
    print(f"[warning] {message}", file=sys.stderr)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m backend.auth_service.service_tokens",
        description="签发 system principal 短时服务 token（D43）",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    issue = sub.add_parser("issue", help="签发服务 token")
    issue.add_argument(
        "--principal", required=True, help="system principal 名（D36），如 system:community-reader"
    )
    issue.add_argument(
        "--scope", required=True, action="append", help="scope（可重复）；必须属于 ALL_SCOPES"
    )
    issue.add_argument(
        "--lifetime-seconds", type=int, default=300, help="有效期秒数（默认 300，verifier 上限）"
    )
    issue.add_argument("--out", default=None, help="输出文件（默认 stdout）")
    return parser


def issue_service_token(
    *,
    principal: str,
    scopes: list[str],
    lifetime_seconds: int,
    settings: Settings,
) -> str:
    """签发 system principal 短时 token（D43）。

    claims 与 AccessTokenIssuer 同构（iss/aud/sub/actor_type/scopes/iat/exp/jti），
    但 sub 为 principal 名而非用户 UUID（D36）；actor_type=system。
    """
    issuer = AccessTokenIssuer(settings)
    now = int(time.time())
    claims = {
        "iss": issuer.issuer,
        "aud": issuer.audience,
        "sub": principal,
        "actor_type": "system",
        "scopes": sorted(scopes),
        "iat": now,
        "exp": now + lifetime_seconds,
        "jti": str(uuid4()),
    }
    private_key = issuer._load_private_key()  # 同域内部复用，避免重复 IO
    return jwt.encode(claims, private_key, algorithm="RS256")


def main() -> None:
    args = build_parser().parse_args()
    if args.command != "issue":
        raise SystemExit(f"未知命令: {args.command}")
    if not args.principal.startswith("system:"):
        raise SystemExit(f"principal 必须以 'system:' 开头: {args.principal!r}")
    unknown = set(args.scope) - set(ALL_SCOPES)
    if unknown:
        raise SystemExit(f"未知 scope（不在 ALL_SCOPES 中）: {sorted(unknown)}")
    if args.lifetime_seconds <= 0:
        raise SystemExit("--lifetime-seconds 必须为正整数")

    settings = Settings()
    max_lifetime = settings.auth_token_max_lifetime_seconds
    if args.lifetime_seconds > max_lifetime:
        _warn(
            f"--lifetime-seconds={args.lifetime_seconds} 超过 verifier 上限 "
            f"{max_lifetime}s：token 将被拒绝，除非同步调高"
            " AUTH_TOKEN_MAX_LIFETIME_SECONDS"
        )

    token = issue_service_token(
        principal=args.principal,
        scopes=args.scope,
        lifetime_seconds=args.lifetime_seconds,
        settings=settings,
    )
    if args.out:
        with open(args.out, "w", encoding="utf-8") as f:
            f.write(token + "\n")
        print(f"[ok] token 已写入 {args.out}", file=sys.stderr)
    else:
        print(token)
    exp_epoch = int(datetime.now(UTC).timestamp()) + args.lifetime_seconds
    print(
        f"[info] principal={args.principal} scopes={sorted(args.scope)} exp_epoch={exp_epoch}",
        file=sys.stderr,
    )


if __name__ == "__main__":
    main()

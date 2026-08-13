"""认证服务管理 CLI（方案 §10.3 / 附录 A.5 #17）：disable / enable。

- disable：置 status='disabled' 并撤销该用户全部 refresh family（方案 §4.4）。
- enable：恢复 status='active'。
- 第一版不提供自助注销（§10.3 文档明示）。

用法：python -m backend.auth_service.cli disable <username>
     python -m backend.auth_service.cli enable <username>
"""

from __future__ import annotations

import argparse
import asyncio
import sys

from sqlalchemy import text

from backend.auth_service.database import AuthDatabase
from backend.auth_service.session import revoke_all_families
from backend.settings import get_settings


async def _set_status(username: str, status: str) -> int:
    settings = get_settings()
    db = AuthDatabase(settings)
    try:
        async with db.session_factory() as session:
            async with session.begin():
                result = await session.execute(
                    text(
                        "UPDATE users SET status = :status, updated_at = now() "
                        "WHERE username = :username RETURNING user_id"
                    ),
                    {"status": status, "username": username},
                )
                row = result.first()
                if row is None:
                    print(f"用户不存在: {username}", file=sys.stderr)
                    return 2
                user_id = row[0]
                if status == "disabled":
                    revoked = await revoke_all_families(session, user_id)
                    print(f"已禁用用户 {username}（撤销 refresh token {revoked} 个）")
                else:
                    print(f"已启用用户 {username}")
        return 0
    finally:
        await db.close()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="auth-service-cli", description="认证账号管理")
    sub = parser.add_subparsers(dest="command", required=True)
    for name in ("disable", "enable"):
        p = sub.add_parser(name, help=f"{name} 用户账号")
        p.add_argument("username", help="规范化后的用户名")
    return parser


def main() -> None:
    args = build_parser().parse_args()
    status = "disabled" if args.command == "disable" else "active"
    sys.exit(asyncio.run(_set_status(args.username, status)))


if __name__ == "__main__":
    main()

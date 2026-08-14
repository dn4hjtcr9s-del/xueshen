"""Community 幂等记录清理 SQL 的回归测试。

确保 PostgreSQL 分批删除通过子查询限制行数，不再生成不支持的
``DELETE ... LIMIT`` 语法，避免维护任务在启动后持续报错。
"""

from __future__ import annotations

from typing import Any

from backend.community.persistence import idempotency


async def test_delete_expired_uses_postgresql_compatible_batch_delete(monkeypatch: Any) -> None:
    """分批清理应使用 ctid 子查询，并保留 batch_size 参数。"""
    captured: dict[str, Any] = {}

    async def fake_exec_rowcount(session: Any, sql: Any, params: dict[str, Any]) -> int:
        captured["sql"] = str(sql)
        captured["params"] = params
        return 7

    monkeypatch.setattr(idempotency, "exec_rowcount", fake_exec_rowcount)

    deleted = await idempotency.delete_expired(object(), batch_size=250)

    normalized_sql = " ".join(captured["sql"].split())
    assert deleted == 7
    assert "WHERE ctid IN ( SELECT ctid" in normalized_sql
    assert "ORDER BY expires_at LIMIT :batch_size" in normalized_sql
    assert "DELETE FROM community_idempotency_requests WHERE expires_at" not in normalized_sql
    assert captured["params"]["batch_size"] == 250

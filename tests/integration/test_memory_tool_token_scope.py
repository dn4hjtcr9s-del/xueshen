"""真实签发的记忆读取 token → 真实工具端点（评审 C-1）。

缺陷：``conversation/worker/main.py`` 的签发点只请求 ``memory:context``，而
``/internal/memory/tool/{prime,search,read}`` 的 guard 是 ``require(scope=memory:read)``，
``AuthContext.has_scope`` 又是精确成员判定 → 403；prime 的 4xx 在
``graph/nodes/memory.py`` **不可降级** → 打开 ``MEMORY_PRIME_ENABLED`` 后每轮 turn 必失败。

此前所有测试都注入 Fake 网关，从未覆盖"真实令牌 → 真实 guard"。这里用 auth_test
运行时（真实 RSA 密钥、真实 JWT 校验、真实身份映射表）走完整 HTTP 栈：

- 用 worker 的真实签发函数 ``issue_memory_context_token`` 产出的 token 打三个端点，
  断言**不再是 403**，且 search/read 能读到真实数据；
- 反向护栏：只带 ``memory:context`` 的旧令牌必须 403（证明本用例抓得住该回归）。
"""

from __future__ import annotations

from typing import Any
from uuid import UUID, uuid4

import httpx
import jwt
import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from backend.auth_service.agent_tokens import issue_agent_token
from backend.conversation.worker.main import (
    MEMORY_AGENT_SCOPES,
    issue_memory_context_token,
)
from backend.memory.persistence import documents as docs_repo
from backend.memory.storage.local_markdown import LocalMarkdownStore
from backend.settings import Settings

MEMORY_ID = "mastery:ellipse"
TOOL_CONTENT = "# 椭圆\n\n第一定义：到两定点距离之和为常数。\n"
TOOL_SEARCH_QUERY = "椭圆"
FALLBACK_ISSUER = "gewu-auth"

PRIME_PATH = "/api/v1/internal/memory/tool/prime"
SEARCH_PATH = "/api/v1/internal/memory/tool/search"
READ_PATH = "/api/v1/internal/memory/tool/read"


async def _seed_readable_memory(
    session_factory: async_sessionmaker[AsyncSession],
    store: LocalMarkdownStore,
    user_id: UUID,
) -> None:
    """造一份可读的活动版本 + 注册表投影：read 能读到正文、search 能命中。"""
    stored = await store.write_immutable_version(
        user_id=user_id, memory_id=MEMORY_ID, version=1, content=TOOL_CONTENT.encode()
    )
    async with session_factory() as session:
        async with session.begin():
            await docs_repo.upsert_document(
                session,
                user_id=user_id,
                memory_id=MEMORY_ID,
                memory_type="mastery",
                topic_key="ellipse",
                topic_title="椭圆",
                logical_path="mastery/ellipse.md",
            )
            await docs_repo.set_active_version(
                session,
                user_id=user_id,
                memory_id=MEMORY_ID,
                active_version=1,
                active_storage_key=stored.storage_key,
                active_checksum=stored.checksum,
            )
            await session.execute(
                text(
                    "INSERT INTO memory_index_entries ("
                    "  user_id, memory_id, source_version, memory_type, topic_key, title,"
                    "  summary, keywords, aliases, search_text, updated_at"
                    ") VALUES ("
                    "  :user_id, :memory_id, 1, 'mastery', 'ellipse', '椭圆', '圆锥曲线之一',"
                    "  CAST(ARRAY['焦点'] AS text[]), CAST(ARRAY['ellipse'] AS text[]),"
                    "  '椭圆 圆锥曲线之一', now())"
                ),
                {"user_id": user_id, "memory_id": MEMORY_ID},
            )


async def _bind_delegated_user(
    session_factory: async_sessionmaker[AsyncSession], user_id: UUID, *, issuer: str
) -> None:
    """Agent token 的 delegated_sub 经身份映射表解析回内部 user_id（§3.1/§18.4）。"""
    async with session_factory() as session:
        async with session.begin():
            await session.execute(
                text(
                    "INSERT INTO account_identity_mappings "
                    "(internal_user_id, issuer, external_subject) "
                    "VALUES (:user_id, :issuer, :subject)"
                ),
                {"user_id": user_id, "issuer": issuer, "subject": str(user_id)},
            )


def _issue_token_via_worker(
    monkeypatch: pytest.MonkeyPatch, settings: Settings, user_id: UUID
) -> str:
    """调用 worker 的真实签发函数（密钥取注入的 Settings，而非进程 env）。"""
    monkeypatch.setattr("backend.auth_service.agent_tokens.get_settings", lambda: settings)
    return issue_memory_context_token(settings=settings, user_id=str(user_id))


def _auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


async def test_worker_issued_token_passes_memory_tool_endpoints(
    auth_api_client: httpx.AsyncClient,
    auth_test_settings: Settings,
    session_factory: async_sessionmaker[AsyncSession],
    store: LocalMarkdownStore,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """C-1：worker 签发的令牌打 prime/search/read 一律不再 403。"""
    user_id = uuid4()
    await _seed_readable_memory(session_factory, store, user_id)
    await _bind_delegated_user(
        session_factory, user_id, issuer=auth_test_settings.auth_issuer or FALLBACK_ISSUER
    )
    token = _issue_token_via_worker(monkeypatch, auth_test_settings, user_id)
    headers = _auth(token)

    prime = await auth_api_client.post(PRIME_PATH, json={}, headers=headers)
    assert prime.status_code != 403, f"prime 不得 403（C-1）: {prime.text}"
    assert prime.status_code == 200, prime.text
    assert [entry["memory_id"] for entry in prime.json()["index_entries"]] == [MEMORY_ID]

    search = await auth_api_client.post(
        SEARCH_PATH, json={"queries": [TOOL_SEARCH_QUERY]}, headers=headers
    )
    assert search.status_code == 200, search.text
    assert [item["memory_id"] for item in search.json()["items"]] == [MEMORY_ID]

    read = await auth_api_client.post(
        READ_PATH,
        json={"memory_id": MEMORY_ID, "line_offset": 0, "max_lines": 200},
        headers=headers,
    )
    assert read.status_code == 200, read.text
    assert "第一定义" in read.json()["content"]


async def test_worker_issued_token_carries_context_and_read_scopes(
    auth_test_settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    """令牌必须**同时**带 memory:context（既有用法）与 memory:read（工具端点）。"""
    token = _issue_token_via_worker(monkeypatch, auth_test_settings, uuid4())
    claims: dict[str, Any] = jwt.decode(
        token,
        auth_test_settings.auth_public_key,
        algorithms=["RS256"],
        audience=auth_test_settings.auth_audience,
        issuer=auth_test_settings.auth_issuer or FALLBACK_ISSUER,
    )
    assert set(claims["scopes"]) == {"memory:context", "memory:read"}
    assert set(MEMORY_AGENT_SCOPES) == {"memory:context", "memory:read"}, (
        "两个 scope 都必须在 AGENT_ALLOWED_SCOPES 白名单内，否则签发直接抛错"
    )


async def test_context_only_token_is_forbidden_on_tool_endpoints(
    auth_api_client: httpx.AsyncClient,
    auth_test_settings: Settings,
    session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """回归护栏：只带 memory:context 的令牌必须 403（即 C-1 的根因，防止回退）。"""
    user_id = uuid4()
    await _bind_delegated_user(
        session_factory, user_id, issuer=auth_test_settings.auth_issuer or FALLBACK_ISSUER
    )
    monkeypatch.setattr(
        "backend.auth_service.agent_tokens.get_settings", lambda: auth_test_settings
    )
    context_only = issue_agent_token(
        agent_subject="conversation-agent-test",
        delegated_sub=str(user_id),
        actor_type="conversation_agent",
        requested_scopes=["memory:context"],
    )

    response = await auth_api_client.post(PRIME_PATH, json={}, headers=_auth(context_only))
    assert response.status_code == 403, "缺 memory:read 时必须 403（本断言证明用例有效）"

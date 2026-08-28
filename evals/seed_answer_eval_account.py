"""创建 Answer Eval 专用账号并写入唯一共享的只读 Fixture。

脚本只准备测试数据，不启动模型、不创建 Conversation Turn、不运行评测。
账号使用固定 user_id；Memory 文档、检索索引和知识图谱 Overlay 都按固定版本写入，
后续 Answer Eval 必须以只读方式使用它们。
"""

from __future__ import annotations

import asyncio
import json
import os
import secrets
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import UUID, uuid5

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker, create_async_engine

from backend.auth_service.security import hash_password
from backend.memory.storage.base import logical_path_for
from backend.memory.storage.local_markdown import LocalMarkdownStore
from backend.memory.storage.markdown_schema import (
    IndexDocument,
    IndexEntry,
    LearnerDocument,
    MasteryDocument,
    render_index,
    render_learner,
    render_mastery,
)
from backend.settings import get_settings

ROOT = Path(__file__).resolve().parents[1]
FIXTURE_PATH = ROOT / "evals" / "answer_eval_fixture_v1.json"
CASES_PATH = ROOT / "evals" / "answer_eval_cases_v1.jsonl"
CREDENTIAL_PATH = ROOT / ".local" / "answer_eval_account.json"

AUTH_OUTBOX_EVENT_ID = uuid5(
    UUID("6ba7b810-9dad-11d1-80b4-00c04fd430c8"),
    "xueshen:answer-eval-auth-outbox:v1",
)
SEED_NAMESPACE = UUID("6ba7b810-9dad-11d1-80b4-00c04fd430c8")
FIXED_AT = datetime.fromisoformat("2026-08-18T00:00:00+00:00")


def _load_fixture() -> dict[str, Any]:
    """读取并校验共享 Fixture 的基本身份信息。"""
    fixture = json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))
    if fixture.get("fixture_id") != "answer-eval-shared-state-v1":
        raise ValueError("Fixture ID 不符合 Answer Eval 固定约定")
    account = fixture.get("account") or {}
    if account.get("username") != "answer_eval_2026":
        raise ValueError("Fixture 账号用户名不符合固定约定")
    if fixture.get("freeze_policy", {}).get("memory_submit_enabled") is not False:
        raise ValueError("Answer Eval Fixture 必须关闭 Memory Submit")
    if fixture.get("freeze_policy", {}).get("graph_states_read_only_during_eval") is not True:
        raise ValueError("Answer Eval Fixture 必须声明图谱只读")
    return fixture


def _load_cases() -> list[dict[str, Any]]:
    """确认 Case 全部引用同一 Fixture；不执行任何 Case。"""
    rows = [
        json.loads(line)
        for line in CASES_PATH.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    if len(rows) != 100:
        raise ValueError(f"Answer Eval Case 数量必须为 100，实际 {len(rows)}")
    if any(row.get("fixture_id") != "answer-eval-shared-state-v1" for row in rows):
        raise ValueError("发现没有引用共享 Fixture 的 Case")
    if any("memory" in row or "graph_states" in row for row in rows):
        raise ValueError("Case 不得携带独立 Memory 或图谱状态")
    return rows


def _load_or_create_password(user_id: UUID) -> str:
    """生成本地专用账号密码，并以 0600 权限保存，不进入版本库。"""
    if CREDENTIAL_PATH.exists():
        payload = json.loads(CREDENTIAL_PATH.read_text(encoding="utf-8"))
        if payload.get("user_id") != str(user_id):
            raise ValueError(f"{CREDENTIAL_PATH} 已属于其他 user_id")
        password = str(payload.get("password") or "")
        if len(password) >= 10:
            return password
    password = f"AnswerEval-{secrets.token_urlsafe(18)}-2026"
    CREDENTIAL_PATH.parent.mkdir(parents=True, exist_ok=True)
    CREDENTIAL_PATH.write_text(
        json.dumps(
            {
                "purpose": "本地 Answer Eval 专用账号；仅用于后续只读评测。",
                "username": "answer_eval_2026",
                "user_id": str(user_id),
                "password": password,
            },
            ensure_ascii=False,
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    os.chmod(CREDENTIAL_PATH, 0o600)
    return password


def _memory_document_payloads(
    fixture: dict[str, Any], user_id: UUID
) -> tuple[list[tuple[str, str, bytes, dict[str, Any]]], bytes]:
    """渲染 learner/mastery/index 三类文档，返回待写入内容和索引。"""
    memory = fixture["memory"]
    learner_data = memory["learner"]
    learner = LearnerDocument(
        user_id=user_id,
        version=int(learner_data["version"]),
        updated_at=FIXED_AT,
        preferences=list(learner_data["preferences"]),
        goals=list(learner_data["goals"]),
        plans=list(learner_data["plans"]),
        evidence_refs=list(learner_data["evidence_refs"]),
        confidence=float(learner_data["confidence"]),
    )
    documents: list[tuple[str, str, bytes, dict[str, Any]]] = []
    documents.append(
        (
            "learner",
            "learner",
            render_learner(learner).encode("utf-8"),
            {
                "memory_type": "learner",
                "topic_key": None,
                "title": "学习者档案",
                "summary": "；".join(learner.preferences[:3]),
                "keywords": ["学习偏好", "学习目标", "数学"],
                "search_text": " ".join([*learner.preferences, *learner.goals, *learner.plans]),
                "confidence": learner.confidence,
                "evidence_refs": learner.evidence_refs,
            },
        )
    )

    index_entries = [
        IndexEntry(
            memory_id="learner",
            memory_type="learner",
            topic_key=None,
            title="学习者档案",
            version=learner.version,
            updated_at=FIXED_AT,
        )
    ]
    for item in memory["mastery"]:
        doc = MasteryDocument(
            user_id=user_id,
            topic_key=str(item["topic_key"]),
            topic_title=str(item["topic_title"]),
            version=int(item["version"]),
            updated_at=FIXED_AT,
            overview=str(item["overview"]),
            understood=list(item["understood"]),
            difficulties=list(item["difficulties"]),
            review_advice=list(item["review_advice"]),
            evidence_refs=list(item["evidence_refs"]),
            confidence=float(item["confidence"]),
        )
        memory_id = f"mastery:{doc.topic_key}"
        documents.append(
            (
                memory_id,
                "mastery",
                render_mastery(doc).encode("utf-8"),
                {
                    "memory_type": "mastery",
                    "topic_key": doc.topic_key,
                    "title": doc.topic_title,
                    "summary": doc.overview,
                    "keywords": [doc.topic_title, *doc.difficulties],
                    "search_text": " ".join(
                        [
                            doc.topic_title,
                            doc.overview,
                            *doc.understood,
                            *doc.difficulties,
                            *doc.review_advice,
                        ]
                    ),
                    "confidence": doc.confidence,
                    "evidence_refs": doc.evidence_refs,
                },
            )
        )
        index_entries.append(
            IndexEntry(
                memory_id=memory_id,
                memory_type="mastery",
                topic_key=doc.topic_key,
                title=doc.topic_title,
                version=doc.version,
                updated_at=FIXED_AT,
            )
        )

    index = IndexDocument(
        user_id=user_id,
        version=1,
        updated_at=FIXED_AT,
        learner=index_entries[0],
        mastery_entries=index_entries[1:],
    )
    return documents, render_index(index).encode("utf-8")


async def _ensure_auth_account(settings: Any, fixture: dict[str, Any], password: str) -> UUID:
    """幂等创建认证账号与已完成的身份映射 outbox 记录。"""
    account = fixture["account"]
    user_id = UUID(str(account["user_id"]))
    engine: AsyncEngine = create_async_engine(str(settings.auth_database_url))
    try:
        async with engine.begin() as conn:
            existing = (
                await conn.execute(
                    text("SELECT user_id FROM users WHERE username = :username"),
                    {"username": account["username"]},
                )
            ).scalar_one_or_none()
            if existing is not None and UUID(str(existing)) != user_id:
                raise RuntimeError("answer_eval_2026 已存在但 user_id 不匹配，停止写入")
            await conn.execute(
                text(
                    """
                    INSERT INTO users (user_id, username, email, password_hash, status)
                    VALUES (:user_id, :username, :email, :password_hash, 'active')
                    ON CONFLICT (user_id) DO UPDATE SET
                        username = EXCLUDED.username,
                        email = EXCLUDED.email,
                        password_hash = EXCLUDED.password_hash,
                        status = 'active',
                        updated_at = now()
                    """
                ),
                {
                    "user_id": user_id,
                    "username": account["username"],
                    "email": account["email"],
                    "password_hash": hash_password(password),
                },
            )
            await conn.execute(
                text(
                    """
                    INSERT INTO identity_mapping_outbox (
                        event_id, user_id, issuer, external_subject, status, attempts,
                        next_attempt_at, done_at
                    ) VALUES (
                        :event_id, :user_id, :issuer, :external_subject, 'done', 1,
                        :fixed_at, :fixed_at
                    )
                    ON CONFLICT (event_id) DO UPDATE SET
                        user_id = EXCLUDED.user_id,
                        issuer = EXCLUDED.issuer,
                        external_subject = EXCLUDED.external_subject,
                        status = 'done',
                        done_at = EXCLUDED.done_at
                    """
                ),
                {
                    "event_id": AUTH_OUTBOX_EVENT_ID,
                    "user_id": user_id,
                    "issuer": account["issuer"],
                    "external_subject": account["external_subject"],
                    "fixed_at": FIXED_AT,
                },
            )
    finally:
        await engine.dispose()
    return user_id


async def _write_memory_fixture(settings: Any, fixture: dict[str, Any], user_id: UUID) -> None:
    """写入 Memory 文档、检索索引、图谱链接和固定 Overlay。"""
    documents, index_content = _memory_document_payloads(fixture, user_id)
    store = LocalMarkdownStore(settings.memory_storage_root)
    stored: dict[str, tuple[str, str]] = {}
    for memory_id, _memory_type, content, _index_data in documents:
        version = 1
        item = await store.write_immutable_version(
            user_id=user_id,
            memory_id=memory_id,
            version=version,
            content=content,
        )
        await store.materialize_current(user_id=user_id, memory_id=memory_id, content=content)
        stored[memory_id] = (item.storage_key, item.checksum)
    index_item = await store.write_immutable_version(
        user_id=user_id,
        memory_id="index",
        version=1,
        content=index_content,
    )
    await store.materialize_current(user_id=user_id, memory_id="index", content=index_content)
    stored["index"] = (index_item.storage_key, index_item.checksum)

    engine: AsyncEngine = create_async_engine(str(settings.database_url))
    factory: async_sessionmaker[AsyncSession] = async_sessionmaker(
        engine, expire_on_commit=False, autoflush=False
    )
    try:
        async with factory() as session:
            async with session.begin():
                await session.execute(
                    text(
                        """
                        INSERT INTO account_identity_mappings (
                            internal_user_id, issuer, external_subject
                        ) VALUES (:user_id, :issuer, :external_subject)
                        ON CONFLICT (internal_user_id, issuer) DO UPDATE SET
                            external_subject = EXCLUDED.external_subject
                        """
                    ),
                    {
                        "user_id": user_id,
                        "issuer": fixture["account"]["issuer"],
                        "external_subject": fixture["account"]["external_subject"],
                    },
                )
                for memory_id, memory_type, _content, index_data in documents:
                    storage_key, checksum = stored[memory_id]
                    logical_path = logical_path_for(memory_id)
                    await session.execute(
                        text(
                            """
                            INSERT INTO memory_documents (
                                user_id, memory_id, memory_type, topic_key, topic_title,
                                logical_path, active_version, active_storage_key,
                                active_checksum, deleted_version, deleted_at,
                                tombstone_until, index_dirty_at, updated_at
                            ) VALUES (
                                :user_id, :memory_id, :memory_type, :topic_key, :topic_title,
                                :logical_path, 1, :storage_key, :checksum, NULL, NULL,
                                NULL, NULL, :fixed_at
                            )
                            ON CONFLICT (user_id, memory_id) DO UPDATE SET
                                memory_type = EXCLUDED.memory_type,
                                topic_key = EXCLUDED.topic_key,
                                topic_title = EXCLUDED.topic_title,
                                logical_path = EXCLUDED.logical_path,
                                active_version = 1,
                                active_storage_key = EXCLUDED.active_storage_key,
                                active_checksum = EXCLUDED.active_checksum,
                                deleted_version = NULL,
                                deleted_at = NULL,
                                tombstone_until = NULL,
                                index_dirty_at = NULL,
                                updated_at = EXCLUDED.updated_at
                            """
                        ),
                        {
                            "user_id": user_id,
                            "memory_id": memory_id,
                            "memory_type": memory_type,
                            "topic_key": index_data["topic_key"],
                            "topic_title": index_data["title"] if memory_type == "mastery" else None,
                            "logical_path": logical_path,
                            "storage_key": storage_key,
                            "checksum": checksum,
                            "fixed_at": FIXED_AT,
                        },
                    )
                    await session.execute(
                        text(
                            """
                            INSERT INTO memory_index_entries (
                                user_id, memory_id, source_version, memory_type, topic_key,
                                title, summary, keywords, search_text, evidence_refs,
                                confidence, updated_at
                            ) VALUES (
                                :user_id, :memory_id, 1, :memory_type, :topic_key,
                                :title, :summary, :keywords, :search_text,
                                CAST(:evidence_refs AS jsonb), :confidence, :fixed_at
                            )
                            ON CONFLICT (user_id, memory_id) DO UPDATE SET
                                source_version = 1,
                                memory_type = EXCLUDED.memory_type,
                                topic_key = EXCLUDED.topic_key,
                                title = EXCLUDED.title,
                                summary = EXCLUDED.summary,
                                keywords = EXCLUDED.keywords,
                                search_text = EXCLUDED.search_text,
                                evidence_refs = EXCLUDED.evidence_refs,
                                confidence = EXCLUDED.confidence,
                                updated_at = EXCLUDED.updated_at
                            """
                        ),
                        {
                            "user_id": user_id,
                            "memory_id": memory_id,
                            "memory_type": memory_type,
                            "topic_key": index_data["topic_key"],
                            "title": index_data["title"],
                            "summary": index_data["summary"],
                            "keywords": index_data["keywords"],
                            "search_text": index_data["search_text"],
                            "evidence_refs": json.dumps(index_data["evidence_refs"], ensure_ascii=False),
                            "confidence": index_data["confidence"],
                            "fixed_at": FIXED_AT,
                        },
                    )

                # index.md 也需要活动指针，供系统读取固定 Memory 目录。
                index_storage_key, index_checksum = stored["index"]
                await session.execute(
                    text(
                        """
                        INSERT INTO memory_documents (
                            user_id, memory_id, memory_type, topic_key, topic_title,
                            logical_path, active_version, active_storage_key,
                            active_checksum, deleted_version, deleted_at,
                            tombstone_until, index_dirty_at, updated_at
                        ) VALUES (
                            :user_id, 'index', 'index', NULL, NULL, 'index.md',
                            1, :storage_key, :checksum, NULL, NULL, NULL, NULL, :fixed_at
                        )
                        ON CONFLICT (user_id, memory_id) DO UPDATE SET
                            memory_type = 'index',
                            topic_key = NULL,
                            topic_title = NULL,
                            logical_path = 'index.md',
                            active_version = 1,
                            active_storage_key = EXCLUDED.active_storage_key,
                            active_checksum = EXCLUDED.active_checksum,
                            deleted_version = NULL,
                            deleted_at = NULL,
                            tombstone_until = NULL,
                            index_dirty_at = NULL,
                            updated_at = EXCLUDED.updated_at
                        """
                    ),
                    {
                        "user_id": user_id,
                        "storage_key": index_storage_key,
                        "checksum": index_checksum,
                        "fixed_at": FIXED_AT,
                    },
                )

                for item in fixture["memory"]["mastery"]:
                    memory_id = f"mastery:{item['topic_key']}"
                    await session.execute(
                        text(
                            """
                            INSERT INTO memory_graph_links (
                                user_id, memory_id, node_id, memory_version,
                                mapping_method, mapping_confidence, active, updated_at
                            ) VALUES (
                                :user_id, :memory_id, :node_id, 1,
                                'explicit_hint', :confidence, true, :fixed_at
                            )
                            ON CONFLICT (user_id, memory_id, node_id) DO UPDATE SET
                                memory_version = 1,
                                mapping_method = 'explicit_hint',
                                mapping_confidence = EXCLUDED.mapping_confidence,
                                active = true,
                                updated_at = EXCLUDED.updated_at
                            """
                        ),
                        {
                            "user_id": user_id,
                            "memory_id": memory_id,
                            "node_id": item["graph_node_id"],
                            "confidence": 0.99,
                            "fixed_at": FIXED_AT,
                        },
                    )

                for index, state in enumerate(fixture["graph_states"], start=1):
                    memory_id = str(state["source_memory_id"])
                    audit_id = uuid5(SEED_NAMESPACE, f"answer-eval-graph-audit:{state['node_id']}")
                    await session.execute(
                        text(
                            """
                            INSERT INTO graph_user_states (
                                user_id, node_id, status, version, status_source,
                                source_memory_id, source_memory_version, evidence_snapshot,
                                evidence_count, last_user_action_at, last_evidence_at,
                                created_at, updated_at
                            ) VALUES (
                                :user_id, :node_id, :status, 1, :status_source,
                                :source_memory_id, 1, CAST(:evidence_snapshot AS jsonb),
                                :evidence_count, NULL, :fixed_at, :fixed_at, :fixed_at
                            )
                            ON CONFLICT (user_id, node_id) DO UPDATE SET
                                status = EXCLUDED.status,
                                version = 1,
                                status_source = EXCLUDED.status_source,
                                source_memory_id = EXCLUDED.source_memory_id,
                                source_memory_version = 1,
                                evidence_snapshot = EXCLUDED.evidence_snapshot,
                                evidence_count = EXCLUDED.evidence_count,
                                last_evidence_at = EXCLUDED.last_evidence_at,
                                updated_at = EXCLUDED.updated_at
                            """
                        ),
                        {
                            "user_id": user_id,
                            "node_id": state["node_id"],
                            "status": state["status"],
                            "status_source": state["status_source"],
                            "source_memory_id": memory_id,
                            "evidence_snapshot": json.dumps(
                                [
                                    {
                                        "evidence_ref": f"fixture:answer-eval-shared-state-v1:graph:{state['node_id']}",
                                        "direction": "learning",
                                        "strength": 0.9,
                                    }
                                ],
                                ensure_ascii=False,
                            ),
                            "evidence_count": 1,
                            "fixed_at": FIXED_AT,
                        },
                    )
                    await session.execute(
                        text(
                            """
                            INSERT INTO graph_state_audit (
                                audit_id, operation_id, user_id, node_id,
                                before_status, after_status, before_version, after_version,
                                actor_type, reason_codes, evidence_refs,
                                explanation_summary, created_at
                            ) VALUES (
                                :audit_id, NULL, :user_id, :node_id,
                                NULL, :after_status, NULL, 1, 'summary_projection',
                                :reason_codes, CAST(:evidence_refs AS jsonb),
                                :explanation_summary, :fixed_at
                            )
                            ON CONFLICT (audit_id) DO UPDATE SET
                                after_status = EXCLUDED.after_status,
                                after_version = 1,
                                reason_codes = EXCLUDED.reason_codes,
                                evidence_refs = EXCLUDED.evidence_refs,
                                explanation_summary = EXCLUDED.explanation_summary
                            """
                        ),
                        {
                            "audit_id": audit_id,
                            "user_id": user_id,
                            "node_id": state["node_id"],
                            "after_status": state["status"],
                            "reason_codes": state["reason_codes"],
                            "evidence_refs": json.dumps(
                                [f"fixture:answer-eval-shared-state-v1:graph:{state['node_id']}"],
                                ensure_ascii=False,
                            ),
                            "explanation_summary": state["explanation_summary"],
                            "fixed_at": FIXED_AT,
                        },
                    )
    finally:
        await engine.dispose()


async def prepare() -> dict[str, Any]:
    """执行账号和 Fixture 的幂等准备，不执行评测。"""
    fixture = _load_fixture()
    cases = _load_cases()
    user_id = UUID(str(fixture["account"]["user_id"]))
    password = _load_or_create_password(user_id)
    settings = get_settings()
    await _ensure_auth_account(settings, fixture, password)
    await _write_memory_fixture(settings, fixture, user_id)
    return {
        "fixture_id": fixture["fixture_id"],
        "user_id": str(user_id),
        "username": fixture["account"]["username"],
        "case_count": len(cases),
        "credential_file": str(CREDENTIAL_PATH),
        "memory_storage_root": str(settings.memory_storage_root),
        "memory_write_enabled_for_future_eval": False,
        "graph_write_enabled_for_future_eval": False,
    }


def main() -> None:
    """CLI 入口。"""
    result = asyncio.run(prepare())
    print(json.dumps(result, ensure_ascii=False, indent=2))
    print("未启动模型、未创建 Turn、未运行测试或 Answer Eval。")


if __name__ == "__main__":
    main()

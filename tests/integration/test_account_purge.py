"""账号物理删除集成测试（§13.16 / §21.3，评审 P0-1/P0-2 修复）。

覆盖：逐表物理删除、break-glass 压缩、Markdown/Checkpoint 清理、manifest 与
ops ledger 原子推进、运行中任务 drain 重试、完成后幂等重放合成结果。
"""

from __future__ import annotations

import dataclasses
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from backend.memory.contracts.commands import MaintenanceCommand
from backend.memory.contracts.common import user_privacy_hash
from backend.memory.graph.runner import LocalLangGraphRunner
from backend.memory.graph.state import MemoryRuntimeContext
from backend.memory.persistence import account_deletion as deletion_repo
from backend.memory.persistence.identity import IdentityMappingRepository
from backend.memory.services.account_purge import AccountPurgeNotDrainedError
from backend.memory.worker.checkpoint import (
    CheckpointCleanupAdapter,
    thread_id_for_operation,
)
from backend.settings import Settings
from tests.integration.graph_helpers import make_operation, persist_operation

USER = UUID("00000000-0000-0000-0000-00000000b001")
ADMIN = UUID("00000000-0000-0000-0000-00000000a001")
HEX64 = "ab" * 32


class _FakeSaver:
    """记录 adelete_thread 调用的假 checkpointer。"""

    def __init__(self) -> None:
        self.deleted: list[str] = []

    async def setup(self) -> None:
        return None

    async def adelete_thread(self, thread_id: str) -> None:
        self.deleted.append(thread_id)


async def _seed_user_data(
    session_factory: async_sessionmaker[AsyncSession],
    settings: Settings,
    *,
    with_running_op: bool = False,
) -> dict[str, UUID]:
    """在全部 purge 目标表中播种 USER 的数据，返回关键 ID。"""
    user_op = make_operation(
        user_id=USER,
        actor_type="user",
        input_kind="maintenance",
        operation_type="rebuild_index",
        priority=0,
        payload=MaintenanceCommand(kind="rebuild_index"),
    )
    await persist_operation(session_factory, user_op)
    running_op = None
    if with_running_op:
        running_op = make_operation(
            user_id=USER,
            actor_type="user",
            input_kind="maintenance",
            operation_type="rebuild_index",
            priority=0,
            payload=MaintenanceCommand(kind="rebuild_index"),
        )
        await persist_operation(session_factory, running_op)

    async with session_factory() as session:
        async with session.begin():
            await IdentityMappingRepository(session).create(
                internal_user_id=USER,
                issuer="https://accounts.example",
                external_subject="ext-purge-1",
            )
            await session.execute(
                text(
                    "INSERT INTO memory_documents (user_id, memory_id, memory_type, "
                    "logical_path, active_version, active_storage_key, active_checksum) "
                    "VALUES (:u, 'learner', 'learner', 'learner.md', 1, 'k1', :ck)"
                ),
                {"u": USER, "ck": HEX64},
            )
            await session.execute(
                text(
                    "INSERT INTO memory_commits (commit_id, mutation_id, operation_id, "
                    "user_id, memory_id, action, actor_type, after_version, checksum) "
                    "VALUES (:cid, :mid, :op, :u, 'learner', 'create', 'user', 1, :ck)"
                ),
                {
                    "cid": uuid4(),
                    "mid": uuid4(),
                    "op": user_op.operation_id,
                    "u": USER,
                    "ck": HEX64,
                },
            )
            await session.execute(
                text(
                    "INSERT INTO memory_index_entries (user_id, memory_id, source_version, "
                    "memory_type, title, summary, search_text, updated_at) "
                    "VALUES (:u, 'learner', 1, 'learner', 't', 's', 't s', now())"
                ),
                {"u": USER},
            )
            await session.execute(
                text(
                    "INSERT INTO memory_review_candidates (candidate_id, operation_id, user_id, "
                    "candidate_type, normalized_match_key, candidate_payload, confidence, status) "
                    "VALUES (:cid, :op, :u, 'mastery', 'k', '{}'::jsonb, 0.7, 'pending')"
                ),
                {"cid": uuid4(), "op": user_op.operation_id, "u": USER},
            )
            await session.execute(
                text(
                    "INSERT INTO memory_deleted_evidence_suppressions "
                    "(user_id, memory_id, evidence_ref_hash, hash_key_version) "
                    "VALUES (:u, 'learner', :h, 'v1')"
                ),
                {"u": USER, "h": HEX64},
            )
            await session.execute(
                text(
                    "INSERT INTO knowledge_graph_nodes (node_id, title, source_file, "
                    "source_checksum) VALUES ('n9001', '测试节点', 'test.md', :ck) "
                    "ON CONFLICT (node_id) DO NOTHING"
                ),
                {"ck": HEX64},
            )
            await session.execute(
                text(
                    "INSERT INTO memory_graph_links (user_id, memory_id, node_id, "
                    "memory_version, mapping_method, mapping_confidence) "
                    "VALUES (:u, 'learner', 'n9001', 1, 'explicit_hint', 0.95)"
                ),
                {"u": USER},
            )
            await session.execute(
                text(
                    "INSERT INTO graph_user_states (user_id, node_id, status, status_source) "
                    "VALUES (:u, 'n9001', 'learning', 'user')"
                ),
                {"u": USER},
            )
            await session.execute(
                text(
                    "INSERT INTO graph_user_node_activity (user_id, node_id) VALUES (:u, 'n9001')"
                ),
                {"u": USER},
            )
            await session.execute(
                text(
                    "INSERT INTO graph_activity_seen_events "
                    "(user_id, node_id, activity_type, activity_id) "
                    "VALUES (:u, 'n9001', 'page_view', 'a1')"
                ),
                {"u": USER},
            )
            await session.execute(
                text(
                    "INSERT INTO graph_state_audit (audit_id, operation_id, user_id, node_id, "
                    "actor_type) VALUES (:aid, :op, :u, 'n9001', 'user')"
                ),
                {"aid": uuid4(), "op": user_op.operation_id, "u": USER},
            )
            await session.execute(
                text(
                    "INSERT INTO source_deletions (source_deletion_id, user_id, source_system, "
                    "source_ref, deleted_at, idempotency_hash) "
                    "VALUES (:sid, :u, 'conversation', 'ref-1', now(), :h)"
                ),
                {"sid": uuid4(), "u": USER, "h": HEX64},
            )
            outbox_id = uuid4()
            await session.execute(
                text(
                    "INSERT INTO memory_outbox (outbox_id, operation_id, user_id, event_type, "
                    "aggregate_type, aggregate_id, aggregate_version, payload) "
                    "VALUES (:oid, :op, :u, 'memory.changed', 'memory', 'learner', 1, '{}'::jsonb)"
                ),
                {"oid": outbox_id, "op": user_op.operation_id, "u": USER},
            )
            await session.execute(
                text(
                    "INSERT INTO memory_outbox_deliveries (delivery_id, outbox_id, target, "
                    "idempotency_key) VALUES (:did, :oid, 'user_notification', :ik)"
                ),
                {"did": uuid4(), "oid": outbox_id, "ik": f"dlv-{uuid4().hex[:12]}"},
            )
            await session.execute(
                text(
                    "INSERT INTO memory_internal_event_log (event_log_id, outbox_id, event_type, "
                    "idempotency_key, user_id, payload) "
                    "VALUES (:eid, :oid, 'memory.changed', :ik, :u, '{}'::jsonb)"
                ),
                {"eid": uuid4(), "oid": outbox_id, "ik": f"log-{uuid4().hex[:12]}", "u": USER},
            )
            await session.execute(
                text(
                    "INSERT INTO memory_user_notifications (notification_id, user_id, event_type, "
                    "title, body, aggregate_type, aggregate_id, source_outbox_id) "
                    "VALUES (:nid, :u, 'memory.changed', 't', 'b', 'memory', 'learner', :oid)"
                ),
                {"nid": uuid4(), "u": USER, "oid": outbox_id},
            )
            await session.execute(
                text(
                    "INSERT INTO memory_llm_call_metrics (call_id, operation_id, user_hash, "
                    "model_name, prompt_version, schema_name, status) "
                    "VALUES (:cid, :op, :uh, 'm', 'p', 's', 'ok')"
                ),
                {
                    "cid": uuid4(),
                    "op": user_op.operation_id,
                    "uh": user_privacy_hash(settings.privacy_hmac_key, str(USER)),
                },
            )
            grant_id = uuid4()
            await session.execute(
                text(
                    "INSERT INTO memory_break_glass_grants (grant_id, admin_user_id, "
                    "target_user_id, reason, scopes, expires_at) "
                    "VALUES (:gid, :admin, :u, '排查', '{memory:read}', now() + interval '1 hour')"
                ),
                {"gid": grant_id, "admin": ADMIN, "u": USER},
            )
            await session.execute(
                text(
                    "INSERT INTO memory_break_glass_audit (audit_id, grant_id, admin_user_id, "
                    "target_user_id, action, resource_type, trace_id) "
                    "VALUES (:aid, :gid, :admin, :u, 'read_body', 'route', :tid)"
                ),
                {"aid": uuid4(), "gid": grant_id, "admin": ADMIN, "u": USER, "tid": uuid4().hex},
            )
            if running_op is not None:
                await session.execute(
                    text(
                        "UPDATE memory_operations SET status = 'running', "
                        "lease_expires_at = now() + interval '5 minutes' "
                        "WHERE operation_id = :id"
                    ),
                    {"id": running_op.operation_id},
                )
    return {
        "user_op": user_op.operation_id,
        "running_op": running_op.operation_id if running_op else UUID(int=0),
    }


def _markdown_user_dir(settings: Settings, user_id: UUID) -> Path:
    return Path(settings.memory_storage_root) / "users" / str(user_id)[:2] / str(user_id)


async def _count_user_rows(session: AsyncSession, user_id: UUID) -> dict[str, int]:
    tables = {
        "account_identity_mappings": "internal_user_id",
        "memory_documents": "user_id",
        "memory_commits": "user_id",
        "memory_index_entries": "user_id",
        "memory_review_candidates": "user_id",
        "memory_deleted_evidence_suppressions": "user_id",
        "memory_graph_links": "user_id",
        "graph_user_states": "user_id",
        "graph_user_node_activity": "user_id",
        "graph_activity_seen_events": "user_id",
        "graph_state_audit": "user_id",
        "source_deletions": "user_id",
        "memory_outbox": "user_id",
        "memory_internal_event_log": "user_id",
        "memory_user_notifications": "user_id",
        "memory_operations": "user_id",
    }
    counts: dict[str, int] = {}
    for table, column in tables.items():
        result = await session.execute(
            text(f"SELECT COUNT(*) FROM {table} WHERE {column} = :u"), {"u": user_id}
        )
        counts[table] = int(result.scalar_one())
    for table, column in {
        "memory_break_glass_grants": "target_user_id",
        "memory_break_glass_audit": "target_user_id",
    }.items():
        result = await session.execute(
            text(f"SELECT COUNT(*) FROM {table} WHERE {column} = :u"), {"u": user_id}
        )
        counts[table] = int(result.scalar_one())
    return counts


async def test_purge_account_memory_full_flow(
    settings: Settings,
    session_factory: async_sessionmaker[AsyncSession],
    runtime_context: MemoryRuntimeContext,
) -> None:
    """§13.16 全流程：逐表删除 + break-glass 压缩 + Markdown/Checkpoint 清理 +
    manifest/ledger 推进 + 完成审计。"""
    ids = await _seed_user_data(session_factory, settings)
    user_dir = _markdown_user_dir(settings, USER)
    user_dir.mkdir(parents=True)
    (user_dir / "current").mkdir()
    (user_dir / "current" / "learner.md").write_text("content", encoding="utf-8")

    deletion_id = uuid4()
    async with session_factory() as session:
        async with session.begin():
            await deletion_repo.insert_manifest(
                session,
                account_deletion_id=deletion_id,
                user_hash=user_privacy_hash(settings.privacy_hmac_key, str(USER)),
                user_hash_key_version=settings.privacy_hmac_key_version,
                requested_at=datetime.now(UTC),
                backup_retention_until=datetime.now(UTC),
            )

    purge_op = make_operation(
        user_id=USER,
        actor_type="system",
        input_kind="maintenance",
        operation_type="purge_account_memory",
        priority=0,
        payload=MaintenanceCommand(kind="purge_account_memory", target_user_id=USER),
    )
    await persist_operation(session_factory, purge_op)

    saver = _FakeSaver()
    context = dataclasses.replace(
        runtime_context, checkpoint_cleanup=CheckpointCleanupAdapter(saver=saver)
    )
    result = await LocalLangGraphRunner(context=context).run(purge_op)
    assert result.status == "succeeded"

    async with session_factory() as session:
        counts = await _count_user_rows(session, USER)
        assert counts == {k: 0 for k in counts}, f"仍有残留: {counts}"
        # llm 指标按 user_hash 删除
        llm = await session.execute(
            text("SELECT COUNT(*) FROM memory_llm_call_metrics WHERE user_hash = :uh"),
            {"uh": user_privacy_hash(settings.privacy_hmac_key, str(USER))},
        )
        assert int(llm.scalar_one()) == 0

        manifest = await deletion_repo.get_manifest_by_user_hash(
            session, user_hash=user_privacy_hash(settings.privacy_hmac_key, str(USER))
        )
        assert manifest is not None
        assert manifest["status"] == "completed"
        assert manifest["purge_completed_at"] is not None
        assert len(manifest["completion_proof_checksum"]) == 64

        ledger = await deletion_repo.list_ledger_entries(session)
        assert len(ledger) == 1
        assert ledger[0]["status"] == "completed"
        assert ledger[0]["completion_proof_checksum"] == manifest["completion_proof_checksum"]

        audits = await session.execute(
            text(
                "SELECT action FROM memory_privacy_audit_records "
                "WHERE user_hash = :uh ORDER BY action"
            ),
            {"uh": user_privacy_hash(settings.privacy_hmac_key, str(USER))},
        )
        actions = [row[0] for row in audits.all()]
    assert "account_memory_purged" in actions
    assert "break_glass.grant" in actions
    assert "break_glass.read_body" in actions

    assert not user_dir.exists()
    assert set(saver.deleted) == {
        thread_id_for_operation(ids["user_op"]),
        thread_id_for_operation(purge_op.operation_id),
    }


async def test_purge_waits_for_running_operations(
    settings: Settings,
    session_factory: async_sessionmaker[AsyncSession],
    runtime_context: MemoryRuntimeContext,
) -> None:
    """§21.3 步骤 3：运行中的用户任务未退出时 purge 抛出可重试错误，数据不动。"""
    ids = await _seed_user_data(session_factory, settings, with_running_op=True)
    async with session_factory() as session:
        async with session.begin():
            await deletion_repo.insert_manifest(
                session,
                account_deletion_id=uuid4(),
                user_hash=user_privacy_hash(settings.privacy_hmac_key, str(USER)),
                user_hash_key_version=settings.privacy_hmac_key_version,
                requested_at=datetime.now(UTC),
                backup_retention_until=datetime.now(UTC),
            )

    purge_op = make_operation(
        user_id=USER,
        actor_type="system",
        input_kind="maintenance",
        operation_type="purge_account_memory",
        priority=0,
        payload=MaintenanceCommand(kind="purge_account_memory", target_user_id=USER),
    )
    await persist_operation(session_factory, purge_op)

    with pytest.raises(AccountPurgeNotDrainedError):
        await LocalLangGraphRunner(context=runtime_context).run(purge_op)

    async with session_factory() as session:
        counts = await _count_user_rows(session, USER)
        assert counts["memory_documents"] == 1  # 数据未被删除
        assert counts["memory_operations"] == 3  # user_op + running_op + purge_op
        manifest = await deletion_repo.get_manifest_by_user_hash(
            session,
            user_hash=user_privacy_hash(settings.privacy_hmac_key, str(USER)),
        )
        assert manifest is not None
        assert manifest["status"] == "requested"  # 未推进

    # 运行中任务结束后 purge 成功
    async with session_factory() as session:
        async with session.begin():
            await session.execute(
                text(
                    "UPDATE memory_operations SET status = 'succeeded', "
                    "completed_at = now(), locked_by = NULL, lease_expires_at = NULL "
                    "WHERE operation_id = :id"
                ),
                {"id": ids["running_op"]},
            )
    result = await LocalLangGraphRunner(context=runtime_context).run(purge_op)
    assert result.status == "succeeded"
    async with session_factory() as session:
        counts = await _count_user_rows(session, USER)
        assert counts == {k: 0 for k in counts}

"""知识总结 Phase 6 队列熔断与 retention 集成测试。"""

from __future__ import annotations

import asyncio
import importlib
import json
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from sqlalchemy.orm import Session

from backend.conversation.persistence import knowledge_summary_generations as generations_repo
from backend.conversation.persistence import knowledge_summary_retention as retention_repo
from backend.conversation.persistence import threads as threads_repo
from backend.conversation.persistence import turns as turns_repo


async def _insert_generation_job(
    session: AsyncSession,
    *,
    user_id: UUID,
    trigger: str,
    generation_id: UUID | None = None,
) -> tuple[UUID, UUID, UUID]:
    """建立最小 Thread/Turn/Generation 外键图，供队列和 retention 测试复用。"""
    now = datetime.now(UTC)
    thread_id = uuid4()
    turn_id = uuid4()
    job_id = generation_id or uuid4()
    checkpoint = f"conv-src-v1:{thread_id}:{turn_id}:test"
    assert await threads_repo.insert_thread(session, thread_id, user_id)
    assert await turns_repo.insert_turn(
        session,
        turn_id=turn_id,
        thread_id=thread_id,
        user_id=user_id,
        client_request_id=f"phase6-{turn_id}",
        request_id=f"request-{turn_id}",
        run_id=f"run-{turn_id}",
        user_message_id=uuid4(),
        expected_thread_version=0,
        graph_thread_id=str(thread_id),
        next_attempt_at=now,
    )
    await session.execute(
        text(
            """
            UPDATE conversation.conversation_turns
            SET status = 'completed', source_checkpoint_id = :checkpoint
            WHERE turn_id = :turn_id
            """
        ),
        {"turn_id": turn_id, "checkpoint": checkpoint},
    )
    assert await generations_repo.insert_generation_job(
        session,
        generation_id=job_id,
        idempotency_key=f"phase6:{job_id}",
        client_request_id=None,
        user_id=user_id,
        thread_id=thread_id,
        turn_id=turn_id,
        source_checkpoint_id=checkpoint,
        trigger=trigger,
        primary_turn_occurred_at=now,
    )
    return job_id, thread_id, turn_id


@pytest.mark.asyncio
async def test_model_call_attempt_migration_backfills_nonempty_legacy_rows(
    conversation_session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """0005 必须为非空旧审计表稳定回填 attempt_no，不能因新唯一约束升级失败。"""
    migration = importlib.import_module(
        "conversation_migrations.versions.0005_knowledge_summary_model_call_attempts"
    )
    table_name = "knowledge_summary_model_calls_legacy"
    generation_id = uuid4()
    other_generation_id = uuid4()
    first_call_id = uuid4()
    second_call_id = uuid4()
    third_call_id = uuid4()
    merge_call_id = uuid4()
    first_created_at = datetime(2026, 8, 18, 8, 0, tzinfo=UTC)
    second_created_at = first_created_at + timedelta(seconds=1)

    async with conversation_session_factory() as session:
        async with session.begin():
            await session.execute(
                text(
                    f"""
                    CREATE TEMPORARY TABLE {table_name} (
                        call_id uuid PRIMARY KEY,
                        generation_id uuid NOT NULL,
                        purpose text NOT NULL,
                        request_hash char(64) NOT NULL,
                        created_at timestamptz NOT NULL,
                        CONSTRAINT knowledge_summary_model_calls_generation_id_purpose_request_key
                            UNIQUE (generation_id, purpose, request_hash)
                    ) ON COMMIT DROP
                    """
                )
            )
            await session.execute(
                text(
                    f"""
                    INSERT INTO {table_name} (
                        call_id, generation_id, purpose, request_hash, created_at
                    ) VALUES
                        (
                            :first_call_id, :generation_id, 'extract', :first_hash,
                            :first_created_at
                        ),
                        (
                            :second_call_id, :generation_id, 'extract', :second_hash,
                            :second_created_at
                        ),
                        (
                            :third_call_id, :generation_id, 'extract', :third_hash,
                            :second_created_at
                        ),
                        (
                            :merge_call_id, :generation_id, 'merge_plan', :merge_hash,
                            :first_created_at
                        )
                    """
                ),
                {
                    "first_call_id": first_call_id,
                    "second_call_id": second_call_id,
                    "third_call_id": third_call_id,
                    "merge_call_id": merge_call_id,
                    "generation_id": generation_id,
                    "first_hash": "a" * 64,
                    "second_hash": "b" * 64,
                    "third_hash": "c" * 64,
                    "merge_hash": "d" * 64,
                    "first_created_at": first_created_at,
                    "second_created_at": second_created_at,
                },
            )

            def run_upgrade(sync_session: Session) -> None:
                connection = sync_session.connection()

                def execute(statement: str) -> None:
                    connection.execute(
                        text(
                            statement.replace(
                                "conversation.knowledge_summary_model_calls", table_name
                            )
                        )
                    )

                monkeypatch.setattr(migration.op, "execute", execute)
                migration.upgrade()

            await session.run_sync(run_upgrade)
            attempts = (
                await session.execute(
                    text(
                        f"""
                        SELECT call_id, purpose, attempt_no
                        FROM {table_name}
                        WHERE generation_id = :generation_id
                        ORDER BY purpose, attempt_no
                        """
                    ),
                    {"generation_id": generation_id},
                )
            ).all()
            equal_timestamp_calls = sorted(
                (second_call_id, third_call_id), key=lambda call_id: call_id.int
            )
            assert attempts == [
                (first_call_id, "extract", 1),
                (equal_timestamp_calls[0], "extract", 2),
                (equal_timestamp_calls[1], "extract", 3),
                (merge_call_id, "merge_plan", 1),
            ]

            with pytest.raises(IntegrityError):
                async with session.begin_nested():
                    await session.execute(
                        text(
                            f"""
                            INSERT INTO {table_name} (
                                call_id, generation_id, purpose, request_hash, created_at,
                                attempt_no
                            ) VALUES (
                                :call_id, :generation_id, 'extract', :request_hash, :created_at, 1
                            )
                            """
                        ),
                        {
                            "call_id": uuid4(),
                            "generation_id": generation_id,
                            "request_hash": "e" * 64,
                            "created_at": second_created_at,
                        },
                    )

            await session.execute(
                text(
                    f"""
                    INSERT INTO {table_name} (
                        call_id, generation_id, purpose, request_hash, created_at
                    ) VALUES (
                        :call_id, :generation_id, 'extract', :request_hash, :created_at
                    )
                    """
                ),
                {
                    "call_id": uuid4(),
                    "generation_id": other_generation_id,
                    "request_hash": "f" * 64,
                    "created_at": second_created_at,
                },
            )
            default_attempt = (
                await session.execute(
                    text(
                        f"""
                        SELECT attempt_no FROM {table_name}
                        WHERE generation_id = :generation_id
                        """
                    ),
                    {"generation_id": other_generation_id},
                )
            ).scalar_one()
            assert default_attempt == 1


@pytest.mark.asyncio
async def test_two_workers_cannot_exceed_global_processing_limit(
    conversation_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """双 Worker 并发领取必须由全局 claim 锁串行化，processing 不得超过 4。"""
    async with conversation_session_factory() as session:
        async with session.begin():
            for _index in range(8):
                await _insert_generation_job(session, user_id=uuid4(), trigger="manual")

    async def claim(worker_id: str) -> list[dict[str, object]]:
        async with conversation_session_factory() as session:
            async with session.begin():
                return await generations_repo.claim_generation_jobs(
                    session,
                    worker_id=worker_id,
                    lease_seconds=60,
                    max_concurrency=4,
                    manual_reserved_slots=1,
                    now=datetime.now(UTC),
                )

    worker_a, worker_b = await asyncio.gather(claim("worker-a"), claim("worker-b"))
    assert sorted((len(worker_a), len(worker_b))) == [0, 4]

    async with conversation_session_factory() as session:
        processing = (
            await session.execute(
                text(
                    """
                    SELECT COUNT(*)
                    FROM conversation.knowledge_summary_generation_jobs
                    WHERE status = 'processing'
                    """
                )
            )
        ).scalar_one()
    assert processing == 4


@pytest.mark.asyncio
async def test_auto_suspension_still_claims_manual_jobs(
    conversation_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """§21.5：暂停自动生成后，manual 仍可领取，auto 必须保留 pending。"""
    async with conversation_session_factory() as session:
        async with session.begin():
            auto_id, _, _ = await _insert_generation_job(session, user_id=uuid4(), trigger="auto")
            manual_id, _, _ = await _insert_generation_job(
                session, user_id=uuid4(), trigger="manual"
            )
            claimed = await generations_repo.claim_generation_jobs(
                session,
                worker_id="phase6-test-worker",
                lease_seconds=60,
                max_concurrency=4,
                manual_reserved_slots=1,
                auto_generation_suspended=True,
            )
            assert [row["generation_id"] for row in claimed] == [manual_id]
            auto_status = (
                await session.execute(
                    text(
                        """
                        SELECT status
                        FROM conversation.knowledge_summary_generation_jobs
                        WHERE generation_id = :generation_id
                        """
                    ),
                    {"generation_id": auto_id},
                )
            ).scalar_one()
            assert auto_status == "pending"


@pytest.mark.asyncio
async def test_retention_scrubs_payloads_after_terminal_retention_window(
    conversation_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """§20.5：终态 Job 的模型响应和 Job payload 按保留期 scrub，而非立刻抹除。"""
    now = datetime(2026, 8, 18, tzinfo=UTC)
    async with conversation_session_factory() as session:
        async with session.begin():
            generation_id, _, _ = await _insert_generation_job(
                session,
                user_id=uuid4(),
                trigger="manual",
            )
            await session.execute(
                text(
                    """
                    UPDATE conversation.knowledge_summary_generation_jobs
                    SET status = 'succeeded',
                        completed_at = :completed_at,
                        input_manifest = CAST(:input_manifest AS jsonb),
                        extraction_result = CAST(:extraction_result AS jsonb),
                        merge_plan_result = CAST(:merge_plan_result AS jsonb)
                    WHERE generation_id = :generation_id
                    """
                ),
                {
                    "generation_id": generation_id,
                    "completed_at": now - timedelta(days=31),
                    "input_manifest": json.dumps(
                        {"schema_version": "knowledge_input_v1", "input_hash": "hash"}
                    ),
                    "extraction_result": json.dumps({"candidates": []}),
                    "merge_plan_result": json.dumps({"plans": []}),
                },
            )
            await generations_repo.insert_model_call(
                session,
                call_id=uuid4(),
                generation_id=generation_id,
                purpose="extract",
                model_name="phase6-test-model",
                prompt_version="knowledge_extract_v1",
                schema_version="knowledge_extract_schema_v1",
                request_hash="f" * 64,
                response_payload={"candidates": [{"text": "保留期内模型审计正文"}]},
                input_tokens=10,
                output_tokens=5,
                latency_ms=1,
                status="succeeded",
            )
            scrubbed_calls = await retention_repo.scrub_model_call_payloads(
                session,
                cutoff=now - timedelta(days=14),
                batch_size=10,
            )
            scrubbed_jobs = await retention_repo.scrub_generation_payloads(
                session,
                cutoff=now - timedelta(days=30),
                batch_size=10,
            )
            assert scrubbed_calls == 1
            assert scrubbed_jobs == 1
            model_payload, payload_scrubbed_at = (
                await session.execute(
                    text(
                        """
                        SELECT response_payload, payload_scrubbed_at
                        FROM conversation.knowledge_summary_model_calls
                        WHERE generation_id = :generation_id
                        """
                    ),
                    {"generation_id": generation_id},
                )
            ).one()
            assert model_payload == {"scrubbed": True}
            assert payload_scrubbed_at is not None
            input_manifest, extraction_result, merge_plan_result = (
                await session.execute(
                    text(
                        """
                        SELECT input_manifest, extraction_result, merge_plan_result
                        FROM conversation.knowledge_summary_generation_jobs
                        WHERE generation_id = :generation_id
                        """
                    ),
                    {"generation_id": generation_id},
                )
            ).one()
            assert input_manifest["input_hash"] == "hash"
            assert extraction_result == {"scrubbed": True}
            assert merge_plan_result == {"scrubbed": True}


@pytest.mark.asyncio
async def test_expired_processing_leases_are_reclaimed_when_slots_appear_full(
    conversation_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """过期 processing 不应占用并发槽，满槽崩溃后的 Job 必须可重新领取。"""
    now = datetime(2026, 8, 19, 12, 0, tzinfo=UTC)
    async with conversation_session_factory() as session:
        async with session.begin():
            generation_ids: list[UUID] = []
            for _index in range(4):
                generation_id, _, _ = await _insert_generation_job(
                    session, user_id=uuid4(), trigger="manual"
                )
                generation_ids.append(generation_id)
                await session.execute(
                    text(
                        """
                        UPDATE conversation.knowledge_summary_generation_jobs
                        SET status = 'processing', lease_owner = 'dead-worker',
                            lease_generation = 1, lease_expires_at = :expired
                        WHERE generation_id = :generation_id
                        """
                    ),
                    {"generation_id": generation_id, "expired": now - timedelta(seconds=1)},
                )
            claimed = await generations_repo.claim_generation_jobs(
                session,
                worker_id="recovery-worker",
                lease_seconds=60,
                max_concurrency=4,
                manual_reserved_slots=1,
                now=now,
            )
            assert {row["generation_id"] for row in claimed} == set(generation_ids)
            assert all(row["status"] == "processing" for row in claimed)


@pytest.mark.asyncio
async def test_model_call_attempts_preserve_failed_retries_and_success_cache(
    conversation_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """同一 request hash 的失败重试和后续成功审计必须全部保留。"""
    async with conversation_session_factory() as session:
        async with session.begin():
            generation_id, _, _ = await _insert_generation_job(
                session, user_id=uuid4(), trigger="manual"
            )
            common = {
                "generation_id": generation_id,
                "purpose": "extract",
                "model_name": "phase6-test-model",
                "prompt_version": "knowledge_extract_v1",
                "schema_version": "knowledge_extract_schema_v1",
                "request_hash": "a" * 64,
                "input_tokens": None,
                "output_tokens": None,
                "latency_ms": 1,
            }
            for error_code in ("FIRST_FAILURE", "SECOND_FAILURE"):
                await generations_repo.insert_model_call(
                    session,
                    call_id=uuid4(),
                    response_payload=None,
                    status="failed",
                    error_code=error_code,
                    **common,
                )
            await generations_repo.insert_model_call(
                session,
                call_id=uuid4(),
                response_payload={"candidates": []},
                status="succeeded",
                **common,
            )
            failed_count = await generations_repo.count_failed_model_calls(
                session, generation_id=generation_id, purpose="extract"
            )
            cached = await generations_repo.get_cached_model_call(
                session,
                generation_id=generation_id,
                purpose="extract",
                request_hash="a" * 64,
            )
            attempts = (
                (
                    await session.execute(
                        text(
                            """
                        SELECT attempt_no, status, error_code
                        FROM conversation.knowledge_summary_model_calls
                        WHERE generation_id = :generation_id
                        ORDER BY attempt_no
                        """
                        ),
                        {"generation_id": generation_id},
                    )
                )
                .mappings()
                .all()
            )
            assert failed_count == 2
            assert cached is not None and cached["status"] == "succeeded"
            assert [(row["attempt_no"], row["status"]) for row in attempts] == [
                (1, "failed"),
                (2, "failed"),
                (3, "succeeded"),
            ]

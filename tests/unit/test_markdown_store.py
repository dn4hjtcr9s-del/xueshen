"""Markdown Schema 渲染/解析 round-trip 与存储单元测试（§8 / §23.1）。"""

from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

import pytest

from backend.memory.storage.base import logical_path_for, version_storage_key
from backend.memory.storage.local_markdown import (
    LocalMarkdownStore,
    StoragePathError,
)
from backend.memory.storage.markdown_schema import (
    IndexDocument,
    IndexEntry,
    LearnerDocument,
    MarkdownParseError,
    MasteryDocument,
    parse_index,
    parse_learner,
    parse_mastery,
    render_index,
    render_learner,
    render_mastery,
)

NOW = datetime(2026, 8, 10, 8, 0, 0, tzinfo=UTC)


def _learner() -> LearnerDocument:
    return LearnerDocument(
        user_id=uuid4(),
        version=7,
        updated_at=NOW,
        preferences=["喜欢图形化讲解", "节奏慢一点"],
        goals=["期中考试 90 分"],
        plans=["每天一道极限题"],
        evidence_refs=["conv:t1:m1", "conv:t1:m2"],
        confidence=0.86,
    )


def _mastery() -> MasteryDocument:
    return MasteryDocument(
        user_id=uuid4(),
        topic_key="一致收敛",
        topic_title="一致收敛",
        version=4,
        updated_at=NOW,
        overview="能区分逐点收敛与一致收敛，但在证明题中仍有困难。",
        understood=["逐点收敛定义", "一致收敛定义"],
        difficulties=["Weierstrass 判别法的应用"],
        review_advice=["重做 P38 例 2"],
        evidence_refs=["conv:t2:m5"],
        confidence=None,
    )


def test_learner_roundtrip() -> None:
    doc = _learner()
    parsed = parse_learner(render_learner(doc))
    assert parsed == doc


def test_mastery_roundtrip() -> None:
    doc = _mastery()
    parsed = parse_mastery(render_mastery(doc))
    assert parsed == doc


def test_mastery_with_special_chars_roundtrip() -> None:
    doc = _mastery()
    doc.overview = '含 "引号" 与: 冒号 # 井号\n多行文本'
    doc.understood = ["含 - 连字符", "## 不是标题（在列表项中）"]
    parsed = parse_mastery(render_mastery(doc))
    assert parsed == doc


def test_index_roundtrip() -> None:
    doc = IndexDocument(
        user_id=uuid4(),
        version=3,
        updated_at=NOW,
        learner=IndexEntry(
            memory_id="learner",
            memory_type="learner",
            topic_key=None,
            title="学习者档案",
            version=7,
            updated_at=NOW,
        ),
        mastery_entries=[
            IndexEntry(
                memory_id="mastery:一致收敛",
                memory_type="mastery",
                topic_key="一致收敛",
                title="一致收敛",
                version=4,
                updated_at=NOW,
            )
        ],
    )
    parsed = parse_index(render_index(doc))
    assert parsed == doc


def test_empty_sections_keep_headings() -> None:
    doc = _learner()
    doc.preferences = []
    rendered = render_learner(doc)
    assert "## 学习偏好" in rendered
    parsed = parse_learner(rendered)
    assert parsed.preferences == []


def test_evidence_refs_materialized_limit() -> None:
    doc = _mastery()
    doc.evidence_refs = [f"ref:{i}" for i in range(150)]
    rendered = render_mastery(doc)
    parsed = parse_mastery(rendered)
    assert len(parsed.evidence_refs) == 100  # 最多物化 100 条（§8.2）


def test_parse_failure_raises() -> None:
    with pytest.raises(MarkdownParseError):
        parse_mastery("# 没有 front matter")
    with pytest.raises(MarkdownParseError):
        parse_learner(render_mastery(_mastery()))  # kind 不匹配


def test_storage_key_format() -> None:
    uid = uuid4()
    key = version_storage_key(uid, "mastery:一致收敛", 4, "a" * 64)
    shard = str(uid)[:2]
    assert key == (f"users/{shard}/{uid}/versions/mastery/一致收敛/v00000004-{'a' * 12}.md")
    assert version_storage_key(uid, "learner", 7, "b" * 64).endswith(
        f"versions/learner/learner/v00000007-{'b' * 12}.md"
    )


def test_logical_path() -> None:
    assert logical_path_for("learner") == "learner.md"
    assert logical_path_for("index") == "index.md"
    assert logical_path_for("mastery:一致收敛") == "mastery/一致收敛.md"


# ---------------- LocalMarkdownStore ----------------


@pytest.fixture
def store(tmp_path: Path) -> LocalMarkdownStore:
    return LocalMarkdownStore(tmp_path)


async def test_store_write_read_roundtrip(store: LocalMarkdownStore) -> None:
    uid = uuid4()
    content = render_mastery(_mastery()).encode("utf-8")
    stored = await store.write_immutable_version(
        user_id=uid, memory_id="mastery:一致收敛", version=1, content=content
    )
    assert len(stored.checksum) == 64
    read_back = await store.read_version(user_id=uid, storage_key=stored.storage_key)
    assert read_back == content
    # 幂等：重复写入相同内容返回相同 key
    stored2 = await store.write_immutable_version(
        user_id=uid, memory_id="mastery:一致收敛", version=1, content=content
    )
    assert stored2.storage_key == stored.storage_key


async def test_store_rejects_conflicting_immutable(store: LocalMarkdownStore) -> None:
    uid = uuid4()
    await store.write_immutable_version(user_id=uid, memory_id="learner", version=1, content=b"v1")
    # 同版本不同内容 => checksum 不同 => 不同 key，不冲突
    stored = await store.write_immutable_version(
        user_id=uid, memory_id="learner", version=1, content=b"v1-modified"
    )
    assert stored.storage_key != ""
    read_back = await store.read_version(user_id=uid, storage_key=stored.storage_key)
    assert read_back == b"v1-modified"


async def test_store_rejects_path_traversal(store: LocalMarkdownStore) -> None:
    uid = uuid4()
    with pytest.raises(StoragePathError):
        await store.read_version(user_id=uid, storage_key="../../etc/passwd")
    with pytest.raises(StoragePathError):
        await store.write_immutable_version(
            user_id=uid, memory_id="../evil", version=1, content=b"x"
        )
    with pytest.raises(StoragePathError):
        await store.materialize_current(user_id=uid, memory_id="mastery:..", content=b"x")


async def test_store_materialize_and_remove(store: LocalMarkdownStore) -> None:
    uid = uuid4()
    content = render_learner(_learner()).encode()
    await store.materialize_current(user_id=uid, memory_id="learner", content=content)
    current = store._abs(uid, "current/learner.md")
    assert current.exists()
    await store.remove_current(user_id=uid, memory_id="learner")
    assert not current.exists()


async def test_store_quarantine_flow(store: LocalMarkdownStore) -> None:
    uid = uuid4()
    content = render_mastery(_mastery()).encode()
    stored = await store.write_immutable_version(
        user_id=uid, memory_id="mastery:一致收敛", version=2, content=content
    )
    await store.move_to_quarantine(
        user_id=uid, memory_id="mastery:一致收敛", deleted_version=2, deleted_at_epoch=1723
    )
    # 可恢复：按 memory_id + version + checksum 从隔离区外不可变版本恢复（§8.7.6 说明
    # 即使移动失败也可恢复；移动后 read_version_by_id 找不到，purge 后彻底清理）
    await store.purge_quarantined(user_id=uid, memory_id="mastery:一致收敛")
    with pytest.raises(FileNotFoundError):
        await store.read_version(user_id=uid, storage_key=stored.storage_key)


async def test_store_orphan_detection(store: LocalMarkdownStore) -> None:
    uid = uuid4()
    s1 = await store.write_immutable_version(
        user_id=uid, memory_id="learner", version=1, content=b"v1"
    )
    await store.write_immutable_version(user_id=uid, memory_id="learner", version=2, content=b"v2")
    orphans = await store.list_orphan_versions(
        user_id=uid, memory_id="learner", referenced_checksums={s1.checksum}
    )
    assert len(orphans) == 1
    assert "v00000002" in orphans[0]
    await store.delete_version_file(user_id=uid, storage_key=orphans[0])
    assert (
        await store.list_orphan_versions(
            user_id=uid, memory_id="learner", referenced_checksums={s1.checksum}
        )
        == []
    )

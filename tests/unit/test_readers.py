"""Reader 边界与删除抑制单元测试（§6.1 / §17.3 / §17.4 / 裁决 A）。"""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID, uuid4

import pytest
from pydantic import ValidationError

from backend.memory.contracts.errors import SourceDeletedError, SourceTooLargeError
from backend.memory.contracts.evidence import (
    SourceBundle,
    SourceDeletedEvent,
    SourceItem,
)
from backend.memory.readers.filtering import filter_bundle_with_deletions
from backend.memory.readers.testing import (
    FakeActivityReader,
    FakeConversationReader,
    FakeSourceDeletionHandler,
)

USER = UUID("00000000-0000-4000-8000-000000000002")
NOW = datetime(2026, 8, 11, 8, 0, 0, tzinfo=UTC)


def item(ref: str, content: str = "内容", **metadata: object) -> SourceItem:
    return SourceItem(
        source_ref=ref, role="user", content=content, occurred_at=NOW, metadata=dict(metadata)
    )


def deleted_row(ref: str, version: str | None = None) -> dict[str, object]:
    return {"source_ref": ref, "source_version": version, "deleted_at": NOW}


async def test_fake_conversation_reader_filters_by_message_ids() -> None:
    reader = FakeConversationReader()
    reader.add_message("t1", item("m1", "我学会了配方法"))
    reader.add_message("t1", item("m2", "谢谢"))
    reader.add_message("t2", item("m3", "其他线程"))

    bundle = await reader.read(
        user_id=USER, thread_id="t1", checkpoint_id=None, message_ids=["m1", "mX"]
    )
    assert [i.source_ref for i in bundle.items] == ["m1"]
    assert bundle.total_utf8_bytes == len("我学会了配方法".encode())
    assert reader.calls[0]["checkpoint_id"] is None


async def test_fake_activity_reader_and_bundle_bytes_dedup() -> None:
    reader = FakeActivityReader()
    reader.add_activity(item("a1", "重复内容"))
    reader.add_activity(item("a2", "重复内容"))
    reader.add_activity(item("a3", "不同内容"))

    bundle = await reader.read(
        user_id=USER,
        activity_type="exercise_attempt",
        activity_ids=["a1", "a2", "a3"],
        content_ref=None,
    )
    # 去重后字节：重复内容只计一次（§17.4 裁决 21）
    expected = len("重复内容".encode()) + len("不同内容".encode())
    assert bundle.total_utf8_bytes == expected


def test_source_bundle_too_large_raises() -> None:
    # 内容必须互不相同，否则去重后不超限（§17.4 去重字节语义）
    items = [item(f"r{i}", f"{i}" + "x" * 19_999) for i in range(5)]
    with pytest.raises(SourceTooLargeError):
        SourceBundle.from_items(items)


def test_source_item_metadata_limits() -> None:
    with pytest.raises(ValidationError, match="50"):
        item("r1", **{f"k{i}": "v" for i in range(51)})
    with pytest.raises(ValidationError, match="4096"):
        item("r2", big="y" * 5000)
    with pytest.raises(ValidationError, match="JSON"):
        item("r3", bad=object())


async def test_fake_deletion_handler_idempotent() -> None:
    handler = FakeSourceDeletionHandler()
    event = SourceDeletedEvent(
        event_id=uuid4(),
        source_system="conversation",
        source_ref="m1",
        source_version=None,
        deleted_at=NOW,
    )
    assert await handler.handle(user_id=USER, event=event) == "recorded"
    assert await handler.handle(user_id=USER, event=event) == "duplicate"
    # 不同事件 ID 是新的删除事实
    event2 = event.model_copy(update={"event_id": uuid4()})
    assert await handler.handle(user_id=USER, event=event2) == "recorded"
    # 不同用户互不影响
    other = UUID("00000000-0000-4000-8000-000000000099")
    assert await handler.handle(user_id=other, event=event) == "recorded"


def test_filter_partial_deletion_returns_kept_with_deleted_refs() -> None:
    bundle = SourceBundle.from_items([item("m1", "保留"), item("m2", "删除")])
    result = filter_bundle_with_deletions(bundle, [deleted_row("m2")])
    assert [i.source_ref for i in result.items] == ["m1"]
    assert result.deleted_refs == ["m2"]
    assert result.total_utf8_bytes == len("保留".encode())


def test_filter_full_deletion_raises_source_deleted() -> None:
    bundle = SourceBundle.from_items([item("m1"), item("m2")])
    with pytest.raises(SourceDeletedError):
        filter_bundle_with_deletions(bundle, [deleted_row("m1"), deleted_row("m2")])


def test_filter_version_semantics() -> None:
    bundle = SourceBundle.from_items(
        [item("m1", "v1", source_version="1"), item("m2", "v2", source_version="2")]
    )
    # source_version NULL 匹配全部版本
    with pytest.raises(SourceDeletedError):
        filter_bundle_with_deletions(bundle, [deleted_row("m1"), deleted_row("m2")])
    # 指定版本只删除匹配版本
    result = filter_bundle_with_deletions(bundle, [deleted_row("m1", version="1")])
    assert [i.source_ref for i in result.items] == ["m2"]
    # 版本不匹配不删除
    result = filter_bundle_with_deletions(bundle, [deleted_row("m1", version="9")])
    assert len(result.items) == 2


def test_filter_no_deletions_passthrough() -> None:
    bundle = SourceBundle.from_items([item("m1")])
    assert filter_bundle_with_deletions(bundle, []) is bundle

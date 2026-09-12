"""Rollout 段物理删除的**唯一**入口集合（review-2 新发现 6 / 15）。

删除一条 rollout 段的**物理载体**有两份，缺一不可：

1. **对象镜像**（``objects/...`` 或远端 bucket）——按 manifest 的 ``object_key`` 删；
2. **本地热缓存段**（``{root}/threads/...``）——recorder 无论哪种后端都会在本地写它。

review-2 抓到两个"只删一半"的缺口，都由本模块集中修掉：

- **新发现 6**：``object_key IS NULL`` 的未封存段（``status='open'``）没有任何 key 可推导，
  旧的删除路径把它们整段跳过（连 tombstone 都不落）→ 用户原文留在磁盘上；
- **新发现 15**：kodo 后端只删云端对象，本地 ``conversation_rollout_root`` 的热段无人清理。

因此三条删除入口——delete_thread Job、CLI ``delete-thread-rollouts``、CLI
``retention-scan --apply``——都必须经由本模块的两个**统一助手**：段级的
:func:`delete_rollout_payloads`（对象 + 逐 key 热段）与 thread 级的
:func:`delete_thread_rollout_payloads`（再加按 ``<thread_id>`` 目录清扫）。

- 有 ``object_key`` 的段走 :func:`delete_rollout_objects` + :func:`delete_rollout_hot_segments`；
- **thread 级删除**再用 :func:`sweep_thread_hot_segments` 按 ``<thread_id>`` 目录扫一遍，
  覆盖未封存段、kodo 本地缓存、以及没有 manifest 行的孤儿热文件。

失败语义：任何一项失败都进 :attr:`RolloutDeletionReport.failures`，调用方**不得**据此落
tombstone（与 I-6 "缺对象存储不落 tombstone" 同一原则）。对象删除最终一致，重跑即幂等。
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from typing import Any
from uuid import UUID

from backend.conversation.rollout.object_store import (
    delete_hot_segment,
    delete_hot_thread_segments,
)


@dataclass(slots=True)
class RolloutDeletionReport:
    """物理删除结果；``failures`` 非空即"没删干净"，调用方必须拒绝落 tombstone。"""

    deleted_objects: int = 0
    deleted_hot_segments: int = 0
    failures: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.failures

    def merge(self, other: RolloutDeletionReport) -> RolloutDeletionReport:
        """把子步骤的结果并进本报告（保持"任一失败即整体失败"）。"""
        self.deleted_objects += other.deleted_objects
        self.deleted_hot_segments += other.deleted_hot_segments
        self.failures.extend(other.failures)
        return self

    def summary(self) -> str:
        """一行中文摘要（CLI 与日志共用，避免两处措辞不一致）。"""
        text = (
            f"删除对象 {self.deleted_objects} 个、热缓存段 {self.deleted_hot_segments} 个，"
            f"失败 {len(self.failures)} 项"
        )
        if self.failures:
            text += "：" + "；".join(self.failures)
        return text


async def delete_rollout_objects(
    *, object_store: Any, object_keys: Sequence[str]
) -> RolloutDeletionReport:
    """删除已封存段的**对象镜像**；幂等（对象不存在视为成功）。"""
    report = RolloutDeletionReport()
    for key in object_keys:
        try:
            await object_store.delete(key=key)
        except Exception as exc:  # 单个失败不阻断其余 key：尽量多删数据
            report.failures.append(f"对象删除失败 {key}（{exc}）")
            continue
        report.deleted_objects += 1
    return report


async def delete_rollout_hot_segments(
    *, object_store: Any, object_keys: Sequence[str]
) -> RolloutDeletionReport:
    """删除与 ``object_keys`` 同构的**本地热缓存段**（逐段精确清理）。

    实现不具备热缓存能力时记为失败而不是静默 no-op：调用方会把"没有异常"当成
    "本机已无残留"，静默跳过正是 review-2 新发现 6/15 的病根。
    """
    report = RolloutDeletionReport()
    for key in object_keys:
        try:
            removed = await delete_hot_segment(object_store=object_store, key=key)
        except Exception as exc:
            report.failures.append(f"热段删除失败 {key}（{exc}）")
            continue
        if not removed:
            report.failures.append(
                f"热段删除能力缺失（object_store 未实现 HotSegmentDeleter）：{key}"
            )
            continue
        report.deleted_hot_segments += 1
    return report


async def sweep_thread_hot_segments(
    *, object_store: Any, thread_id: UUID | str
) -> RolloutDeletionReport:
    """按 ``<thread_id>`` 目录清扫本地热段（未封存段 / kodo 本地缓存 / 孤儿文件）。

    **前置条件**：调用方已确认该 thread 没有活动 Turn（删除流程的 R4 守卫），
    否则可能删掉正在写入的段。retention 是段级删除，**不得**调用本函数。
    """
    report = RolloutDeletionReport()
    try:
        removed = await delete_hot_thread_segments(object_store=object_store, thread_id=thread_id)
    except Exception as exc:
        report.failures.append(f"热段目录清扫失败 thread={thread_id}（{exc}）")
        return report
    if removed is None:
        report.failures.append(
            f"热段目录清扫能力缺失（object_store 未实现 HotSegmentRootDeleter）：thread={thread_id}"
        )
        return report
    report.deleted_hot_segments += removed
    return report


async def delete_rollout_payloads(
    *,
    object_store: Any,
    object_keys: Iterable[str] = (),
) -> RolloutDeletionReport:
    """**段级**统一删除助手：对象镜像 + 同构热段，逐个 key 精确清理。

    ``retention-scan`` 走这条路径：它只清理过期的**单个段**，绝不能按 thread 清扫目录
    （那会连带删掉同 thread 内未过期的段）。
    """
    keys = [str(key) for key in object_keys]
    report = RolloutDeletionReport()
    report.merge(await delete_rollout_objects(object_store=object_store, object_keys=keys))
    # 对象删除失败**不**跳过热段清理：热段是磁盘上的用户正文，优先级更高。
    report.merge(await delete_rollout_hot_segments(object_store=object_store, object_keys=keys))
    return report


async def delete_thread_rollout_payloads(
    *,
    object_store: Any,
    object_keys: Iterable[str] = (),
    thread_id: UUID | str,
) -> RolloutDeletionReport:
    """**thread 级**统一删除助手：段级载荷 + 按 thread 目录清扫。

    delete_thread Job 与 CLI ``delete-thread-rollouts`` 走这条路径。清扫是必需的：
    ``object_key IS NULL`` 的未封存段没有 key 可推导（新发现 6），kodo 模式的本地热缓存
    也不在任何 key 上（新发现 15）。

    **前置条件**：调用方已确认该 thread 没有活动 Turn（删除流程的 R4 守卫）。
    """
    report = await delete_rollout_payloads(object_store=object_store, object_keys=object_keys)
    report.merge(await sweep_thread_hot_segments(object_store=object_store, thread_id=thread_id))
    return report


__all__ = [
    "RolloutDeletionReport",
    "delete_rollout_hot_segments",
    "delete_rollout_objects",
    "delete_rollout_payloads",
    "delete_thread_rollout_payloads",
    "sweep_thread_hot_segments",
]

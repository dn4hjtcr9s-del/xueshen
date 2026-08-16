"""Study 任务状态机纯函数（§12.3/D11，无 I/O）。

状态转移矩阵冻结在 contracts/domain.py TASK_TRANSITIONS；cancelled 只由
revision/计划生命周期产生，任意任务操作拒绝。launch 是带学习入口的 start
便利操作（§12.3：pending→in_progress 或复用 in_progress）。
"""

from __future__ import annotations

from backend.study.contracts.domain import TASK_TRANSITIONS
from backend.study.contracts.errors import StudyInvalidTaskTransitionError


def apply_transition(current: str, action: str) -> str:
    """按冻结矩阵返回下一状态；非法转移抛 StudyInvalidTaskTransitionError。"""
    next_status = TASK_TRANSITIONS.get(current, {}).get(action)
    if next_status is None:
        raise StudyInvalidTaskTransitionError(
            f"任务状态 {current} 不允许操作 {action}（§12.3 转移矩阵）"
        )
    return next_status


def launch_transition(current: str) -> str:
    """launch 语义（§12.3）：pending→in_progress；in_progress 保持（复用 Session）。"""
    if current == "pending":
        return "in_progress"
    if current == "in_progress":
        return "in_progress"
    raise StudyInvalidTaskTransitionError(
        f"任务状态 {current} 不允许 launch（§12.3：仅 pending/in_progress）"
    )


def lifecycle_cancel(current: str) -> str | None:
    """revision 移除/计划归档时的取消转移（§12.3）：可为 pending/in_progress/skipped。"""
    if current in ("pending", "in_progress", "skipped"):
        return "cancelled"
    return None

"""Study 进度计算纯函数（方案 §13.1/D12，无 I/O）。

- 双口径：按任务数 / 按预计工作量（normalized estimated_minutes）；
- cancelled 不计入分母；skipped 计入未完成；
- 分母为零 → 0；否则 ROUND_HALF_UP 四舍五入到整数并限制在 0–100；
- 进度算法由后端确定性处理，模型不得输出百分比。
"""

from __future__ import annotations

from decimal import ROUND_HALF_UP, Decimal


def percent(numerator: int, denominator: int) -> int:
    """ROUND_HALF_UP 百分比（§13.1：分母为零返回 0，结果限制 0–100）。"""
    if denominator <= 0:
        return 0
    value = int(
        (Decimal(numerator) / Decimal(denominator) * 100).quantize(
            Decimal("1"), rounding=ROUND_HALF_UP
        )
    )
    return max(0, min(100, value))


def task_progress(completed_count: int, total_count: int) -> int:
    """按任务数进度 = completed ÷ 非 cancelled（§13.1）。"""
    return percent(completed_count, total_count)


def workload_progress(completed_minutes: int, total_minutes: int) -> int:
    """按预计工作量进度 = completed 任务 normalized minutes 之和 ÷ 分母（§13.1）。"""
    return percent(completed_minutes, total_minutes)


def dual_progress(
    completed_count: int,
    total_count: int,
    completed_minutes: int,
    total_minutes: int,
) -> tuple[int, int]:
    """一次计算双口径，返回 (task_progress_percent, workload_progress_percent)。"""
    return task_progress(completed_count, total_count), workload_progress(
        completed_minutes, total_minutes
    )

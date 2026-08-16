"""Study Session 计时纯函数（§12.5/§13.2/D28，无 I/O）。

- heartbeat 单调 seq：相同 seq 幂等重放，小于已确认值拒绝；
- 只统计有效 heartbeat 区间：间隔 ≥ 最小有效间隔（默认 30s）才累计；
- 超过空闲阈值（默认 120s）停止累计：单段最多计 idle_timeout 秒；
- 服务器时间由调用方传入，不信任客户端时间戳。
"""

from __future__ import annotations

from datetime import datetime


def added_active_seconds(
    *,
    gap_seconds: float,
    min_interval_seconds: int,
    idle_timeout_seconds: int,
) -> int:
    """一段 heartbeat 间隔应累计的活跃秒数（§13.2 规则 3–5）。

    - gap < min_interval：不计（过快/重复区间不虚增）；
    - min_interval ≤ gap ≤ idle_timeout：计 gap 整段；
    - gap > idle_timeout：只计 idle_timeout（超时后停止累计）。
    """
    if gap_seconds < min_interval_seconds:
        return 0
    return int(min(gap_seconds, idle_timeout_seconds))


def heartbeat_decision(
    *,
    seq: int,
    last_seq: int,
    now: datetime,
    last_heartbeat_at: datetime | None,
    min_interval_seconds: int,
    idle_timeout_seconds: int,
) -> tuple[str, int]:
    """判定一次 heartbeat：返回 (判定, 新增活跃秒数)。

    判定取值：replay（同 seq 幂等重放）/ conflict（seq 小于已确认值）/
    too_fast（间隔小于最小有效间隔）/ accepted。
    """
    if seq < last_seq:
        return "conflict", 0
    if seq == last_seq:
        return "replay", 0
    if last_heartbeat_at is None:
        return "accepted", 0
    gap = (now - last_heartbeat_at).total_seconds()
    added = added_active_seconds(
        gap_seconds=gap,
        min_interval_seconds=min_interval_seconds,
        idle_timeout_seconds=idle_timeout_seconds,
    )
    if added == 0:
        # 间隔小于最小有效间隔（非空闲场景：gap 可能也超过 idle，此时 added 为
        # idle_timeout 而非 0，不会走到这里）
        return "too_fast", 0
    return "accepted", added

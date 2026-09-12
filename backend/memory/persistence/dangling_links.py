"""memory_dangling_links 持久层：悬空链接两级制（memory-rebuild §3.2 / §5.9④ / 决议表 E 组）。

**两级制**：``[[link]]`` 指向尚不存在的主题是合法的（§3.2「悬空链接合法」）。
同一条悬空链接

- 在 **1 个批次**里出现 → 只是"候选主题"，只进 index 的候选主题区；
- 累计到 **≥2 个批次**出现 → consolidation 才正式建立 mastery 文档
  （§5.9④「第一次出现只加入 index 的候选主题区；累计至少两个批次出现才创建正式
  mastery 文档」）。

跨批次的"出现过多少个批次"必须落在 PG 里（2026-09-12 裁决：新建
``memory_dangling_links`` 表），不能写进 ``index.md``（那是派生品，``rebuild_index``
会整份重写），也不复用 ``memory_review_candidates``（那是人工审核队列，语义不同）。

本模块只负责 SQL 与纯函数；**事务由调用方管理**（函数不自己 begin/commit），
所有查询恒带 ``user_id``（用户隔离硬要求）。
"""

from __future__ import annotations

import json
from collections.abc import Iterable, Mapping
from typing import Any
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from backend.memory.contracts.common import TopicKeyError, normalize_topic_title
from backend.memory.persistence.database import exec_rowcount

#: source_memory_ids 的元素上限（与 0001 的 evidence_refs <= 100 同一口径，
#: 也与 0010 迁移里的 CHECK 约束一致——两处独立真相，改动必须同步）。
MAX_SOURCE_MEMORY_IDS = 100

#: target / target_key 的列宽（0010 迁移 varchar(160)）。
MAX_TARGET_LENGTH = 160

#: 只有 candidate 行参与累计：dismissed 是用户明确否决，promoted 已正式建档。
ACCUMULATING_STATUS = "candidate"

_LINK_COLUMNS = (
    "link_id, user_id, target, target_key, status, sighting_batches, "
    "first_seen_at, last_seen_at, first_batch_operation_id, "
    "last_batch_operation_id, source_memory_ids, promoted_memory_id"
)

#: 登记本批出现的 upsert。语义见 record_sightings 的 docstring，关键点：
#: - ``ON CONFLICT (user_id, target_key)``：同一用户同一目标只有一行，跨批次累加；
#: - 幂等键是 ``last_batch_operation_id``：相等则 CASE 取 0，本批不再 +1；
#: - DO UPDATE 上的 WHERE 只放行 candidate 行，dismissed/promoted 行原样保留
#:   （``RETURNING`` 也就不会返回它们）；
#: - ``source_memory_ids`` 在 SQL 里做"旧值 + 新值"的保序去重合并，并截断到上限。
_RECORD_SQL = f"""
INSERT INTO memory_dangling_links (
    link_id, user_id, target, target_key, status, sighting_batches,
    first_seen_at, last_seen_at,
    first_batch_operation_id, last_batch_operation_id, source_memory_ids
)
SELECT
    gen_random_uuid(), :user_id, item.target, item.target_key, 'candidate', 1,
    now(), now(), :batch_operation_id, :batch_operation_id, item.source_memory_ids
FROM jsonb_to_recordset(CAST(:sightings AS jsonb))
    AS item(target text, target_key text, source_memory_ids jsonb)
ON CONFLICT (user_id, target_key) DO UPDATE SET
    sighting_batches = memory_dangling_links.sighting_batches + CASE
        WHEN memory_dangling_links.last_batch_operation_id = EXCLUDED.last_batch_operation_id
            THEN 0
        ELSE 1
    END,
    last_seen_at = now(),
    last_batch_operation_id = EXCLUDED.last_batch_operation_id,
    source_memory_ids = (
        SELECT COALESCE(jsonb_agg(picked.value ORDER BY picked.ord), '[]'::jsonb)
        FROM (
            SELECT deduped.value, deduped.ord
            FROM (
                SELECT DISTINCT ON (elem.value) elem.value, elem.ord
                FROM jsonb_array_elements_text(
                    memory_dangling_links.source_memory_ids || EXCLUDED.source_memory_ids
                ) WITH ORDINALITY AS elem(value, ord)
                ORDER BY elem.value, elem.ord
            ) AS deduped
            ORDER BY deduped.ord
            LIMIT {MAX_SOURCE_MEMORY_IDS}
        ) AS picked
    )
WHERE memory_dangling_links.status = '{ACCUMULATING_STATUS}'
RETURNING {_LINK_COLUMNS}
"""

_LIST_CANDIDATES_SQL = f"""
SELECT {_LINK_COLUMNS}
FROM memory_dangling_links
WHERE user_id = :user_id
  AND status = '{ACCUMULATING_STATUS}'
  AND sighting_batches >= :min_batches
ORDER BY sighting_batches DESC, last_seen_at DESC, link_id ASC
LIMIT :limit
"""

_LIST_USER_LINKS_SQL = f"""
SELECT {_LINK_COLUMNS}
FROM memory_dangling_links
WHERE user_id = :user_id
  AND (CAST(:status AS text) IS NULL OR status = :status)
ORDER BY sighting_batches DESC, last_seen_at DESC, link_id ASC
"""

#: 标记正式建档：candidate → promoted；已经是同一 memory_id 的 promoted 行再调一次
#: 仍返回 True（夜间批重试必须幂等，不能让调用方把"已生效"误判为失败）。
_MARK_PROMOTED_SQL = """
UPDATE memory_dangling_links
SET status = 'promoted', promoted_memory_id = :memory_id
WHERE user_id = :user_id
  AND target_key = :target_key
  AND (
      status = 'candidate'
      OR (status = 'promoted' AND promoted_memory_id = :memory_id)
  )
"""


class DanglingLinkError(ValueError):
    """悬空链接入参非法（缺字段、类型不符、规范化后为空、超出列宽）。"""


# ---------------------------------------------------------------------------
# 纯函数：规范化、解析与批内合并（图节点可直接复用，无需数据库）
# ---------------------------------------------------------------------------


def normalize_link_target(raw: str) -> str:
    """``[[...]]`` 目标 → 比较键（NFKC + 去首尾空白，列宽内非空）。

    直接复用 :func:`normalize_topic_title`，因此**真实语义**是（不要按直觉假设）：

    - 全角/半角折叠：``Ｅllipse`` → ``Ellipse``（NFKC），``椭圆 `` → ``椭圆``；
    - **不改大小写**：``Ellipse`` 与 ``ellipse`` 是**两个不同的键**；
    - 内部空白不折叠：``椭 圆`` 仍是 ``椭 圆``（区别于 ``topic_key_from_title``
      的"空白折成 '-'"）；
    - 含控制字符/隐藏字符时 ``normalize_topic_title`` 抛 ``TopicKeyError``，
      这里原样向上传播（登记入口宁可报错，也不要静默把两条链接合并成一行）。
    """
    try:
        key = normalize_topic_title(raw)
    except TopicKeyError as exc:
        raise DanglingLinkError(f"悬空链接目标非法: {raw!r}（{exc}）") from exc
    if not key:
        raise DanglingLinkError(f"悬空链接目标规范化后为空: {raw!r}")
    if len(key) > MAX_TARGET_LENGTH:
        raise DanglingLinkError(f"悬空链接目标超过 {MAX_TARGET_LENGTH} 字符: {raw!r}")
    return key


def merge_batch_sightings(sightings: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """批内合并：同一 ``target_key`` 只算 **1 次**出现。

    ``sighting_batches`` 数的是"出现过多少个**批次**"，不是"出现次数"，所以同一批
    里同一目标被多篇文档引用也只登记 1 行；``source_memory_ids`` 在批内合并去重
    （保序、截断到 :data:`MAX_SOURCE_MEMORY_IDS`）。``target`` 取首次出现的原始文本
    （同一 key 的不同写法只保留第一个，避免展示文本随批次漂移）。

    纯函数，便于单测直接断言；返回值就是 :func:`record_sightings` 的入参形状。
    """
    merged: dict[str, dict[str, Any]] = {}
    for raw in sightings:
        if not isinstance(raw, dict):
            raise DanglingLinkError(f"悬空链接条目必须是 dict: {raw!r}")
        target = normalize_link_target(_require_str(raw, "target"))
        target_key = normalize_link_target(_require_str(raw, "target_key"))
        sources = _normalize_source_ids(raw.get("source_memory_ids"))
        entry = merged.get(target_key)
        if entry is None:
            merged[target_key] = {
                "target": target,
                "target_key": target_key,
                "source_memory_ids": sources,
            }
            continue
        entry["source_memory_ids"] = _merge_source_ids(entry["source_memory_ids"], sources)
    return list(merged.values())


def resolve_link_target(target: str, *, known: Mapping[str, str]) -> str | None:
    """按 §3.2 的解析顺序把 ``[[link]]`` 目标解析成 memory_id；解析不到返回 ``None``。

    ``known`` 是"规范化查找键 → memory_id"的命名空间，应由 :func:`link_namespace`
    构造（它保证 **memory_id 精确 → name → aliases** 的优先级）。返回 ``None``
    有两种含义，调用方按场景区分：

    - **悬空链接**（合法）：目标尚未建档，正是本模块要收集的对象；
    - **非法目标**（含控制字符、规范化后为空）：扫描路径不应因单个坏链接中断，
      因此这里不抛错；登记时 :func:`normalize_link_target` 才会拒绝。

    大小写仍然敏感（只做 NFKC + strip，与键的构造保持一致）。
    """
    try:
        key = normalize_link_target(target)
    except DanglingLinkError:
        return None
    return known.get(key)


def link_namespace(entries: Iterable[Mapping[str, Any]]) -> dict[str, str]:
    """从 ``memory_index_entries`` 行构造链接解析命名空间（§3.2 三级优先级）。

    每行读 ``memory_id``（必填）、``title``（= index 的 name）、``aliases``。
    三类键注册到同一命名空间，按 **memory_id → name → aliases** 的顺序分三趟写，
    先注册者胜出——这样某文档的 alias 不会覆盖另一文档的正式 name。

    非法键（控制字符、空串）直接跳过：这是只读扫描路径，不能因为注册表里一条脏
    别名就让整次 consolidation 失败。
    """
    rows = [dict(row) for row in entries]
    namespace: dict[str, str] = {}

    def register(raw: Any, memory_id: str) -> None:
        if not isinstance(raw, str):
            return
        try:
            key = normalize_link_target(raw)
        except DanglingLinkError:
            return
        namespace.setdefault(key, memory_id)

    def usable(row: Mapping[str, Any]) -> str | None:
        memory_id = row.get("memory_id")
        return memory_id if isinstance(memory_id, str) and memory_id else None

    # 三趟注册：先全部 memory_id，再全部 name，最后全部 aliases（先注册者胜出）。
    for row in rows:
        if (memory_id := usable(row)) is not None:
            register(memory_id, memory_id)
    for row in rows:
        if (memory_id := usable(row)) is not None:
            register(row.get("title"), memory_id)
    for row in rows:
        if (memory_id := usable(row)) is None:
            continue
        for alias in row.get("aliases") or []:
            register(alias, memory_id)
    return namespace


def document_dangling_sightings(
    *,
    memory_id: str,
    links: Iterable[str],
    known: Mapping[str, str],
) -> list[dict[str, Any]]:
    """把一篇文档的 ``[[link]]`` 列表筛成 :func:`record_sightings` 的入参。

    - 已能解析（memory_id / name / aliases 命中）的链接**不是**悬空链接，跳过；
    - 非法目标（含控制字符、空、超长）**跳过**而不抛错：单个坏链接不能拖垮整批
      consolidation（§5.9「失败告警不阻塞下一用户/下一批次」）；
    - 同一目标在本文档里重复出现只产出 1 条（登记端还会再按批合并一次）。

    图节点（consolidation）通常只需要 ``extract_links`` + 本函数 + ``record_sightings``。
    """
    sightings: list[dict[str, Any]] = []
    seen: set[str] = set()
    for link in links:
        if not isinstance(link, str):
            continue
        try:
            key = normalize_link_target(link)
        except DanglingLinkError:
            continue
        if key in seen or resolve_link_target(link, known=known) is not None:
            continue
        seen.add(key)
        sightings.append({"target": link, "target_key": key, "source_memory_ids": [memory_id]})
    return sightings


# ---------------------------------------------------------------------------
# 持久化 API（事务由调用方管理）
# ---------------------------------------------------------------------------


async def record_sightings(
    session: AsyncSession,
    *,
    user_id: UUID,
    batch_operation_id: UUID,
    sightings: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """登记本批的悬空链接出现，返回被更新/插入的行（含 status/sighting_batches）。

    - 同一 ``target_key`` 在同一批里出现多次 → **本批只计 1 次**（``sighting_batches``
      是"批次数"而非"出现次数"）；``source_memory_ids`` 批内合并去重。
    - 同一批次被重跑（幂等重试）**不重复 +1**：幂等键是 ``last_batch_operation_id``，
      该列等于本批 ``batch_operation_id`` 时只刷新 ``last_seen_at`` 与
      ``source_memory_ids``。判定与累加在**同一条 upsert 语句**里完成，因此并发
      重跑同一批次也不会多算（不需要额外加锁）。
    - ``status='dismissed'``（用户已否决）与 ``status='promoted'``（已正式建档）
      的行不再累积：它们不进入 ``DO UPDATE``，也不在返回值里。
    - 入参非法（缺字段/类型不符/规范化后为空/超 160 字符）抛
      :class:`DanglingLinkError`，不静默丢弃；调用方若要"跳过坏链接"，用
      :func:`document_dangling_sightings` 预筛。

    ``batch_operation_id`` 写入 ``first/last_batch_operation_id``，供 §5.9 验收要求的
    "悬空链接变更可从 batch_operation_id 回溯"。
    """
    merged = merge_batch_sightings(sightings)
    if not merged:
        return []
    result = await session.execute(
        text(_RECORD_SQL),
        {
            "user_id": user_id,
            "batch_operation_id": batch_operation_id,
            "sightings": _json_dumps(merged),
        },
    )
    # 多行 upsert 的 RETURNING 顺序不保证，按 target_key 排序给出确定性结果。
    return sorted((dict(row) for row in result.mappings().all()), key=lambda r: r["target_key"])


async def list_candidates(
    session: AsyncSession, *, user_id: UUID, min_batches: int = 2, limit: int = 50
) -> list[dict[str, Any]]:
    """列出达到建档门槛的候选（``status='candidate' AND sighting_batches >= min_batches``）。

    默认 ``min_batches=2`` 就是 §5.9④ 的两级制门槛：只出现过 1 个批次的悬空链接
    只进 index 的候选主题区，不进这里。
    """
    result = await session.execute(
        text(_LIST_CANDIDATES_SQL),
        {"user_id": user_id, "min_batches": min_batches, "limit": limit},
    )
    return [dict(row) for row in result.mappings().all()]


async def list_user_links(
    session: AsyncSession, *, user_id: UUID, status: str | None = None
) -> list[dict[str, Any]]:
    """列出该用户的全部悬空链接记录（index 候选主题区展示 / 运维排查）。"""
    result = await session.execute(
        text(_LIST_USER_LINKS_SQL), {"user_id": user_id, "status": status}
    )
    return [dict(row) for row in result.mappings().all()]


async def mark_promoted(
    session: AsyncSession, *, user_id: UUID, target_key: str, memory_id: str
) -> bool:
    """把候选标记为已正式建档（``status='promoted'`` + ``promoted_memory_id``）。

    返回"该 target_key 现在是否就是 promoted 且指向本 memory_id"：首次迁移返回
    True；对同一 ``(target_key, memory_id)`` 重试也返回 True（幂等，夜间批重试不会
    把"已生效"误判成失败）。行不存在、已 dismissed、或已 promoted 到别的 memory_id
    时返回 False，且不改动任何字段。
    """
    key = normalize_link_target(target_key)
    rowcount = await exec_rowcount(
        session,
        text(_MARK_PROMOTED_SQL),
        {"user_id": user_id, "target_key": key, "memory_id": memory_id},
    )
    return rowcount == 1


# ---------------------------------------------------------------------------
# 内部工具
# ---------------------------------------------------------------------------


def _require_str(raw: Mapping[str, Any], key: str) -> str:
    value = raw.get(key)
    if not isinstance(value, str):
        raise DanglingLinkError(f"悬空链接条目缺少字符串字段 {key}: {raw!r}")
    return value


def _normalize_source_ids(raw: Any) -> list[str]:
    """source_memory_ids 规范化：丢空串、去重保序、截断到上限。"""
    if raw is None:
        return []
    if not isinstance(raw, list):
        raise DanglingLinkError(f"source_memory_ids 必须是列表: {raw!r}")
    result: list[str] = []
    seen: set[str] = set()
    for item in raw:
        if not isinstance(item, str):
            raise DanglingLinkError(f"source_memory_ids 元素必须是字符串: {item!r}")
        value = item.strip()
        if not value or value in seen:
            continue
        seen.add(value)
        result.append(value)
        if len(result) >= MAX_SOURCE_MEMORY_IDS:
            break
    return result


def _merge_source_ids(existing: list[str], incoming: list[str]) -> list[str]:
    return _normalize_source_ids([*existing, *incoming])


def _json_dumps(value: Any) -> str:
    """入参 JSON 序列化：``ensure_ascii=False`` 保留中文原文，便于直接读日志。"""
    return json.dumps(value, ensure_ascii=False)

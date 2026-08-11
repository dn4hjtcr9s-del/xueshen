"""知识图谱只读注册表（规格 §13.8 / §16.4）。

业务运行期只读数据库注册表，不在每次请求时重新解析 Markdown。
同步命令在一个事务中替换注册表。
"""

from __future__ import annotations

import unicodedata
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from backend.memory.knowledge_graph.parser import ParsedGraph

PUNCT_TRANSLATION = str.maketrans(
    {
        "（": "(",
        "）": ")",
        "，": ",",
        "。": ".",
        "：": ":",
        "；": ";",
        "！": "!",
        "？": "?",
        "、": ",",
        "　": " ",
        "−": "-",
        "–": "-",
        "×": "x",
        "·": ".",
    }
)


def normalize_graph_title(title: str) -> str:
    """规范化空白、大小写、全半角、常用中英文标点及安全的数学记号变体（§16.4）。"""
    normalized = unicodedata.normalize("NFKC", title).translate(PUNCT_TRANSLATION)
    return " ".join(normalized.lower().split())


def derived_aliases(title: str) -> list[str]:
    """由正式节点标题确定性生成 derived alias（同步时可重建）。"""
    aliases = {normalize_graph_title(title)}
    # 去掉章节序号前缀（如 "第一章 "）的形式
    parts = title.split(" ", 1)
    if len(parts) == 2 and parts[0]:
        aliases.add(normalize_graph_title(parts[1]))
    return sorted(a for a in aliases if a)


class KnowledgeGraphRegistry:
    """注册表只读访问 + 同步写入。"""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    # ---------------- 只读 ----------------

    async def list_nodes(self) -> list[dict[str, Any]]:
        result = await self._session.execute(
            text("SELECT * FROM knowledge_graph_nodes ORDER BY node_id")
        )
        return [dict(r) for r in result.mappings().all()]

    async def list_edges(self) -> list[dict[str, Any]]:
        result = await self._session.execute(
            text("SELECT * FROM knowledge_graph_edges ORDER BY from_node_id, to_node_id")
        )
        return [dict(r) for r in result.mappings().all()]

    async def get_node(self, node_id: str) -> dict[str, Any] | None:
        result = await self._session.execute(
            text("SELECT * FROM knowledge_graph_nodes WHERE node_id = :node_id"),
            {"node_id": node_id},
        )
        row = result.mappings().first()
        return dict(row) if row else None

    async def node_exists(self, node_id: str) -> bool:
        result = await self._session.execute(
            text("SELECT 1 FROM knowledge_graph_nodes WHERE node_id = :node_id"),
            {"node_id": node_id},
        )
        return result.scalar() is not None

    async def edges_from(self, node_id: str) -> list[str]:
        result = await self._session.execute(
            text("SELECT to_node_id FROM knowledge_graph_edges WHERE from_node_id = :node_id"),
            {"node_id": node_id},
        )
        return [str(r[0]) for r in result.all()]

    async def edges_to(self, node_id: str) -> list[str]:
        result = await self._session.execute(
            text("SELECT from_node_id FROM knowledge_graph_edges WHERE to_node_id = :node_id"),
            {"node_id": node_id},
        )
        return [str(r[0]) for r in result.all()]

    async def find_by_normalized_title(self, normalized: str) -> list[dict[str, Any]]:
        """标题或 alias 规范化精确匹配（§16.4 优先级 2/3）。"""
        result = await self._session.execute(
            text(
                """
                SELECT n.* FROM knowledge_graph_nodes n
                WHERE lower(n.title) = :normalized
                UNION
                SELECT n.* FROM knowledge_graph_nodes n
                JOIN knowledge_graph_node_aliases a ON a.node_id = n.node_id
                WHERE a.normalized_alias = :normalized
                """
            ),
            {"normalized": normalized},
        )
        return [dict(r) for r in result.mappings().all()]

    async def trgm_candidates(self, query: str, limit: int = 5) -> list[dict[str, Any]]:
        """标题和 alias 的原始 pg_trgm 相似度候选（§16.4 优先级 4）。"""
        result = await self._session.execute(
            text(
                """
                SELECT node_id, title, MAX(score) AS score FROM (
                    SELECT node_id, title, similarity(title, :query) AS score
                    FROM knowledge_graph_nodes
                    UNION ALL
                    SELECT a.node_id, n.title, similarity(a.alias, :query) AS score
                    FROM knowledge_graph_node_aliases a
                    JOIN knowledge_graph_nodes n ON n.node_id = a.node_id
                ) s
                GROUP BY node_id, title
                ORDER BY score DESC
                LIMIT :limit
                """
            ),
            {"query": query, "limit": limit},
        )
        return [dict(r) for r in result.mappings().all()]

    async def latest_applied_sync(self) -> dict[str, Any] | None:
        result = await self._session.execute(
            text(
                "SELECT * FROM knowledge_graph_sync_runs WHERE applied = true "
                "ORDER BY applied_at DESC LIMIT 1"
            )
        )
        row = result.mappings().first()
        return dict(row) if row else None

    # ---------------- 同步写入 ----------------

    async def create_sync_run(self, parsed: ParsedGraph) -> UUID:
        run_id = uuid4()
        await self._session.execute(
            text(
                """
                INSERT INTO knowledge_graph_sync_runs (
                    run_id, graph_file, graph_checksum, catalog_file,
                    catalog_checksum, manifest_checksum, result
                ) VALUES (
                    :run_id, :graph_file, :graph_checksum, :catalog_file,
                    :catalog_checksum, :manifest_checksum, CAST(:result AS jsonb)
                )
                """
            ),
            {
                "run_id": run_id,
                "graph_file": "数学知识科技树关系图.md",
                "graph_checksum": parsed.graph_checksum,
                "catalog_file": "教材目录.md",
                "catalog_checksum": parsed.catalog_checksum,
                "manifest_checksum": parsed.manifest_checksum,
                "result": (
                    f'{{"node_count": {len(parsed.nodes)}, "edge_count": {len(parsed.edges)}}}'
                ),
            },
        )
        return run_id

    async def plan_removals(self, parsed: ParsedGraph) -> list[str]:
        """新图谱中不存在、但注册表中已有的节点。"""
        result = await self._session.execute(text("SELECT node_id FROM knowledge_graph_nodes"))
        existing = {str(r[0]) for r in result.all()}
        incoming = {n.node_id for n in parsed.nodes}
        return sorted(existing - incoming)

    async def removal_references(self, node_ids: list[str]) -> dict[str, int]:
        """删除节点会波及的 Overlay / activity / link 数量（§13.8）。"""
        refs: dict[str, int] = {}
        for table in ("graph_user_states", "graph_user_node_activity", "memory_graph_links"):
            result = await self._session.execute(
                text(f"SELECT COUNT(*) FROM {table} WHERE node_id = ANY(:node_ids)"),
                {"node_ids": node_ids},
            )
            refs[table] = int(result.scalar_one())
        return refs

    async def archive_removal_audit(
        self,
        *,
        sync_run_id: UUID,
        node_ids: list[str],
        privacy_hmac_key: str,
    ) -> None:
        """--allow-remove：先归档审计再删除（§13.8）。禁止无审计级联删除。"""
        import hashlib
        import json as jsonlib

        for node_id in node_ids:
            for table in ("graph_user_states", "graph_user_node_activity", "memory_graph_links"):
                result = await self._session.execute(
                    text(f"SELECT * FROM {table} WHERE node_id = :node_id"),
                    {"node_id": node_id},
                )
                rows = [dict(r) for r in result.mappings().all()]
                by_user: dict[str, list[dict[str, Any]]] = {}
                for row in rows:
                    by_user.setdefault(str(row["user_id"]), []).append(row)
                for user_id, user_rows in by_user.items():
                    checksum = hashlib.sha256(
                        jsonlib.dumps(user_rows, sort_keys=True, default=str).encode()
                    ).hexdigest()
                    user_hash = hashlib.sha256(
                        f"privacy-audit:v1:{privacy_hmac_key}:{user_id}".encode()
                    ).hexdigest()
                    await self._session.execute(
                        text(
                            """
                            INSERT INTO knowledge_graph_node_removal_audit (
                                removal_audit_id, sync_run_id, node_id, record_type,
                                user_hash, original_record_checksum, affected_count
                            ) VALUES (
                                :removal_audit_id, :sync_run_id, :node_id, :record_type,
                                :user_hash, :original_record_checksum, :affected_count
                            )
                            """
                        ),
                        {
                            "removal_audit_id": uuid4(),
                            "sync_run_id": sync_run_id,
                            "node_id": node_id,
                            "record_type": table,
                            "user_hash": user_hash,
                            "original_record_checksum": checksum,
                            "affected_count": len(user_rows),
                        },
                    )

    async def apply_sync(
        self, *, parsed: ParsedGraph, sync_run_id: UUID, allow_remove: bool
    ) -> None:
        """一个事务中替换注册表：节点、边、derived alias。"""
        incoming = {n.node_id for n in parsed.nodes}
        removals = await self.plan_removals(parsed)
        if removals and not allow_remove:
            refs = await self.removal_references(removals)
            raise ValueError(f"同步将删除节点 {removals}，引用计数 {refs}；需要 --allow-remove")

        # 更新/插入节点
        for node in parsed.nodes:
            await self._session.execute(
                text(
                    """
                    INSERT INTO knowledge_graph_nodes (
                        node_id, title, group_key, source_file, source_checksum, metadata
                    ) VALUES (
                        :node_id, :title, :group_key, :source_file, :source_checksum,
                        CAST(:metadata AS jsonb)
                    )
                    ON CONFLICT (node_id) DO UPDATE
                    SET title = EXCLUDED.title, group_key = EXCLUDED.group_key,
                        source_file = EXCLUDED.source_file,
                        source_checksum = EXCLUDED.source_checksum,
                        metadata = EXCLUDED.metadata, synced_at = now()
                    """
                ),
                {
                    "node_id": node.node_id,
                    "title": node.title,
                    "group_key": node.group_key,
                    "source_file": "数学知识科技树关系图.md",
                    "source_checksum": parsed.graph_checksum,
                    "metadata": (
                        f'{{"subgraph_id": "{node.subgraph_id or ""}", '
                        f'"stage": "{node.stage or ""}"}}'
                    ),
                },
            )
        # 替换边
        await self._session.execute(
            text(
                "DELETE FROM knowledge_graph_edges "
                "WHERE from_node_id = ANY(:ids) OR to_node_id = ANY(:ids)"
            ),
            {"ids": list(incoming)},
        )
        for edge in parsed.edges:
            await self._session.execute(
                text(
                    """
                    INSERT INTO knowledge_graph_edges (
                        from_node_id, to_node_id, relation_type, source_checksum
                    ) VALUES (
                        :from_node_id, :to_node_id, :relation_type, :source_checksum
                    )
                    ON CONFLICT DO NOTHING
                    """
                ),
                {
                    "from_node_id": edge.from_node_id,
                    "to_node_id": edge.to_node_id,
                    "relation_type": edge.relation_type,
                    "source_checksum": parsed.graph_checksum,
                },
            )
        # 重建 derived alias；保留仍指向存在节点的 manual alias（§16.4）
        await self._session.execute(
            text("DELETE FROM knowledge_graph_node_aliases WHERE alias_source = 'derived'")
        )
        for node in parsed.nodes:
            for alias in derived_aliases(node.title):
                await self._session.execute(
                    text(
                        """
                        INSERT INTO knowledge_graph_node_aliases (
                            node_id, alias, normalized_alias, alias_source
                        ) VALUES (:node_id, :alias, :normalized_alias, 'derived')
                        ON CONFLICT DO NOTHING
                        """
                    ),
                    {
                        "node_id": node.node_id,
                        "alias": alias,
                        "normalized_alias": normalize_graph_title(alias),
                    },
                )
        # 删除被移除节点（审计由调用方先行完成）
        if removals:
            await self._session.execute(
                text("DELETE FROM graph_user_states WHERE node_id = ANY(:ids)"),
                {"ids": removals},
            )
            await self._session.execute(
                text("DELETE FROM graph_user_node_activity WHERE node_id = ANY(:ids)"),
                {"ids": removals},
            )
            await self._session.execute(
                text("DELETE FROM memory_graph_links WHERE node_id = ANY(:ids)"),
                {"ids": removals},
            )
            await self._session.execute(
                text("DELETE FROM knowledge_graph_nodes WHERE node_id = ANY(:ids)"),
                {"ids": removals},
            )
        await self._session.execute(
            text(
                "UPDATE knowledge_graph_sync_runs SET applied = true, applied_at = now() "
                "WHERE run_id = :run_id"
            ),
            {"run_id": sync_run_id},
        )

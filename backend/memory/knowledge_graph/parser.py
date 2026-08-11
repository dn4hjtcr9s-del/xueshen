"""固定知识图谱 Mermaid 解析器（规格 §16.1）。

权威来源：knowledge_graph/数学知识科技树关系图.md（节点 ID、标题、分组、有向边）。
校验失败规则：重复 node ID、缺失节点、悬空边、非法 ID、同一 ID 标题变化
一律使同步失败。
虚线边 `-.->`（教材顺序提示边）按 2026-08-11 裁决 A 处理：跳过并记录警告，
不阻断同步，不入库；6 个无细目专题节点照常入库。
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from pathlib import Path

NODE_ID_PATTERN = re.compile(r"^n\d{3,}$")
NODE_DEF_PATTERN = re.compile(r'^\s*(n\d{3,})\s*\["([^"]+)"\]\s*$')
SUBGRAPH_START_PATTERN = re.compile(r'^\s*subgraph\s+(\w+)\s*\["([^"]+)"\]\s*$')
SUBGRAPH_ID_ONLY_PATTERN = re.compile(r"^\s*subgraph\s+(\w+)\s*$")
SOLID_EDGE_PATTERN = re.compile(r"^\s*(n\d{3,})\s*-->\s*(n\d{3,})\s*$")
DASHED_EDGE_PATTERN = re.compile(r"^\s*n\d{3,}\s*-\..*?\.->\s*n\d{3,}\s*$")
CLASS_DEF_PATTERN = re.compile(r"^\s*classDef\s+\w+\s+.*$")
CLASS_ASSIGN_PATTERN = re.compile(r"^\s*class\s+((?:n\d{3,},)*n\d{3,})\s+(\w+)\s*;?\s*$")

GRAPH_FILE_NAME = "数学知识科技树关系图.md"
CATALOG_FILE_NAME = "教材目录.md"


class KnowledgeGraphParseError(ValueError):
    """图谱解析/校验失败，同步命令必须中止。"""


@dataclass(frozen=True)
class ParsedNode:
    node_id: str
    title: str
    group_key: str | None
    subgraph_id: str | None
    stage: str | None


@dataclass(frozen=True)
class ParsedEdge:
    from_node_id: str
    to_node_id: str
    relation_type: str = "prerequisite"


@dataclass
class ParsedGraph:
    nodes: list[ParsedNode] = field(default_factory=list)
    edges: list[ParsedEdge] = field(default_factory=list)
    skipped_dashed_edges: list[str] = field(default_factory=list)
    graph_checksum: str = ""
    catalog_checksum: str = ""
    manifest_checksum: str = ""


def sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def compute_manifest_checksum(
    graph_checksum: str, catalog_checksum: str, nodes: list[ParsedNode], edges: list[ParsedEdge]
) -> str:
    """节点/边/源文件 checksum 的确定性 manifest 摘要。"""
    lines = [f"graph:{graph_checksum}", f"catalog:{catalog_checksum}"]
    for node in sorted(nodes, key=lambda n: n.node_id):
        lines.append(f"node:{node.node_id}:{node.title}:{node.group_key or ''}")
    for edge in sorted(edges, key=lambda e: (e.from_node_id, e.to_node_id, e.relation_type)):
        lines.append(f"edge:{edge.from_node_id}:{edge.to_node_id}:{edge.relation_type}")
    return hashlib.sha256("\n".join(lines).encode("utf-8")).hexdigest()


def parse_graph_file(graph_path: Path, catalog_path: Path) -> ParsedGraph:
    """解析 Mermaid 图谱文件并执行全部确定性校验。"""
    if not graph_path.exists():
        raise KnowledgeGraphParseError(f"图谱文件不存在: {graph_path}")
    if not catalog_path.exists():
        raise KnowledgeGraphParseError(f"教材目录不存在: {catalog_path}")

    text = graph_path.read_text(encoding="utf-8")
    in_mermaid = False
    current_group: tuple[str, str] | None = None  # (subgraph_id, group_title)
    titles: dict[str, str] = {}
    groups: dict[str, tuple[str | None, str | None]] = {}
    stages: dict[str, str] = {}
    edges: list[ParsedEdge] = []
    dashed: list[str] = []

    for line_no, line in enumerate(text.splitlines(), start=1):
        stripped = line.strip()
        if stripped.startswith("```mermaid"):
            in_mermaid = True
            continue
        if in_mermaid and stripped.startswith("```"):
            in_mermaid = False
            continue
        if not in_mermaid:
            continue

        m = SUBGRAPH_START_PATTERN.match(line)
        if m:
            current_group = (m.group(1), m.group(2))
            continue
        m = SUBGRAPH_ID_ONLY_PATTERN.match(line)
        if m:
            current_group = (m.group(1), m.group(1))
            continue
        if stripped == "end":
            current_group = None
            continue
        if stripped.startswith("%%") or stripped.startswith("flowchart") or not stripped:
            continue
        if stripped.startswith("direction"):
            continue
        if CLASS_DEF_PATTERN.match(line):
            continue
        m = CLASS_ASSIGN_PATTERN.match(line)
        if m:
            for node_id in m.group(1).split(","):
                stages[node_id] = m.group(2)
            continue
        if DASHED_EDGE_PATTERN.match(line):
            dashed.append(f"line {line_no}: {stripped}")
            continue
        m = NODE_DEF_PATTERN.match(line)
        if m:
            node_id, title = m.group(1), m.group(2)
            if not NODE_ID_PATTERN.match(node_id):
                raise KnowledgeGraphParseError(f"非法节点 ID: {node_id} (line {line_no})")
            if node_id in titles and titles[node_id] != title:
                raise KnowledgeGraphParseError(
                    f"同一 ID 标题变化: {node_id} '{titles[node_id]}' -> '{title}'"
                )
            titles[node_id] = title
            groups[node_id] = (
                current_group[1] if current_group else None,
                current_group[0] if current_group else None,
            )
            continue
        m = SOLID_EDGE_PATTERN.match(line)
        if m:
            edges.append(ParsedEdge(from_node_id=m.group(1), to_node_id=m.group(2)))
            continue
        raise KnowledgeGraphParseError(f"无法识别的行 (line {line_no}): {stripped}")

    if not titles:
        raise KnowledgeGraphParseError("未解析到任何节点")

    dangling = sorted(
        ({e.from_node_id for e in edges} | {e.to_node_id for e in edges}) - set(titles)
    )
    if dangling:
        raise KnowledgeGraphParseError(f"悬空边引用缺失节点: {dangling}")

    unknown_stage_nodes = sorted(set(stages) - set(titles))
    if unknown_stage_nodes:
        raise KnowledgeGraphParseError(f"class 赋值引用缺失节点: {unknown_stage_nodes}")

    seen_edges: set[tuple[str, str, str]] = set()
    unique_edges: list[ParsedEdge] = []
    for edge in edges:
        key = (edge.from_node_id, edge.to_node_id, edge.relation_type)
        if key not in seen_edges:
            seen_edges.add(key)
            unique_edges.append(edge)

    nodes = [
        ParsedNode(
            node_id=node_id,
            title=title,
            group_key=groups[node_id][0],
            subgraph_id=groups[node_id][1],
            stage=stages.get(node_id),
        )
        for node_id, title in sorted(titles.items())
    ]
    graph_checksum = sha256_file(graph_path)
    catalog_checksum = sha256_file(catalog_path)
    return ParsedGraph(
        nodes=nodes,
        edges=unique_edges,
        skipped_dashed_edges=dashed,
        graph_checksum=graph_checksum,
        catalog_checksum=catalog_checksum,
        manifest_checksum=compute_manifest_checksum(
            graph_checksum, catalog_checksum, nodes, unique_edges
        ),
    )


def parse_catalog_titles(catalog_path: Path) -> list[str]:
    """教材目录四级标题（####）作为辅助展示元数据来源，不生成节点 ID。"""
    titles: list[str] = []
    for line in catalog_path.read_text(encoding="utf-8").splitlines():
        if line.startswith("#### "):
            titles.append(line.removeprefix("#### ").strip())
    return titles

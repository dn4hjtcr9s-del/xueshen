"""知识图谱解析器单元测试（§16.1 / §23.1）。"""

from pathlib import Path

import pytest

from backend.memory.knowledge_graph.parser import (
    KnowledgeGraphParseError,
    parse_graph_file,
)
from backend.memory.knowledge_graph.registry import (
    derived_aliases,
    normalize_graph_title,
)

VALID_GRAPH = """# 测试图谱

```mermaid
flowchart LR
    subgraph g1["基础代数"]
        direction TB
        n001["第一章 有理数"]
        n002["第二章 代数式"]
    end
    subgraph g2["几何"]
        n010["第一章 三角形"]
    end
    n001 --> n002
    n002 --> n010
    classDef junior fill:#fff;
    class n001,n002 junior;
```
"""

CATALOG = """# 教材目录

#### 第一章 有理数

- 1.1 正数
"""


def _write(tmp_path: Path, graph: str = VALID_GRAPH, catalog: str = CATALOG) -> tuple[Path, Path]:
    g = tmp_path / "数学知识科技树关系图.md"
    c = tmp_path / "教材目录.md"
    g.write_text(graph, encoding="utf-8")
    c.write_text(catalog, encoding="utf-8")
    return g, c


def test_parse_valid_graph(tmp_path: Path) -> None:
    g, c = _write(tmp_path)
    parsed = parse_graph_file(g, c)
    assert len(parsed.nodes) == 3
    assert len(parsed.edges) == 2
    by_id = {n.node_id: n for n in parsed.nodes}
    assert by_id["n001"].group_key == "基础代数"
    assert by_id["n001"].stage == "junior"
    assert by_id["n010"].group_key == "几何"
    assert len(parsed.manifest_checksum) == 64


def test_dashed_edge_skipped_with_warning(tmp_path: Path) -> None:
    """裁决 A（2026-08-11）：虚线边跳过不入库，同步不失败。"""
    graph = VALID_GRAPH.replace(
        "    n001 --> n002", "    n001 --> n002\n    n001 -. 教材顺序 .-> n009"
    )
    g, c = _write(tmp_path, graph)
    parsed = parse_graph_file(g, c)
    assert len(parsed.skipped_dashed_edges) == 1
    assert all(e.to_node_id != "n009" for e in parsed.edges)  # 虚线边不入库


def test_dangling_edge_fails(tmp_path: Path) -> None:
    graph = VALID_GRAPH.replace("n002 --> n010", "n002 --> n099")
    g, c = _write(tmp_path, graph)
    with pytest.raises(KnowledgeGraphParseError, match="悬空边"):
        parse_graph_file(g, c)


def test_title_change_same_id_fails(tmp_path: Path) -> None:
    graph = VALID_GRAPH.replace("n002 --> n010", 'n002 --> n010\n        n001["不同的标题"]')
    g, c = _write(tmp_path, graph)
    with pytest.raises(KnowledgeGraphParseError, match="同一 ID 标题变化"):
        parse_graph_file(g, c)


def test_unrecognized_line_fails(tmp_path: Path) -> None:
    graph = VALID_GRAPH.replace("n002 --> n010", "n002 --> n010\n    click n001 href x")
    g, c = _write(tmp_path, graph)
    with pytest.raises(KnowledgeGraphParseError, match="无法识别"):
        parse_graph_file(g, c)


def test_normalize_graph_title() -> None:
    assert normalize_graph_title("第一章  有理数（上）") == "第一章 有理数(上)"
    assert normalize_graph_title("Ｌｉｍｉｔ　极限") == "limit 极限"


def test_derived_aliases() -> None:
    aliases = derived_aliases("第一章 有理数")
    assert normalize_graph_title("第一章 有理数") in aliases
    assert "有理数" in aliases

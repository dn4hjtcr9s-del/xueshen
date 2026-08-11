// 知识地图纯函数层（规格 §20.2）：dagre 确定性层次布局与四状态语义。
// 坐标只由后端固定边计算（后端不保存 x/y）；不展示百分比掌握度。
import dagre from "@dagrejs/dagre";
import type { GraphEdgeView, GraphNodeView, GraphStatus } from "../../api/memory";

export interface LayoutNode {
  node: GraphNodeView;
  x: number;
  y: number;
  width: number;
  height: number;
}

export interface GraphLayout {
  nodes: Map<string, LayoutNode>;
  width: number;
  height: number;
}

export const NODE_WIDTH = 132;
export const NODE_HEIGHT = 40;

/** dagre 确定性层次布局：输入全量固定节点与 prerequisite 边，输出节点坐标。 */
export function layoutGraph(nodes: GraphNodeView[], edges: GraphEdgeView[]): GraphLayout {
  const graph = new dagre.graphlib.Graph();
  graph.setGraph({ rankdir: "LR", nodesep: 26, ranksep: 72, marginx: 12, marginy: 12 });
  graph.setDefaultEdgeLabel(() => ({}));
  const ids = new Set(nodes.map((node) => node.node_id));
  for (const node of nodes) {
    graph.setNode(node.node_id, { width: NODE_WIDTH, height: NODE_HEIGHT });
  }
  for (const edge of edges) {
    if (ids.has(edge.from_node_id) && ids.has(edge.to_node_id)) {
      graph.setEdge(edge.from_node_id, edge.to_node_id);
    }
  }
  dagre.layout(graph);
  const layout = new Map<string, LayoutNode>();
  for (const node of nodes) {
    const position = graph.node(node.node_id);
    layout.set(node.node_id, {
      node,
      x: position.x,
      y: position.y,
      width: NODE_WIDTH,
      height: NODE_HEIGHT,
    });
  }
  const size = graph.graph();
  return { nodes: layout, width: size.width ?? 800, height: size.height ?? 480 };
}

export type DisplayStatus = GraphStatus | "none";

export function displayStatus(status: GraphStatus | null | undefined): DisplayStatus {
  return status ?? "none";
}

export const STATUS_LABEL: Record<DisplayStatus, string> = {
  none: "无状态",
  learning: "学习中",
  proficient: "熟练",
  expert: "精通",
};

export const STATUS_COLOR: Record<DisplayStatus, string> = {
  none: "var(--ink-faint)",
  learning: "var(--cinnabar)",
  proficient: "var(--gold)",
  expert: "var(--pine)",
};

/** §20.2：用户尝试点击“精通”时的固定提示文案，不得发送非法请求。 */
export const EXPERT_FORBIDDEN_MESSAGE =
  "精通状态由你长期的学习表现自动评估，不能手动设置。你可以继续学习、练习和讲解相关知识，系统会根据积累的学习证据更新状态。";

export interface GraphFilter {
  query: string;
  group: string | null;
  status: DisplayStatus | "all";
}

/** 筛选只控制可见节点与渲染范围，不改变已加载的完整图谱数据（§20.2）。 */
export function filterNodeIds(
  nodes: GraphNodeView[],
  overlays: Map<string, GraphStatus | null>,
  filter: GraphFilter,
): Set<string> {
  const query = filter.query.trim();
  const visible = new Set<string>();
  for (const node of nodes) {
    if (filter.group !== null && node.group_key !== filter.group) continue;
    if (query && !node.title.includes(query)) continue;
    if (filter.status !== "all" && displayStatus(overlays.get(node.node_id)) !== filter.status)
      continue;
    visible.add(node.node_id);
  }
  return visible;
}

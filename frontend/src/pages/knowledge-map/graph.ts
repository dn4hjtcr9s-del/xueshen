// 知识地图纯函数层：保留图谱语义，同时为交互式力导向图提供阶段与聚焦计算。
// 页面坐标只存在于浏览器会话中，后端仍只负责节点、边和学习状态。
import type { GraphEdgeView, GraphNodeView, GraphStatus } from "../../api/memory";

export type GraphStage = "junior" | "high" | "university" | "unknown";

export const STAGE_LABEL: Record<GraphStage, string> = {
  junior: "初中",
  high: "高中",
  university: "大学",
  unknown: "未标注",
};

/** 去掉教材章节编号和专题前缀，得到画布上更适合阅读的知识点名称。 */
export function displayNodeTitle(title: string): string {
  const normalized = title.trim();
  const withoutChapter = normalized.replace(
    /^\*?\s*第(?:[0-9]+|[一二三四五六七八九十百千万]+)章\s*/,
    "",
  );
  const withoutSpecialPrefix = withoutChapter.replace(/^(?:综合与实践|数学建模|数学探究)\s*/, "");
  const withoutStageSuffix = withoutSpecialPrefix.replace(/\s+(?:初中|高中|大学)$/, "");
  return withoutStageSuffix || normalized;
}

/** 将后端 metadata.stage 归一化为图谱的三个教育阶段。 */
export function graphStage(node: GraphNodeView): GraphStage {
  const value = String(node.metadata.stage ?? "").trim().toLowerCase();
  if (["junior", "初中", "middle", "middle_school"].includes(value)) return "junior";
  if (["high", "高中", "senior", "high_school"].includes(value)) return "high";
  if (["university", "大学", "college", "undergraduate"].includes(value)) return "university";
  return "unknown";
}

/**
 * 阶段软约束的目标纵坐标：图谱坐标原点在画布中心，大学在上、初中在下。
 * 这是力导向的偏好，不是把节点锁在几条横线上。
 */
export function stageTargetY(stage: GraphStage, viewportHeight: number): number {
  // 扩大三阶段的纵向呼吸带，让大图不会集中在画布下半部。
  const span = Math.max(210, viewportHeight * 0.60);
  if (stage === "university") return -span;
  if (stage === "junior") return span;
  return 0;
}

/**
 * 按有向先修关系计算知识进阶深度。深度越大表示越靠近知识树冠，
 * 用于在同一教育阶段内形成从基础到高阶的轻微纵向梯度。
 */
/** 找出只作为先修关系起点、没有任何入边的节点，用作学习起点提示。 */
export function initialNodeIds(
  nodes: GraphNodeView[],
  edges: GraphEdgeView[],
): Set<string> {
  const ids = new Set(nodes.map((node) => node.node_id));
  const incoming = new Set<string>();
  const outgoing = new Set<string>();
  for (const edge of edges) {
    if (!ids.has(edge.from_node_id) || !ids.has(edge.to_node_id)) continue;
    outgoing.add(edge.from_node_id);
    incoming.add(edge.to_node_id);
  }
  return new Set(nodes
    .filter((node) => outgoing.has(node.node_id) && !incoming.has(node.node_id))
    .map((node) => node.node_id));
}

export function progressionDepths(
  nodes: GraphNodeView[],
  edges: GraphEdgeView[],
): Map<string, number> {
  const ids = new Set(nodes.map((node) => node.node_id));
  const indegree = new Map<string, number>(nodes.map((node) => [node.node_id, 0]));
  const successors = new Map<string, string[]>();
  for (const edge of edges) {
    if (!ids.has(edge.from_node_id) || !ids.has(edge.to_node_id)) continue;
    const next = successors.get(edge.from_node_id) ?? [];
    next.push(edge.to_node_id);
    successors.set(edge.from_node_id, next);
    indegree.set(edge.to_node_id, (indegree.get(edge.to_node_id) ?? 0) + 1);
  }

  const depths = new Map<string, number>(nodes.map((node) => [node.node_id, 0]));
  const queue = nodes.filter((node) => indegree.get(node.node_id) === 0).map((node) => node.node_id);
  for (let index = 0; index < queue.length; index += 1) {
    const current = queue[index];
    const depth = depths.get(current) ?? 0;
    for (const successor of successors.get(current) ?? []) {
      depths.set(successor, Math.max(depths.get(successor) ?? 0, depth + 1));
      const remaining = (indegree.get(successor) ?? 1) - 1;
      indegree.set(successor, remaining);
      if (remaining === 0) queue.push(successor);
    }
  }
  return depths;
}

/** 将图谱深度叠加到阶段带上，形成大致的树根—树冠轮廓。 */
export function progressionTargetY(
  stage: GraphStage,
  depth: number,
  maxDepth: number,
  viewportHeight: number,
): number {
  const stageY = stageTargetY(stage, viewportHeight);
  if (maxDepth <= 0) return stageY;
  const depthOffset = ((maxDepth / 2 - depth) / maxDepth) * Math.min(76, viewportHeight * 0.14);
  return stageY + depthOffset;
}

export interface GraphBbox {
  x: [number, number];
  y: [number, number];
}

/** 将视口中心限制在图谱范围内，防止平移后只剩空白区域。 */
export function clampGraphCenter(
  center: { x: number; y: number },
  zoom: number,
  bbox: GraphBbox,
  viewport: { width: number; height: number },
  paddingPx = 48,
): { x: number; y: number } {
  if (
    !Number.isFinite(center.x) ||
    !Number.isFinite(center.y) ||
    !Number.isFinite(zoom) ||
    zoom <= 0 ||
    viewport.width <= 0 ||
    viewport.height <= 0 ||
    !bbox.x.every(Number.isFinite) ||
    !bbox.y.every(Number.isFinite)
  ) {
    return center;
  }

  const padding = Math.max(0, paddingPx) / zoom;
  const halfViewport = {
    x: viewport.width / (2 * zoom),
    y: viewport.height / (2 * zoom),
  };
  const clampAxis = (value: number, min: number, max: number, halfSize: number): number => {
    const paddedMin = min - padding;
    const paddedMax = max + padding;
    if (paddedMax - paddedMin <= halfSize * 2) return (paddedMin + paddedMax) / 2;
    return Math.min(paddedMax - halfSize, Math.max(paddedMin + halfSize, value));
  };

  return {
    x: clampAxis(center.x, bbox.x[0], bbox.x[1], halfViewport.x),
    y: clampAxis(center.y, bbox.y[0], bbox.y[1], halfViewport.y),
  };
}

export type FocusedLinkDirection = "incoming" | "outgoing" | null;

/** 相对当前焦点判断边的方向：入边是前置知识，出边是下一步学习方向。 */
export function focusedLinkDirection(
  edge: GraphEdgeView,
  focusId: string | null,
): FocusedLinkDirection {
  if (!focusId) return null;
  if (edge.to_node_id === focusId) return "incoming";
  if (edge.from_node_id === focusId) return "outgoing";
  return null;
}

/** 以无向关系计算节点距当前焦点的层级，方便 hover/click 时逐层降亮。 */
export function focusDepths(
  edges: GraphEdgeView[],
  focusId: string | null,
  maxDepth = 3,
): Map<string, number> {
  const depths = new Map<string, number>();
  if (!focusId) return depths;
  const adjacent = new Map<string, string[]>();
  for (const edge of edges) {
    const from = adjacent.get(edge.from_node_id) ?? [];
    const to = adjacent.get(edge.to_node_id) ?? [];
    from.push(edge.to_node_id);
    to.push(edge.from_node_id);
    adjacent.set(edge.from_node_id, from);
    adjacent.set(edge.to_node_id, to);
  }
  const queue = [focusId];
  depths.set(focusId, 0);
  while (queue.length > 0) {
    const current = queue.shift()!;
    const depth = depths.get(current)!;
    if (depth >= maxDepth) continue;
    for (const next of adjacent.get(current) ?? []) {
      if (depths.has(next)) continue;
      depths.set(next, depth + 1);
      queue.push(next);
    }
  }
  return depths;
}

/** 关系越远越暗；无焦点时所有节点恢复正常显示。 */
export function focusOpacity(depth: number | undefined): number {
  if (depth === undefined) return 0.12;
  if (depth === 0) return 1;
  if (depth === 1) return 0.88;
  if (depth === 2) return 0.58;
  return 0.3;
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
    if (query && !node.title.includes(query) && !displayNodeTitle(node.title).includes(query)) continue;
    if (filter.status !== "all" && displayStatus(overlays.get(node.node_id)) !== filter.status)
      continue;
    visible.add(node.node_id);
  }
  return visible;
}

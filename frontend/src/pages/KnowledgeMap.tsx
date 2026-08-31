// 知识地图页：使用真实节点与边驱动的交互式力导向图。
// 本模块只重构可视化层：学习状态、节点详情和后端图谱语义保持不变。
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { forceCollide, forceY } from "d3-force";
import ForceGraph2D, { type ForceGraphMethods } from "react-force-graph-2d";
import { LocateFixed, Maximize2, MessageCircle, RotateCcw, Search, X } from "lucide-react";
import {
  clearGraphState,
  getGraphStateExplanation,
  getKnowledgeGraph,
  getMyGraphStates,
  MemoryApiError,
  setGraphState,
  type GraphEdgeView,
  type GraphOverlayView,
  type GraphStateExplanation,
  type GraphStatus,
  type GraphNodeView,
  type KnowledgeGraphSnapshot,
} from "../api/memory";
import { isTerminalStatus, useOperationPolling } from "../api/operations";
import {
  clampGraphCenter,
  displayNodeTitle,
  displayStatus,
  EXPERT_FORBIDDEN_MESSAGE,
  filterNodeIds,
  focusDepths,
  focusedLinkDirection,
  focusOpacity,
  graphStage,
  initialNodeIds,
  progressionDepths,
  progressionTargetY,
  STAGE_LABEL,
  STATUS_COLOR,
  STATUS_LABEL,
  type DisplayStatus,
  type GraphStage,
} from "./knowledge-map/graph";

type VisualNode = GraphNodeView & {
  id: string;
  stage: GraphStage;
  degree: number;
  progressionDepth: number;
  isInitial: boolean;
  x?: number;
  y?: number;
  vx?: number;
  vy?: number;
  fx?: number;
  fy?: number;
};

type VisualLink = GraphEdgeView & {
  source: string | VisualNode;
  target: string | VisualNode;
};

type GraphRef = ForceGraphMethods<VisualNode, VisualLink>;
type Viewport = { width: number; height: number };

const STATUS_FILTERS: { value: DisplayStatus | "all"; label: string }[] = [
  { value: "all", label: "全部状态" },
  { value: "none", label: "无状态" },
  { value: "learning", label: "学习中" },
  { value: "proficient", label: "熟练" },
  { value: "expert", label: "精通" },
];

// Canvas 无法直接读取 CSS 变量，绘图层使用与设计系统对应的稳定色值。
const GRAPH_COLORS: Record<DisplayStatus, string> = {
  none: "#9a8f7a",
  learning: "#bd321a",
  proficient: "#a87d23",
  expert: "#2e6b54",
};
const GRAPH_BACKGROUND = "#f4eee1";
// 没有任何入边的先修起点固定使用绿色，提示学习路径从这里开始。
const GRAPH_INITIAL_COLOR = "#2e8b68";
// 聚焦节点时，入边表示已经掌握的前置知识，出边表示下一步学习方向。
const GRAPH_INCOMING_COLOR = "#4f9f75";
const GRAPH_OUTGOING_COLOR = "#c79536";
const GRAPH_NEUTRAL_COLOR = "#6b6151";
// 视口边缘保留固定安全区；适应全图时使用同一数值，保证初始视图位于中心。
const GRAPH_VIEW_PADDING = 48;

function colorWithAlpha(hex: string, alpha: number): string {
  const value = hex.replace("#", "");
  const red = Number.parseInt(value.slice(0, 2), 16);
  const green = Number.parseInt(value.slice(2, 4), 16);
  const blue = Number.parseInt(value.slice(4, 6), 16);
  return `rgba(${red}, ${green}, ${blue}, ${Math.max(0, Math.min(1, alpha))})`;
}

function endpointId(endpoint: string | VisualNode | undefined): string {
  return typeof endpoint === "object" && endpoint !== null ? endpoint.node_id : String(endpoint ?? "");
}

function roundRect(
  context: CanvasRenderingContext2D,
  x: number,
  y: number,
  width: number,
  height: number,
  radius: number,
) {
  const r = Math.min(radius, width / 2, height / 2);
  context.beginPath();
  context.moveTo(x + r, y);
  context.arcTo(x + width, y, x + width, y + height, r);
  context.arcTo(x + width, y + height, x, y + height, r);
  context.arcTo(x, y + height, x, y, r);
  context.arcTo(x, y, x + r, y, r);
  context.closePath();
}

/** 将完整知识点名称按画布宽度换行，不用省略号截断名称。 */
function wrapLabel(context: CanvasRenderingContext2D, title: string, maxWidth: number): string[] {
  const lines: string[] = [];
  let line = "";
  for (const character of title) {
    const candidate = `${line}${character}`;
    if (line && context.measureText(candidate).width > maxWidth) {
      lines.push(line);
      line = character;
    } else {
      line = candidate;
    }
  }
  if (line) lines.push(line);
  return lines.length > 0 ? lines : [title];
}

function supportsCanvas(): boolean {
  if (typeof document === "undefined") return false;
  // jsdom 没有真实 Canvas 实现，测试时直接走无 Canvas 的可访问代理列表。
  if (typeof navigator !== "undefined" && navigator.userAgent.includes("jsdom")) return false;
  try {
    const canvas = document.createElement("canvas");
    return Boolean(canvas.getContext("2d"));
  } catch {
    return false;
  }
}

export function KnowledgeMapPage({ goChat }: { goChat: () => void }) {
  const [snapshot, setSnapshot] = useState<KnowledgeGraphSnapshot | null>(null);
  const [overlays, setOverlays] = useState<Map<string, GraphOverlayView>>(new Map());
  const [loadError, setLoadError] = useState<string | null>(null);
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [hoveredId, setHoveredId] = useState<string | null>(null);
  const [query, setQuery] = useState("");
  const [group, setGroup] = useState<string | null>(null);
  const [statusFilter, setStatusFilter] = useState<DisplayStatus | "all">("all");
  const [operationId, setOperationId] = useState<string | null>(null);
  const [actionError, setActionError] = useState<string | null>(null);
  const [expertNotice, setExpertNotice] = useState(false);
  const [explanation, setExplanation] = useState<GraphStateExplanation | null>(null);
  const [viewport, setViewport] = useState<Viewport>({ width: 1100, height: 620 });
  const [canvasSupported, setCanvasSupported] = useState(false);
  const viewportRef = useRef<HTMLDivElement>(null);
  const graphRef = useRef<GraphRef | undefined>(undefined);
  const positionCacheRef = useRef(new Map<string, Pick<VisualNode, "x" | "y" | "fx" | "fy">>());
  const didFitRef = useRef(false);
  const constrainingPanRef = useRef(false);
  const polling = useOperationPolling(operationId);

  const reloadOverlays = useCallback(async () => {
    const views = await getMyGraphStates();
    setOverlays(new Map(views.map((view) => [view.node_id, view])));
  }, []);

  useEffect(() => {
    let cancelled = false;
    (async () => {
      try {
        const [graph, views] = await Promise.all([getKnowledgeGraph(), getMyGraphStates()]);
        if (cancelled) return;
        setSnapshot(graph);
        setOverlays(new Map(views.map((view) => [view.node_id, view])));
      } catch (error) {
        if (!cancelled) {
          setLoadError(
            error instanceof MemoryApiError ? error.message : "知识图谱加载失败，请稍后重试。",
          );
        }
      }
    })();
    return () => {
      cancelled = true;
    };
  }, []);

  useEffect(() => {
    setCanvasSupported(supportsCanvas());
  }, []);

  useEffect(() => {
    const element = viewportRef.current;
    if (!element) return;
    const measure = () => {
      const rect = element.getBoundingClientRect();
      setViewport({
        width: Math.max(320, Math.round(rect.width || 1100)),
        height: Math.max(480, Math.round(rect.height || 620)),
      });
    };
    measure();
    if (typeof ResizeObserver === "undefined") return;
    const observer = new ResizeObserver(measure);
    observer.observe(element);
    return () => observer.disconnect();
  }, [snapshot]);

  // 写操作完成后以服务端 Overlay 为准刷新。
  useEffect(() => {
    const result = polling.result;
    if (!result || !isTerminalStatus(result.status)) return;
    setOperationId(null);
    if (result.status === "succeeded") {
      void reloadOverlays();
    } else if (result.status !== "cancelled") {
      setActionError(result.error?.message ?? "操作未能完成，状态已回滚。");
      void reloadOverlays();
    }
  }, [polling.result, reloadOverlays]);

  const groups = useMemo(
    () =>
      snapshot
        ? [
            ...new Set(
              snapshot.nodes
                .map((node) => node.group_key)
                .filter((value): value is string => value !== null),
            ),
          ].sort()
        : [],
    [snapshot],
  );
  const statusMap = useMemo(() => {
    const map = new Map<string, GraphStatus | null>();
    for (const node of snapshot?.nodes ?? []) {
      map.set(node.node_id, overlays.get(node.node_id)?.status ?? null);
    }
    return map;
  }, [snapshot, overlays]);
  const visibleIds = useMemo(
    () =>
      snapshot
        ? filterNodeIds(snapshot.nodes, statusMap, { query, group, status: statusFilter })
        : new Set<string>(),
    [snapshot, statusMap, query, group, statusFilter],
  );
  const nodeById = useMemo(
    () => new Map((snapshot?.nodes ?? []).map((node) => [node.node_id, node])),
    [snapshot],
  );
  const visibleNodes = useMemo<VisualNode[]>(() => {
    if (!snapshot) return [];
    const degree = new Map<string, number>();
    const depths = progressionDepths(snapshot.nodes, snapshot.edges);
    const initialIds = initialNodeIds(snapshot.nodes, snapshot.edges);
    const maxDepth = Math.max(0, ...depths.values());
    for (const edge of snapshot.edges) {
      degree.set(edge.from_node_id, (degree.get(edge.from_node_id) ?? 0) + 1);
      degree.set(edge.to_node_id, (degree.get(edge.to_node_id) ?? 0) + 1);
    }
    return snapshot.nodes
      .filter((node) => visibleIds.has(node.node_id))
      .map((node) => {
        const stage = graphStage(node);
        const progressionDepth = depths.get(node.node_id) ?? 0;
        return {
          ...node,
          id: node.node_id,
          stage,
          degree: degree.get(node.node_id) ?? 0,
          progressionDepth,
          isInitial: initialIds.has(node.node_id),
          y: progressionTargetY(stage, progressionDepth, maxDepth, viewport.height),
          ...positionCacheRef.current.get(node.node_id),
        };
      });
  }, [snapshot, visibleIds, viewport.height]);
  const visibleNodeIds = useMemo(
    () => new Set(visibleNodes.map((node) => node.node_id)),
    [visibleNodes],
  );
  const visibleLinks = useMemo<VisualLink[]>(
    () =>
      (snapshot?.edges ?? [])
        .filter((edge) => visibleNodeIds.has(edge.from_node_id) && visibleNodeIds.has(edge.to_node_id))
        .map((edge) => ({ ...edge, source: edge.from_node_id, target: edge.to_node_id })),
    [snapshot, visibleNodeIds],
  );
  const graphData = useMemo(
    () => ({ nodes: visibleNodes, links: visibleLinks }),
    [visibleNodes, visibleLinks],
  );
  const focusId = hoveredId ?? selectedId;
  const focusedDepths = useMemo(
    () => focusDepths(snapshot?.edges ?? [], focusId),
    [snapshot, focusId],
  );
  const selected = selectedId ? (nodeById.get(selectedId) ?? null) : null;
  const selectedOverlay = selectedId ? overlays.get(selectedId) : undefined;
  const hovered = hoveredId ? (nodeById.get(hoveredId) ?? null) : null;

  useEffect(() => {
    if (selectedId && !visibleIds.has(selectedId)) {
      setSelectedId(null);
      setExplanation(null);
    }
    if (hoveredId && !visibleIds.has(hoveredId)) {
      setHoveredId(null);
    }
  }, [hoveredId, selectedId, visibleIds]);

  // 阶段力把大学推向上方、初中推向下方；碰撞力只负责留出呼吸空间。
  useEffect(() => {
    if (!canvasSupported || !graphRef.current || visibleNodes.length === 0) return;
    const stageForce = forceY<VisualNode>((node) =>
      progressionTargetY(
        node.stage,
        node.progressionDepth,
        Math.max(0, ...visibleNodes.map((item) => item.progressionDepth)),
        viewport.height,
      ),
    ).strength(0.16);
    // 预留比节点本体更大的缓冲区，避免高连接度节点在视觉上挤成一团。
    const collisionForce = forceCollide<VisualNode>(
      (node) =>
        24 +
        Math.sqrt(node.degree + 1) * 3.2 +
        Math.min(72, [...displayNodeTitle(node.title)].length * 2.8),
    ).strength(0.98);
    graphRef.current.d3Force("stage", stageForce);
    graphRef.current.d3Force("collision", collisionForce);
    const chargeForce = graphRef.current.d3Force("charge") as
      | { strength?: (value: number) => unknown; distanceMax?: (value: number) => unknown }
      | undefined;
    // 让相距较远的分支也保持排斥，避免多个子图挤在同一小块区域。
    chargeForce?.strength?.(-340);
    chargeForce?.distanceMax?.(Math.max(viewport.width, viewport.height) * 1.25);
    const linkForce = graphRef.current.d3Force("link") as
      | { distance?: (value: number | ((link: VisualLink) => number)) => unknown; strength?: (value: number) => unknown }
      | undefined;
    // 关系线故意拉长，并降低束缚强度，让真实关系决定结构、力导向负责留白。
    linkForce?.distance?.((link: VisualLink) => (link.relation_type === "prerequisite" ? 164 : 180));
    linkForce?.strength?.(0.28);
    graphRef.current.d3ReheatSimulation();
    // 阶段力重新稳定后再适应一次视图，避免初次布局把树冠或树根推到画布外。
    const fitTimer = window.setTimeout(
      () => graphRef.current?.zoomToFit(700, GRAPH_VIEW_PADDING),
      2600,
    );
    return () => window.clearTimeout(fitTimer);
  }, [canvasSupported, viewport.height, viewport.width, visibleNodes]);

  useEffect(() => {
    if (canvasSupported) graphRef.current?.resumeAnimation();
  }, [canvasSupported, focusId, overlays, statusFilter]);

  const selectNode = (nodeId: string) => {
    setSelectedId(nodeId);
    setExpertNotice(false);
    setExplanation(null);
  };

  const resetLayout = () => {
    positionCacheRef.current.clear();
    for (const node of graphData.nodes) {
      delete node.x;
      delete node.y;
      delete node.vx;
      delete node.vy;
      delete node.fx;
      delete node.fy;
    }
    didFitRef.current = false;
    graphRef.current?.d3ReheatSimulation();
  };

  const fitGraph = () => {
    graphRef.current?.zoomToFit(500, GRAPH_VIEW_PADDING);
  };

  const constrainGraphPan = useCallback(
    (transform: { k: number; x: number; y: number }) => {
      if (constrainingPanRef.current || !graphRef.current) return;
      const bbox = graphRef.current.getGraphBbox();
      if (!bbox) return;
      const bounded = clampGraphCenter(
        { x: transform.x, y: transform.y },
        transform.k,
        bbox,
        viewport,
        GRAPH_VIEW_PADDING,
      );
      if (Math.abs(bounded.x - transform.x) < 0.5 && Math.abs(bounded.y - transform.y) < 0.5) return;
      constrainingPanRef.current = true;
      graphRef.current.centerAt(bounded.x, bounded.y);
      constrainingPanRef.current = false;
    },
    [viewport],
  );

  const focusSelected = () => {
    if (!selectedId || !graphRef.current) return;
    const node = graphData.nodes.find((item) => item.node_id === selectedId);
    if (node?.x !== undefined && node.y !== undefined) {
      graphRef.current.centerAt(node.x, node.y, 500);
      graphRef.current.zoom(2.2, 500);
    }
  };

  const submitStateAction = async (
    nodeId: string,
    action: "mark_unfamiliar" | "mark_familiar" | "clear",
  ) => {
    const previous = overlays.get(nodeId);
    const optimisticStatus: GraphStatus | null =
      action === "mark_unfamiliar" ? "learning" : action === "mark_familiar" ? "proficient" : null;
    // 乐观 UI：先更新本地 Overlay，失败时回滚（§20.3）。
    setOverlays((prev) => {
      const next = new Map(prev);
      next.set(nodeId, {
        node_id: nodeId,
        status: optimisticStatus,
        version: previous?.version ?? null,
        status_source: "user",
        updated_at: new Date().toISOString(),
      });
      return next;
    });
    setActionError(null);
    setExpertNotice(false);
    try {
      const expectedVersion = previous?.version ?? null;
      const operation =
        action === "clear"
          ? await clearGraphState(nodeId, expectedVersion)
          : await setGraphState(nodeId, action, expectedVersion);
      if (isTerminalStatus(operation.status)) {
        if (operation.status === "succeeded") await reloadOverlays();
        else throw new MemoryApiError(500, operation.error ?? undefined, "操作未能完成");
      } else {
        setOperationId(operation.operation_id);
      }
    } catch (error) {
      setOverlays((prev) => {
        const next = new Map(prev);
        if (previous) next.set(nodeId, previous);
        else next.delete(nodeId);
        return next;
      });
      if (error instanceof MemoryApiError && error.status === 409) {
        setActionError("状态已被更新，请查看最新状态后重新操作。");
        void reloadOverlays();
      } else {
        setActionError(error instanceof MemoryApiError ? error.message : "网络错误，状态已回滚。");
      }
    }
  };

  const showExplanation = async (nodeId: string) => {
    try {
      setExplanation(await getGraphStateExplanation(nodeId));
    } catch {
      setExplanation(null);
    }
  };

  const prerequisitesOf = (nodeId: string) =>
    (snapshot?.edges ?? []).filter((edge) => edge.to_node_id === nodeId).map((edge) => edge.from_node_id);
  const successorsOf = (nodeId: string) =>
    (snapshot?.edges ?? []).filter((edge) => edge.from_node_id === nodeId).map((edge) => edge.to_node_id);

  if (loadError) {
    return (
      <div className="knowledge-map-page">
        <div className="memory-banner" role="alert">
          <span>{loadError}</span>
        </div>
      </div>
    );
  }
  if (!snapshot) {
    return (
      <div className="knowledge-map-page">
        <div className="knowledge-map-loading">知识图谱加载中…</div>
      </div>
    );
  }

  return (
    <div className="knowledge-map-page">
      <div className="knowledge-map-toolbar rise">
        <div className="knowledge-map-toolbar-copy">
          <span className="knowledge-map-kicker">01 / KNOWLEDGE ATLAS</span>
          <strong>关系由知识点决定，阶段只提供方向感</strong>
          <span>{snapshot.nodes.length} 个节点 · {snapshot.edges.length} 条先修关系</span>
        </div>
        <div className="knowledge-map-filters">
          <label className="knowledge-map-search">
            <Search size={14} aria-hidden="true" />
            <input
              aria-label="搜索节点"
              placeholder="搜索知识点…"
              value={query}
              onChange={(event) => setQuery(event.target.value)}
            />
            {query && (
              <button type="button" className="knowledge-map-clear" aria-label="清除搜索" onClick={() => setQuery("")}>
                <X size={13} />
              </button>
            )}
          </label>
          <select
            aria-label="按分组筛选"
            value={group ?? ""}
            onChange={(event) => setGroup(event.target.value || null)}
          >
            <option value="">全部分组</option>
            {groups.map((value) => <option key={value} value={value}>{value}</option>)}
          </select>
          <select
            aria-label="按状态筛选"
            value={statusFilter}
            onChange={(event) => setStatusFilter(event.target.value as DisplayStatus | "all")}
          >
            {STATUS_FILTERS.map((option) => (
              <option key={option.value} value={option.value}>{option.label}</option>
            ))}
          </select>
        </div>
        <div className="knowledge-map-tools">
          <button type="button" className="knowledge-map-tool" onClick={fitGraph} title="适应全部节点">
            <Maximize2 size={14} /> 适应全图
          </button>
          <button type="button" className="knowledge-map-tool" onClick={resetLayout} title="重置布局">
            <RotateCcw size={14} /> 重置布局
          </button>
          {selectedId && (
            <button type="button" className="knowledge-map-tool" onClick={focusSelected} title="聚焦选中节点">
              <LocateFixed size={14} /> 聚焦节点
            </button>
          )}
        </div>
      </div>

      <div className="knowledge-map-viewport" ref={viewportRef}>
        <div className="knowledge-map-stage">
          <div className="knowledge-map-stage-wash" aria-hidden="true" />
          <div className="knowledge-map-stage-labels" aria-hidden="true">
            <span><i />大学</span>
            <span><i />高中</span>
            <span><i />初中</span>
          </div>
          {canvasSupported ? (
            <ForceGraph2D<VisualNode, VisualLink>
              ref={graphRef}
              width={viewport.width}
              height={viewport.height}
              graphData={graphData}
              backgroundColor={GRAPH_BACKGROUND}
              nodeId="id"
              nodeRelSize={1}
              nodeVal={(node) => 5 + Math.sqrt(node.degree + 1)}
              // Canvas 标签显示去掉章节编号后的完整知识点名称；阶段信息保留在悬停卡片中。
              nodeLabel={(node) => displayNodeTitle(node.title)}
              // 最小值仍有限制，但必须低于 133 节点全景所需比例，避免全图被裁切。
              minZoom={0.2}
              maxZoom={3.25}
              nodePointerAreaPaint={(node, paintColor, context) => {
                const radius = 13 + Math.sqrt(node.degree + 1) * 2.4;
                context.fillStyle = paintColor;
                context.beginPath();
                context.arc(node.x ?? 0, node.y ?? 0, radius, 0, 2 * Math.PI, false);
                context.fill();
              }}
              nodeCanvasObject={(node, context, globalScale) => {
                const nodeId = node.node_id;
                const status = displayStatus(statusMap.get(nodeId));
                const color = node.isInitial ? GRAPH_INITIAL_COLOR : GRAPH_COLORS[status];
                const depth = focusedDepths.get(nodeId);
                const opacity = focusId ? focusOpacity(depth) : 0.9;
                const active = nodeId === focusId;
                const selectedNode = nodeId === selectedId;
                const shouldGlow = active || selectedNode;
                const radius = (5.8 + Math.sqrt(node.degree + 1) * 1.35) * (active ? 1.82 : selectedNode ? 1.48 : 1);
                const x = node.x ?? 0;
                const y = node.y ?? 0;

                context.save();
                context.globalAlpha = opacity;
                if (shouldGlow) {
                  const glow = context.createRadialGradient(x, y, radius * 0.3, x, y, radius * (active ? 4.2 : 3.2));
                  glow.addColorStop(0, colorWithAlpha(color, active ? 0.82 : 0.62));
                  glow.addColorStop(0.34, colorWithAlpha(color, active ? 0.34 : 0.22));
                  glow.addColorStop(1, colorWithAlpha(color, 0));
                  context.fillStyle = glow;
                  context.beginPath();
                  context.arc(x, y, radius * (active ? 4.2 : 3.2), 0, 2 * Math.PI, false);
                  context.fill();
                }
                context.shadowColor = colorWithAlpha(color, active ? 0.84 : selectedNode ? 0.62 : 0.2);
                context.shadowBlur = active ? 20 : selectedNode ? 13 : 4;
                context.fillStyle = colorWithAlpha(color, active ? 1 : 0.82);
                context.beginPath();
                context.arc(x, y, radius, 0, 2 * Math.PI, false);
                context.fill();
                context.shadowBlur = 0;
                context.strokeStyle = colorWithAlpha("#fffaf0", active ? 0.94 : 0.62);
                context.lineWidth = active ? 1.8 : 1;
                context.stroke();
                if (selectedNode && !active) {
                  context.strokeStyle = colorWithAlpha(color, 0.88);
                  context.lineWidth = 2;
                  context.beginPath();
                  context.arc(x, y, radius + 4, 0, 2 * Math.PI, false);
                  context.stroke();
                }
                // 每个节点都显示完整知识点名称；长名称换行，只有高亮节点使用标签底色强调。
                const screenFontSize = Math.max(7.5, Math.min(13, 8 + Math.max(0, globalScale - 0.55) * 3));
                context.font = `600 ${screenFontSize / globalScale}px -apple-system, BlinkMacSystemFont, "PingFang SC", sans-serif`;
                const label = displayNodeTitle(node.title);
                const lines = wrapLabel(context, label, Math.min(260, 150 / globalScale));
                const lineHeight = Math.max(12, 13 / globalScale);
                const labelWidth = Math.max(...lines.map((line) => context.measureText(line).width)) + 12;
                const labelHeight = lines.length * lineHeight + 6;
                const leftEdge = -viewport.width / (2 * globalScale);
                const rightEdge = viewport.width / (2 * globalScale);
                const rightLabelX = x + radius + 8;
                const leftLabelX = x - radius - labelWidth - 8;
                // 画布坐标以中心为原点；优先把标签放到右侧，右缘不足时换到左侧。
                const labelOnRight = rightLabelX + labelWidth <= rightEdge - 10 || leftLabelX < leftEdge + 10;
                const labelX = labelOnRight ? rightLabelX : leftLabelX;
                const labelY = y - labelHeight / 2;
                if (active) {
                  context.fillStyle = colorWithAlpha("#fbf7ec", 0.96);
                  roundRect(context, labelX, labelY, labelWidth, labelHeight, 5);
                  context.fill();
                  context.strokeStyle = colorWithAlpha(color, 0.72);
                  context.lineWidth = 1 / globalScale;
                  context.stroke();
                }
                context.textBaseline = "middle";
                context.textAlign = "left";
                for (const [index, line] of lines.entries()) {
                  const lineY = labelY + 3 + lineHeight / 2 + index * lineHeight;
                  // 非高亮标签使用背景描边，保证在关系线和浅色背景上仍然完整可读。
                  context.lineWidth = (active ? 0 : 2.6) / globalScale;
                  context.strokeStyle = colorWithAlpha(GRAPH_BACKGROUND, active ? 0 : Math.min(0.95, opacity + 0.18));
                  context.strokeText(line, labelX + 6, lineY);
                  context.fillStyle = colorWithAlpha(active ? "#221c12" : "#4d4639", Math.min(0.95, opacity + 0.08));
                  context.fillText(line, labelX + 6, lineY);
                }
                context.restore();
              }}
              linkColor={(link) => {
                const from = endpointId(link.source);
                const to = endpointId(link.target);
                if (!focusId) return colorWithAlpha(GRAPH_NEUTRAL_COLOR, 0.42);
                const direction = focusedLinkDirection(link, focusId);
                const depth = Math.min(focusedDepths.get(from) ?? 4, focusedDepths.get(to) ?? 4);
                const color = direction === "incoming"
                  ? GRAPH_INCOMING_COLOR
                  : direction === "outgoing"
                    ? GRAPH_OUTGOING_COLOR
                    : GRAPH_NEUTRAL_COLOR;
                return colorWithAlpha(color, direction ? 0.98 : Math.max(0.08, focusOpacity(depth) * 0.58));
              }}
              linkCanvasObjectMode={(link) =>
                focusedLinkDirection(link, focusId) ? "before" : undefined
              }
              linkCanvasObject={(link, context, globalScale) => {
                const direction = focusedLinkDirection(link, focusId);
                if (!direction) return;
                const source = typeof link.source === "object" ? link.source : null;
                const target = typeof link.target === "object" ? link.target : null;
                if (!source || !target || source.x === undefined || source.y === undefined || target.x === undefined || target.y === undefined) return;
                const color = direction === "incoming" ? GRAPH_INCOMING_COLOR : GRAPH_OUTGOING_COLOR;
                context.save();
                context.globalAlpha = 0.95;
                context.strokeStyle = color;
                context.shadowColor = direction === "incoming"
                  ? "rgba(79, 190, 126, 0.92)"
                  : "rgba(231, 181, 61, 0.92)";
                // 使用长虚线而不是短点线；短 dash 与圆角阴影叠加后会形成不规则圆点。
                context.shadowBlur = 10 / globalScale;
                context.lineWidth = 3.2 / globalScale;
                context.lineCap = "butt";
                context.lineJoin = "round";
                context.setLineDash([11 / globalScale, 7 / globalScale]);
                context.beginPath();
                context.moveTo(source.x, source.y);
                context.lineTo(target.x, target.y);
                context.stroke();
                context.restore();
              }}
              linkWidth={(link) =>
                focusedLinkDirection(link, focusId) ? 2.4 : 0.75
              }
              linkDirectionalArrowLength={4}
              linkDirectionalArrowRelPos={0.96}
              linkDirectionalArrowColor={(link) => {
                const direction = focusedLinkDirection(link, focusId);
                const color = direction === "incoming"
                  ? GRAPH_INCOMING_COLOR
                  : direction === "outgoing"
                    ? GRAPH_OUTGOING_COLOR
                    : GRAPH_NEUTRAL_COLOR;
                return colorWithAlpha(color, direction ? 0.98 : 0.3);
              }}
              // 高亮边与荧光层使用同一组长虚线，避免底层短点线叠加出块状效果。
              linkLineDash={(link) =>
                focusedLinkDirection(link, focusId) ? [11, 7] : [3, 4]
              }
              warmupTicks={240}
              cooldownTicks={220}
              cooldownTime={9000}
              d3AlphaDecay={0.028}
              d3VelocityDecay={0.36}
              enableNodeDrag
              enableZoomInteraction
              enablePanInteraction
              showPointerCursor
              onZoom={constrainGraphPan}
              onZoomEnd={constrainGraphPan}
              onNodeHover={(node) => setHoveredId(node ? node.node_id : null)}
              onNodeClick={(node) => selectNode(node.node_id)}
              onNodeDrag={(node) => {
                positionCacheRef.current.set(node.node_id, { x: node.x, y: node.y, fx: node.x, fy: node.y });
              }}
              onNodeDragEnd={(node) => {
                // 拖动位置只固定在本次页面会话中，不写回后端。
                node.fx = node.x;
                node.fy = node.y;
                positionCacheRef.current.set(node.node_id, { x: node.x, y: node.y, fx: node.x, fy: node.y });
              }}
              onBackgroundClick={() => {
                setSelectedId(null);
                setHoveredId(null);
                setExplanation(null);
                setExpertNotice(false);
              }}
              onEngineStop={() => {
                if (!didFitRef.current) {
                  didFitRef.current = true;
                  graphRef.current?.zoomToFit(800, 28);
                }
              }}
            />
          ) : (
            <div className="knowledge-map-fallback" role="status">
              <strong>当前浏览器不支持 Canvas 图谱</strong>
              <span>请使用下方知识点列表继续查看和操作。</span>
              <div>
                {visibleNodes.map((node) => (
                  <button key={node.node_id} type="button" onClick={() => selectNode(node.node_id)}>
                    {node.title}
                  </button>
                ))}
              </div>
            </div>
          )}

          <div className="knowledge-map-accessibility" aria-label="知识点列表">
            {visibleNodes.map((node) => (
              <button
                key={node.node_id}
                type="button"
                className="knowledge-map-node-proxy"
                data-testid={`node-${node.node_id}`}
                data-status={displayStatus(statusMap.get(node.node_id))}
                aria-label={`知识点：${displayNodeTitle(node.title)}`}
                onClick={() => selectNode(node.node_id)}
              >
                {displayNodeTitle(node.title)}
              </button>
            ))}
          </div>

          {hovered && (
            <div className="knowledge-map-hover-card" role="status">
              <span className="knowledge-map-hover-overline">当前悬停 · {STAGE_LABEL[graphStage(hovered)]}</span>
              <strong>{displayNodeTitle(hovered.title)}</strong>
              <span>{hovered.group_key ?? "未分组"} · {STATUS_LABEL[displayStatus(statusMap.get(hovered.node_id))]}</span>
            </div>
          )}

          <div className="knowledge-map-legend">
            <span><i style={{ background: GRAPH_INITIAL_COLOR }} />学习起点</span>
            <span><i className="knowledge-map-legend-line" style={{ background: GRAPH_INCOMING_COLOR }} />入方向：前置知识</span>
            <span><i className="knowledge-map-legend-line" style={{ background: GRAPH_OUTGOING_COLOR }} />出方向：接下来学习</span>
            {(Object.keys(STATUS_LABEL) as DisplayStatus[]).map((status) => (
              <span key={status}><i style={{ background: STATUS_COLOR[status] }} />{STATUS_LABEL[status]}</span>
            ))}
          </div>
          <div className="knowledge-map-help">悬停：中心发光 · 关系逐层变暗 / 拖拽：调整节点 / 滚轮：有限缩放（0.2×—3.25×）</div>

          {selected && (
            <aside className="knowledge-map-detail rise" aria-label="知识点详情">
              <button
                type="button"
                className="knowledge-map-detail-close"
                aria-label="关闭知识点详情"
                onClick={() => setSelectedId(null)}
              >
                <X size={15} />
              </button>
              <span className="knowledge-map-detail-kicker">SELECTED NODE / 已选知识点</span>
              <div className="map-detail-name">{displayNodeTitle(selected.title)}</div>
              <div className="map-detail-domain">
                {selected.group_key && <span className="tag">{selected.group_key}</span>} {" "}
                <span className="tag">{STAGE_LABEL[graphStage(selected)]}</span>{" "}
                <span className="tag" style={{ color: STATUS_COLOR[displayStatus(selectedOverlay?.status)] }} data-testid="detail-status">
                  {STATUS_LABEL[displayStatus(selectedOverlay?.status)]}
                </span>
              </div>
              {prerequisitesOf(selected.node_id).length > 0 && (
                <div className="section-note">
                  前置：{prerequisitesOf(selected.node_id).map((id) => displayNodeTitle(nodeById.get(id)?.title ?? id)).join("、")}
                </div>
              )}
              {successorsOf(selected.node_id).length > 0 && (
                <div className="section-note">
                  后继：{successorsOf(selected.node_id).map((id) => displayNodeTitle(nodeById.get(id)?.title ?? id)).join("、")}
                </div>
              )}
              {actionError && <div className="map-detail-note" role="alert" style={{ color: "var(--cinnabar)" }}>{actionError}</div>}
              {polling.pending && <div className="section-note">状态提交中…</div>}
              <div className="map-actions">
                <button className="btn btn-ghost" disabled={polling.pending} onClick={() => void submitStateAction(selected.node_id, "mark_unfamiliar")}>不熟悉</button>
                <button className="btn btn-ghost" disabled={polling.pending} onClick={() => void submitStateAction(selected.node_id, "mark_familiar")}>熟悉</button>
                <button className="btn btn-ghost" disabled={polling.pending || !selectedOverlay} onClick={() => void submitStateAction(selected.node_id, "clear")}>清除</button>
                <button className="btn btn-ghost" disabled={polling.pending} onClick={() => setExpertNotice(true)}>精通</button>
              </div>
              {expertNotice && <div className="map-detail-note" role="note" data-testid="expert-notice">{EXPERT_FORBIDDEN_MESSAGE}</div>}
              {selectedOverlay && (
                <button className="link-btn" style={{ marginTop: 8 }} onClick={() => void showExplanation(selected.node_id)}>
                  查看状态依据
                </button>
              )}
              {explanation?.explanation_available && (
                <div className="map-detail-note" data-testid="state-explanation">
                  {explanation.summary ?? "状态由学习证据自动评估。"}
                  {explanation.reason_codes.length > 0 && <div className="memory-time" style={{ marginTop: 4 }}>依据：{explanation.reason_codes.join("、")}</div>}
                </div>
              )}
              <button className="btn btn-red" style={{ width: "100%", justifyContent: "center", marginTop: 14 }} onClick={goChat}>
                <MessageCircle size={15} /> 针对「{selected.title}」提问
              </button>
            </aside>
          )}
        </div>
      </div>
    </div>
  );
}

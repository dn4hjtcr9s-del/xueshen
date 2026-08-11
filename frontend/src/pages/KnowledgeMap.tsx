// 知识地图页（规格 §20.2）：后端固定图谱 + 用户 Overlay 四状态。
// dagre 确定性层次布局；不熟悉/熟悉/清除乐观更新、失败回滚（§20.3）；
// “精通”不可手动设置，点击只显示固定提示且不发请求（§20.2）。
import { useCallback, useEffect, useMemo, useState } from "react";
import { MessageCircle } from "lucide-react";
import {
  clearGraphState,
  getGraphStateExplanation,
  getKnowledgeGraph,
  getMyGraphStates,
  MemoryApiError,
  setGraphState,
  type GraphOverlayView,
  type GraphStateExplanation,
  type GraphStatus,
  type KnowledgeGraphSnapshot,
} from "../api/memory";
import { isTerminalStatus, useOperationPolling } from "../api/operations";
import { SectionHead } from "../ui";
import {
  displayStatus,
  EXPERT_FORBIDDEN_MESSAGE,
  filterNodeIds,
  layoutGraph,
  STATUS_COLOR,
  STATUS_LABEL,
  type DisplayStatus,
} from "./knowledge-map/graph";

const STATUS_FILTERS: { value: DisplayStatus | "all"; label: string }[] = [
  { value: "all", label: "全部状态" },
  { value: "none", label: "无状态" },
  { value: "learning", label: "学习中" },
  { value: "proficient", label: "熟练" },
  { value: "expert", label: "精通" },
];

export function KnowledgeMapPage({ goChat }: { goChat: () => void }) {
  const [snapshot, setSnapshot] = useState<KnowledgeGraphSnapshot | null>(null);
  const [overlays, setOverlays] = useState<Map<string, GraphOverlayView>>(new Map());
  const [loadError, setLoadError] = useState<string | null>(null);
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [query, setQuery] = useState("");
  const [group, setGroup] = useState<string | null>(null);
  const [statusFilter, setStatusFilter] = useState<DisplayStatus | "all">("all");
  const [operationId, setOperationId] = useState<string | null>(null);
  const [actionError, setActionError] = useState<string | null>(null);
  const [expertNotice, setExpertNotice] = useState(false);
  const [explanation, setExplanation] = useState<GraphStateExplanation | null>(null);
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

  // 写操作完成后以服务端 Overlay 为准刷新
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

  const layout = useMemo(
    () => (snapshot ? layoutGraph(snapshot.nodes, snapshot.edges) : null),
    [snapshot],
  );
  const groups = useMemo(
    () =>
      snapshot
        ? [...new Set(snapshot.nodes.map((node) => node.group_key).filter((g): g is string => g !== null))].sort()
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

  const selected = selectedId ? (layout?.nodes.get(selectedId) ?? null) : null;
  const selectedOverlay = selectedId ? overlays.get(selectedId) : undefined;

  const submitStateAction = async (
    nodeId: string,
    action: "mark_unfamiliar" | "mark_familiar" | "clear",
  ) => {
    const previous = overlays.get(nodeId);
    const optimisticStatus: GraphStatus | null =
      action === "mark_unfamiliar" ? "learning" : action === "mark_familiar" ? "proficient" : null;
    // 乐观 UI：先更新本地 Overlay，失败时回滚（§20.3）
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
      // 回滚到提交前的 Overlay
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

  if (loadError) {
    return (
      <div style={{ maxWidth: 1160, margin: "0 auto" }}>
        <SectionHead num="01" title="知识点星图" note="固定知识图谱 · 四状态" />
        <div className="memory-banner" role="alert" style={{ marginTop: 18 }}>
          <span>{loadError}</span>
        </div>
      </div>
    );
  }
  if (!snapshot || !layout) {
    return (
      <div style={{ maxWidth: 1160, margin: "0 auto" }}>
        <SectionHead num="01" title="知识点星图" note="固定知识图谱 · 四状态" />
        <div className="section-note" style={{ marginTop: 18 }}>
          图谱加载中…
        </div>
      </div>
    );
  }

  const successorsOf = (nodeId: string) =>
    snapshot.edges.filter((edge) => edge.from_node_id === nodeId).map((edge) => edge.to_node_id);
  const prerequisitesOf = (nodeId: string) =>
    snapshot.edges.filter((edge) => edge.to_node_id === nodeId).map((edge) => edge.from_node_id);

  return (
    <div style={{ maxWidth: 1160, margin: "0 auto" }}>
      <SectionHead
        num="01"
        title="知识点星图"
        note={`${snapshot.nodes.length} 个固定节点 · 节点颜色 = 学习状态（无百分比掌握度）`}
      />
      <div className="map-filterbar">
        <input
          aria-label="搜索节点"
          placeholder="搜索节点标题…"
          value={query}
          onChange={(e) => setQuery(e.target.value)}
        />
        <select
          aria-label="按分组筛选"
          value={group ?? ""}
          onChange={(e) => setGroup(e.target.value || null)}
        >
          <option value="">全部分组</option>
          {groups.map((g) => (
            <option key={g} value={g}>
              {g}
            </option>
          ))}
        </select>
        <select
          aria-label="按状态筛选"
          value={statusFilter}
          onChange={(e) => setStatusFilter(e.target.value as DisplayStatus | "all")}
        >
          {STATUS_FILTERS.map((option) => (
            <option key={option.value} value={option.value}>
              {option.label}
            </option>
          ))}
        </select>
      </div>
      <div className="map-layout" style={{ marginTop: 18 }}>
        <div className="rise">
          <div className="map-figure">
            <svg viewBox={`0 0 ${layout.width} ${layout.height}`} role="img" aria-label="知识图谱">
              {snapshot.edges
                .filter(
                  (edge) => visibleIds.has(edge.from_node_id) && visibleIds.has(edge.to_node_id),
                )
                .map((edge) => {
                  const from = layout.nodes.get(edge.from_node_id)!;
                  const to = layout.nodes.get(edge.to_node_id)!;
                  return (
                    <line
                      key={`${edge.from_node_id}-${edge.to_node_id}`}
                      x1={from.x}
                      y1={from.y}
                      x2={to.x}
                      y2={to.y}
                      stroke="var(--line)"
                      strokeWidth="1.4"
                      strokeDasharray="5 4"
                    />
                  );
                })}
              {[...visibleIds].map((nodeId) => {
                const item = layout.nodes.get(nodeId)!;
                const status = displayStatus(statusMap.get(nodeId));
                const color = STATUS_COLOR[status];
                const active = nodeId === selectedId;
                return (
                  <g
                    key={nodeId}
                    onClick={() => {
                      setSelectedId(nodeId);
                      setExpertNotice(false);
                      setExplanation(null);
                    }}
                    style={{ cursor: "pointer" }}
                    data-testid={`node-${nodeId}`}
                    data-status={status}
                  >
                    {active && (
                      <rect
                        x={item.x - item.width / 2 - 5}
                        y={item.y - item.height / 2 - 5}
                        width={item.width + 10}
                        height={item.height + 10}
                        rx={8}
                        fill="none"
                        stroke={color}
                        strokeWidth="1.5"
                        strokeDasharray="3 4"
                        opacity={0.8}
                      />
                    )}
                    <rect
                      x={item.x - item.width / 2}
                      y={item.y - item.height / 2}
                      width={item.width}
                      height={item.height}
                      rx={8}
                      fill="var(--card)"
                      stroke={color}
                      strokeWidth={active ? 3 : 2}
                    />
                    <text
                      x={item.x}
                      y={item.y + 4}
                      textAnchor="middle"
                      className="map-node-label"
                      fontWeight={600}
                    >
                      {item.node.title.length > 9
                        ? `${item.node.title.slice(0, 9)}…`
                        : item.node.title}
                    </text>
                  </g>
                );
              })}
            </svg>
            <div className="map-caption">
              图 1 · 知识图谱 — FIG. 1 KNOWLEDGE GRAPH（虚线 = 先修依赖，布局由边确定）
            </div>
          </div>
          <div className="map-legend">
            {(Object.keys(STATUS_LABEL) as DisplayStatus[]).map((status) => (
              <span key={status}>
                <i style={{ background: STATUS_COLOR[status] }} />
                {STATUS_LABEL[status]}
              </span>
            ))}
          </div>
        </div>

        <div className="card map-detail rise" style={{ animationDelay: "0.1s" }}>
          {selected ? (
            <>
              <div className="map-detail-name">{selected.node.title}</div>
              <div className="map-detail-domain">
                {selected.node.group_key && <span className="tag">{selected.node.group_key}</span>}{" "}
                <span
                  className="tag"
                  style={{ color: STATUS_COLOR[displayStatus(selectedOverlay?.status)] }}
                  data-testid="detail-status"
                >
                  {STATUS_LABEL[displayStatus(selectedOverlay?.status)]}
                </span>
              </div>
              {prerequisitesOf(selected.node.node_id).length > 0 && (
                <div className="section-note">
                  前置：
                  {prerequisitesOf(selected.node.node_id)
                    .map((id) => layout.nodes.get(id)?.node.title ?? id)
                    .join("、")}
                </div>
              )}
              {successorsOf(selected.node.node_id).length > 0 && (
                <div className="section-note">
                  后继：
                  {successorsOf(selected.node.node_id)
                    .map((id) => layout.nodes.get(id)?.node.title ?? id)
                    .join("、")}
                </div>
              )}
              {actionError && (
                <div className="map-detail-note" role="alert" style={{ color: "var(--cinnabar)" }}>
                  {actionError}
                </div>
              )}
              {polling.pending && <div className="section-note">状态提交中…</div>}
              <div className="map-actions">
                <button
                  className="btn btn-ghost"
                  disabled={polling.pending}
                  onClick={() => void submitStateAction(selected.node.node_id, "mark_unfamiliar")}
                >
                  不熟悉
                </button>
                <button
                  className="btn btn-ghost"
                  disabled={polling.pending}
                  onClick={() => void submitStateAction(selected.node.node_id, "mark_familiar")}
                >
                  熟悉
                </button>
                <button
                  className="btn btn-ghost"
                  disabled={polling.pending || !selectedOverlay}
                  onClick={() => void submitStateAction(selected.node.node_id, "clear")}
                >
                  清除
                </button>
                <button
                  className="btn btn-ghost"
                  disabled={polling.pending}
                  onClick={() => setExpertNotice(true)}
                >
                  精通
                </button>
              </div>
              {expertNotice && (
                <div className="map-detail-note" role="note" data-testid="expert-notice">
                  {EXPERT_FORBIDDEN_MESSAGE}
                </div>
              )}
              {selectedOverlay && (
                <button
                  className="link-btn"
                  style={{ marginTop: 8 }}
                  onClick={() => void showExplanation(selected.node.node_id)}
                >
                  查看状态依据
                </button>
              )}
              {explanation && explanation.explanation_available && (
                <div className="map-detail-note" data-testid="state-explanation">
                  {explanation.summary ?? "状态由学习证据自动评估。"}
                  {explanation.reason_codes.length > 0 && (
                    <div className="memory-time" style={{ marginTop: 4 }}>
                      依据：{explanation.reason_codes.join("、")}
                    </div>
                  )}
                </div>
              )}
              <button
                className="btn btn-red"
                style={{ width: "100%", justifyContent: "center", marginTop: 14 }}
                onClick={goChat}
              >
                <MessageCircle size={15} /> 针对「{selected.node.title}」提问
              </button>
            </>
          ) : (
            <div className="map-detail-note">点击左侧节点查看详情与状态操作。</div>
          )}
        </div>
      </div>
    </div>
  );
}

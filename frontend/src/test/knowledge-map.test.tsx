// KnowledgeMap 接入测试（§20.2 / §23.6）：后端图谱驱动布局、四状态显示、
// 不熟悉/熟悉/清除交互、精通禁止手动设置、乐观更新失败回滚。
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { http, HttpResponse } from "msw";
import { beforeEach, describe, expect, it } from "vitest";
import { KnowledgeMapPage } from "../pages/KnowledgeMap";
import {
  clampGraphCenter,
  displayNodeTitle,
  EXPERT_FORBIDDEN_MESSAGE,
  focusDepths,
  focusedLinkDirection,
  focusOpacity,
  graphStage,
  initialNodeIds,
  progressionDepths,
  progressionTargetY,
} from "../pages/knowledge-map/graph";
import { graphSnapshot, operationResult, overlay } from "./fixtures";
import { server } from "./server";

function installGraphHandlers(overlays = [overlay({ node_id: "n002", status: "learning" })]) {
  server.use(
    http.get("*/api/v1/knowledge-graph/nodes", () => HttpResponse.json(graphSnapshot())),
    http.get("*/api/v1/knowledge-graph/me/nodes", () => HttpResponse.json(overlays)),
  );
}

/** 可变 Overlay 状态：模拟服务端在写操作后返回新状态。 */
function installMutableGraphHandlers(initial = [overlay({ node_id: "n002", status: "learning" })]) {
  const state = { overlays: initial };
  server.use(
    http.get("*/api/v1/knowledge-graph/nodes", () => HttpResponse.json(graphSnapshot())),
    http.get("*/api/v1/knowledge-graph/me/nodes", () => HttpResponse.json(state.overlays)),
  );
  return state;
}

const noop = () => {};

describe("交互图谱阶段与关系布局语义", () => {
  it("显示名称去掉章节编号和阶段无关前缀，但保留完整知识点内容", () => {
    expect(displayNodeTitle("第二章 导数与微分")).toBe("导数与微分");
    expect(displayNodeTitle("第1章 行列式")).toBe("行列式");
    expect(displayNodeTitle("第二章 导数与微分 大学")).toBe("导数与微分");
    expect(displayNodeTitle("数学建模 建立函数模型解决实际问题")).toBe("建立函数模型解决实际问题");
    expect(displayNodeTitle("自定义专题")).toBe("自定义专题");
  });

  it("节点代理列表也使用清理后的知识点名称", async () => {
    const snapshot = graphSnapshot({
      nodes: [
        { node_id: "n001", title: "第一章 有理数", group_key: "代数", metadata: {} },
        { node_id: "n002", title: "函数", group_key: "代数", metadata: {} },
        { node_id: "n003", title: "极限", group_key: "分析", metadata: {} },
        { node_id: "n004", title: "导数", group_key: "分析", metadata: {} },
      ],
    });
    server.use(
      http.get("*/api/v1/knowledge-graph/nodes", () => HttpResponse.json(snapshot)),
      http.get("*/api/v1/knowledge-graph/me/nodes", () => HttpResponse.json([])),
    );
    render(<KnowledgeMapPage goChat={noop} />);
    await waitFor(() => expect(screen.getByTestId("node-n001")).toBeInTheDocument());
    expect(screen.getByTestId("node-n001")).toHaveTextContent("有理数");
    expect(screen.getByTestId("node-n001")).not.toHaveTextContent("第一章");
  });

  it("将只有出边、没有入边的节点标记为学习起点", () => {
    const snapshot = graphSnapshot({
      nodes: [
        { node_id: "root", title: "基础", group_key: "代数", metadata: {} },
        { node_id: "middle", title: "中间", group_key: "代数", metadata: {} },
        { node_id: "leaf", title: "末端", group_key: "代数", metadata: {} },
        { node_id: "isolated", title: "孤立", group_key: "代数", metadata: {} },
      ],
      edges: [
        { from_node_id: "root", to_node_id: "middle", relation_type: "prerequisite" },
        { from_node_id: "middle", to_node_id: "leaf", relation_type: "prerequisite" },
      ],
    });
    expect(initialNodeIds(snapshot.nodes, snapshot.edges)).toEqual(new Set(["root"]));
  });

  it("按后端 stage 将大学放在上方、初中放在下方", () => {
    const snapshot = graphSnapshot({
      nodes: [
        { node_id: "n001", title: "集合", group_key: "代数", metadata: { stage: "junior" } },
        { node_id: "n002", title: "函数", group_key: "代数", metadata: { stage: "high" } },
        { node_id: "n003", title: "极限", group_key: "分析", metadata: { stage: "university" } },
      ],
      edges: [
        { from_node_id: "n001", to_node_id: "n002", relation_type: "prerequisite" },
        { from_node_id: "n002", to_node_id: "n003", relation_type: "prerequisite" },
      ],
    });
    const depths = progressionDepths(snapshot.nodes, snapshot.edges);
    const maxDepth = Math.max(...depths.values());
    expect(graphStage(snapshot.nodes[0])).toBe("junior");
    expect(progressionTargetY("university", depths.get("n003")!, maxDepth, 600))
      .toBeLessThan(progressionTargetY("junior", depths.get("n001")!, maxDepth, 600));
  });

  it("将图谱视口中心限制在内容范围内，避免平移到空白处", () => {
    const bbox = { x: [-500, 500] as [number, number], y: [-300, 300] as [number, number] };
    const viewport = { width: 1000, height: 600 };
    expect(clampGraphCenter({ x: 5000, y: -5000 }, 1, bbox, viewport)).toEqual({ x: 48, y: -48 });
    expect(clampGraphCenter({ x: 5000, y: -5000 }, 0.2, bbox, viewport)).toEqual({ x: 0, y: 0 });
    expect(clampGraphCenter({ x: 5000, y: -5000 }, 2, bbox, viewport)).toEqual({ x: 274, y: -174 });
  });

  it("按焦点节点区分入方向前置线和出方向学习线", () => {
    const snapshot = graphSnapshot();
    expect(focusedLinkDirection(snapshot.edges[0], "n002")).toBe("incoming");
    expect(focusedLinkDirection(snapshot.edges[1], "n002")).toBe("outgoing");
    expect(focusedLinkDirection(snapshot.edges[0], "n004")).toBe(null);
  });

  it("以无向关系距离计算焦点层级并逐级降低透明度", () => {
    const snapshot = graphSnapshot();
    const depths = focusDepths(snapshot.edges, "n002");
    expect(depths.get("n002")).toBe(0);
    expect(depths.get("n001")).toBe(1);
    expect(depths.get("n004")).toBe(2);
    expect(focusOpacity(0)).toBeGreaterThan(focusOpacity(1));
    expect(focusOpacity(1)).toBeGreaterThan(focusOpacity(2));
    expect(focusOpacity(undefined)).toBeLessThan(focusOpacity(2));
  });
});

describe("KnowledgeMap 四状态显示（§20.2）", () => {
  beforeEach(() => {
    installGraphHandlers([
      overlay({ node_id: "n001", status: null, version: null }),
      overlay({ node_id: "n002", status: "learning" }),
      overlay({ node_id: "n003", status: "proficient" }),
      overlay({ node_id: "n004", status: "expert" }),
    ]);
  });

  it("节点渲染为四种状态语义，无百分比掌握度", async () => {
    render(<KnowledgeMapPage goChat={noop} />);
    await waitFor(() => expect(screen.getByTestId("node-n002")).toHaveAttribute("data-status", "learning"));
    expect(screen.getByTestId("node-n001")).toHaveAttribute("data-status", "none");
    expect(screen.getByTestId("node-n003")).toHaveAttribute("data-status", "proficient");
    expect(screen.getByTestId("node-n004")).toHaveAttribute("data-status", "expert");
    // 图例只展示四状态文案
    for (const label of ["无状态", "学习中", "熟练", "精通"]) {
      expect(screen.getAllByText(label).length).toBeGreaterThan(0);
    }
    // 不再展示百分比掌握度
    expect(screen.queryByText(/%/)).not.toBeInTheDocument();
  });

  it("状态筛选只控制可见节点", async () => {
    const user = userEvent.setup();
    render(<KnowledgeMapPage goChat={noop} />);
    await waitFor(() => expect(screen.getByTestId("node-n002")).toBeInTheDocument());
    await user.selectOptions(screen.getByLabelText("按状态筛选"), "expert");
    expect(screen.queryByTestId("node-n002")).not.toBeInTheDocument();
    expect(screen.getByTestId("node-n004")).toBeInTheDocument();
  });
});

describe("KnowledgeMap 状态交互（§20.2 / §20.3）", () => {
  beforeEach(() => installGraphHandlers());

  it("点击“熟悉”乐观更新并提交 mark_familiar", async () => {
    const state = installMutableGraphHandlers();
    const bodies: unknown[] = [];
    server.use(
      http.put("*/api/v1/knowledge-graph/me/nodes/:id/state", async ({ request, params }) => {
        bodies.push(await request.json());
        state.overlays = [
          ...state.overlays.filter((o) => o.node_id !== params.id),
          overlay({ node_id: String(params.id), status: "proficient", version: 1 }),
        ];
        return HttpResponse.json(
          operationResult({
            operation_type: "set_graph_state",
            graph_state_changes: [
              {
                node_id: "n001",
                before_status: null,
                after_status: "proficient",
                before_version: null,
                after_version: 1,
                source_type: "user",
                reason_codes: [],
                changed_at: "2026-08-11T08:00:00Z",
              },
            ],
          }),
        );
      }),
    );
    const user = userEvent.setup();
    render(<KnowledgeMapPage goChat={noop} />);
    await waitFor(() => expect(screen.getByTestId("node-n001")).toBeInTheDocument());
    await user.click(screen.getByTestId("node-n001"));
    await user.click(screen.getByRole("button", { name: "熟悉" }));
    expect(screen.getByTestId("detail-status")).toHaveTextContent("熟练");
    await waitFor(() => expect(bodies).toHaveLength(1));
    expect(bodies[0]).toMatchObject({ action: "mark_familiar" });
  });

  it("点击“不熟悉”提交 mark_unfamiliar；“清除”调用 DELETE", async () => {
    const state = installMutableGraphHandlers();
    const puts: unknown[] = [];
    let deletes = 0;
    server.use(
      http.put("*/api/v1/knowledge-graph/me/nodes/:id/state", async ({ request, params }) => {
        puts.push(await request.json());
        state.overlays = [
          ...state.overlays.filter((o) => o.node_id !== params.id),
          overlay({ node_id: String(params.id), status: "learning", version: 1 }),
        ];
        return HttpResponse.json(operationResult({ operation_type: "set_graph_state" }));
      }),
      http.delete("*/api/v1/knowledge-graph/me/nodes/:id/state", ({ params }) => {
        deletes += 1;
        state.overlays = state.overlays.filter((o) => o.node_id !== params.id);
        return HttpResponse.json(operationResult({ operation_type: "set_graph_state" }));
      }),
    );
    const user = userEvent.setup();
    render(<KnowledgeMapPage goChat={noop} />);
    await waitFor(() => expect(screen.getByTestId("node-n001")).toBeInTheDocument());
    await user.click(screen.getByTestId("node-n001"));
    await user.click(screen.getByRole("button", { name: "不熟悉" }));
    expect(screen.getByTestId("detail-status")).toHaveTextContent("学习中");
    await waitFor(() => expect(puts).toHaveLength(1));
    expect(puts[0]).toMatchObject({ action: "mark_unfamiliar" });

    await user.click(screen.getByRole("button", { name: "清除" }));
    expect(screen.getByTestId("detail-status")).toHaveTextContent("无状态");
    await waitFor(() => expect(deletes).toBe(1));
  });

  it("点击“精通”显示固定提示且不发非法请求", async () => {
    let writes = 0;
    server.use(
      http.put("*/api/v1/knowledge-graph/me/nodes/:id/state", () => {
        writes += 1;
        return HttpResponse.json(operationResult());
      }),
      http.delete("*/api/v1/knowledge-graph/me/nodes/:id/state", () => {
        writes += 1;
        return HttpResponse.json(operationResult());
      }),
    );
    const user = userEvent.setup();
    render(<KnowledgeMapPage goChat={noop} />);
    await waitFor(() => expect(screen.getByTestId("node-n001")).toBeInTheDocument());
    await user.click(screen.getByTestId("node-n001"));
    await user.click(screen.getByRole("button", { name: "精通" }));
    expect(screen.getByTestId("expert-notice")).toHaveTextContent(EXPERT_FORBIDDEN_MESSAGE);
    expect(writes).toBe(0);
  });

  it("提交失败回滚乐观更新并显示错误", async () => {
    server.use(
      http.put("*/api/v1/knowledge-graph/me/nodes/:id/state", () =>
        HttpResponse.json(
          {
            error: {
              code: "INTERNAL_ERROR",
              message: "存储不可用",
              retryable: true,
              field: null,
              trace_id: "ab".repeat(16),
            },
          },
          { status: 503 },
        ),
      ),
    );
    const user = userEvent.setup();
    render(<KnowledgeMapPage goChat={noop} />);
    await waitFor(() => expect(screen.getByTestId("node-n001")).toBeInTheDocument());
    await user.click(screen.getByTestId("node-n001"));
    await user.click(screen.getByText("熟悉"));
    // 乐观期间显示熟练，失败后回滚为无状态
    await waitFor(() => expect(screen.getByTestId("detail-status")).toHaveTextContent("无状态"));
    expect(screen.getByRole("alert")).toHaveTextContent("存储不可用");
  });
});

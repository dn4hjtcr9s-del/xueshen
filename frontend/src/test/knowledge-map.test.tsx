// KnowledgeMap 接入测试（§20.2 / §23.6）：后端图谱驱动布局、四状态显示、
// 不熟悉/熟悉/清除交互、精通禁止手动设置、乐观更新失败回滚。
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { http, HttpResponse } from "msw";
import { beforeEach, describe, expect, it } from "vitest";
import { KnowledgeMapPage } from "../pages/KnowledgeMap";
import { EXPERT_FORBIDDEN_MESSAGE, layoutGraph } from "../pages/knowledge-map/graph";
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

describe("dagre 布局（§20.2：后端节点和边驱动，无 x/y 存储）", () => {
  it("prerequisite 边的后继节点排在更右层（LR 层次布局）", () => {
    const snapshot = graphSnapshot();
    const layout = layoutGraph(snapshot.nodes, snapshot.edges);
    const x = (id: string) => layout.nodes.get(id)!.x;
    expect(x("n001")).toBeLessThan(x("n002"));
    expect(x("n002")).toBeLessThan(x("n003"));
    expect(x("n003")).toBeLessThan(x("n004"));
  });

  it("无边孤立节点也能布局", () => {
    const snapshot = graphSnapshot({ edges: [] });
    const layout = layoutGraph(snapshot.nodes, snapshot.edges);
    expect(layout.nodes.size).toBe(4);
    expect(layout.width).toBeGreaterThan(0);
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

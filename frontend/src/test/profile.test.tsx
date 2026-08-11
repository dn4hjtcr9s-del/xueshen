// Profile「AI 记住了我什么」接入测试（§20.1 / §23.6）：
// 加载、纠正携带 expected_version、删除、30 天恢复、候选接受/拒绝、409 冲突提示。
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { http, HttpResponse } from "msw";
import { beforeEach, describe, expect, it } from "vitest";
import { MemorySection } from "../pages/profile/MemorySection";
import {
  candidateView,
  deletedItem,
  indexView,
  learnerView,
  masteryView,
  operationResult,
} from "./fixtures";
import { server } from "./server";

function installReadHandlers() {
  server.use(
    http.get("*/api/v1/memory/learner", () => HttpResponse.json(learnerView())),
    http.get("*/api/v1/memory/index", () => HttpResponse.json(indexView())),
    http.get("*/api/v1/memory/deleted", () =>
      HttpResponse.json({ items: [deletedItem()], next_cursor: null, has_more: false }),
    ),
    http.get("*/api/v1/memory/review-candidates", () =>
      HttpResponse.json({ items: [candidateView()], next_cursor: null, has_more: false }),
    ),
    http.get("*/api/v1/memory/mastery/:topicKey", () => HttpResponse.json(masteryView())),
  );
}

describe("Profile 记忆区域（§20.1）", () => {
  beforeEach(installReadHandlers);

  it("加载 learner 与 mastery 列表", async () => {
    render(<MemorySection />);
    expect(await screen.findByText("学习者档案")).toBeInTheDocument();
    expect(await screen.findByText("一次函数")).toBeInTheDocument();
    expect(screen.getByText("例题驱动")).toBeInTheDocument();
    // 待确认候选与已删除列表同时展示
    expect(screen.getByText("三角函数 — 候选概况")).toBeInTheDocument();
    expect(screen.getByText("二次函数")).toBeInTheDocument();
  });

  it("展开 mastery 查看结构化内容", async () => {
    const user = userEvent.setup();
    render(<MemorySection />);
    await screen.findByText("一次函数");
    await user.click(screen.getByText("一次函数"));
    expect(await screen.findByText("整体掌握良好。")).toBeInTheDocument();
    expect(screen.getByText("重做例题")).toBeInTheDocument();
  });

  it("纠正 mastery 携带 expected_version", async () => {
    const bodies: unknown[] = [];
    server.use(
      http.post("*/api/v1/memory/commands/correct", async ({ request }) => {
        bodies.push(await request.json());
        return HttpResponse.json(operationResult());
      }),
    );
    const user = userEvent.setup();
    render(<MemorySection />);
    await screen.findByText("一次函数");
    await user.click(screen.getByText("一次函数"));
    await screen.findByText("整体掌握良好。");
    await user.click(screen.getByLabelText("纠正 一次函数"));
    await user.click(screen.getByText("提交纠正"));
    await waitFor(() => expect(bodies).toHaveLength(1));
    const body = bodies[0] as Record<string, unknown>;
    expect(body.memory_id).toBe("mastery:topic-a");
    expect(body.expected_version).toBe(3);
    expect((body.replacement as Record<string, unknown>).replacement_type).toBe("mastery");
    expect((body.replacement as Record<string, unknown>).topic_title).toBe("一次函数");
    expect(await screen.findByText("已完成。")).toBeInTheDocument();
  });

  it("删除需确认，提交 forget 命令", async () => {
    const bodies: unknown[] = [];
    server.use(
      http.post("*/api/v1/memory/commands/forget", async ({ request }) => {
        bodies.push(await request.json());
        return HttpResponse.json(operationResult({ operation_type: "forget_memory" }));
      }),
    );
    const user = userEvent.setup();
    render(<MemorySection />);
    await screen.findByText("一次函数");
    await user.click(screen.getByLabelText("删除 一次函数"));
    await user.click(screen.getByText("确认删除"));
    await waitFor(() => expect(bodies).toHaveLength(1));
    expect(bodies[0]).toMatchObject({ memory_id: "mastery:topic-a", expected_version: 3 });
  });

  it("30 天内恢复已删除记忆", async () => {
    const bodies: unknown[] = [];
    server.use(
      http.post("*/api/v1/memory/commands/restore", async ({ request }) => {
        bodies.push(await request.json());
        return HttpResponse.json(operationResult({ operation_type: "restore_memory" }));
      }),
    );
    const user = userEvent.setup();
    render(<MemorySection />);
    await screen.findByText("二次函数");
    await user.click(screen.getByLabelText("恢复 二次函数"));
    await waitFor(() => expect(bodies).toHaveLength(1));
    expect(bodies[0]).toMatchObject({ memory_id: "mastery:topic-b", deleted_version: 2 });
  });

  it("接受与拒绝候选", async () => {
    const decisions: unknown[] = [];
    server.use(
      http.post("*/api/v1/memory/review-candidates/:id/decision", async ({ request }) => {
        decisions.push(await request.json());
        return HttpResponse.json(operationResult({ operation_type: "review_candidate" }));
      }),
    );
    const user = userEvent.setup();
    render(<MemorySection />);
    await screen.findByText("三角函数 — 候选概况");
    await user.click(screen.getByText("接受"));
    await waitFor(() => expect(decisions).toHaveLength(1));
    expect(decisions[0]).toMatchObject({ decision: "accept" });
  });

  it("409 冲突刷新数据并提示重新确认（§20.1）", async () => {
    server.use(
      http.post("*/api/v1/memory/commands/correct", () =>
        HttpResponse.json(
          {
            error: {
              code: "MEMORY_VERSION_CONFLICT",
              message: "版本冲突",
              retryable: false,
              field: "expected_version",
              trace_id: "ab".repeat(16),
            },
          },
          { status: 409 },
        ),
      ),
    );
    let indexReads = 0;
    server.use(
      http.get("*/api/v1/memory/index", () => {
        indexReads += 1;
        return HttpResponse.json(indexView());
      }),
    );
    const user = userEvent.setup();
    render(<MemorySection />);
    await screen.findByText("一次函数");
    await user.click(screen.getByText("一次函数"));
    await screen.findByText("整体掌握良好。");
    await user.click(screen.getByLabelText("纠正 一次函数"));
    await user.click(screen.getByText("提交纠正"));
    expect(
      await screen.findByText("数据已被更新，请查看最新内容后重新确认。"),
    ).toBeInTheDocument();
    expect(indexReads).toBeGreaterThan(1);
  });

  it("不展示内部字段（§20.1）", async () => {
    render(<MemorySection />);
    await screen.findByText("一次函数");
    // evidence_refs、lease、thread 等内部字段不出现在页面
    expect(screen.queryByText(/conv:t1:m1/)).not.toBeInTheDocument();
    expect(screen.queryByText(/checkpoint/i)).not.toBeInTheDocument();
  });
});

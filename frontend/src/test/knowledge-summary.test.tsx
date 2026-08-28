/** 知识总结 Phase 5 前端契约测试：验证列表筛选、生成请求和页面卡片状态。 */
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { http, HttpResponse } from "msw";
import { describe, expect, it } from "vitest";
import {
  createKnowledgeSummaryGeneration,
  listKnowledgeSummaries,
} from "../api/knowledgeSummaries";
import { KnowledgeSummaryCard } from "../pages/knowledge-summary/KnowledgeSummaryCard";
import { KnowledgeSummaryDetail } from "../pages/knowledge-summary/KnowledgeSummaryDetail";
import type { KnowledgeSummaryDetailResponse, KnowledgeSummaryListItem } from "../types/knowledgeSummary";
import { server } from "./server";

const SUMMARY: KnowledgeSummaryListItem = {
  summary_id: "11111111-1111-4111-8111-111111111111",
  topic_group_title: "线性代数",
  topic_title: "特征值的几何意义",
  overview_excerpt: "特征向量是在变换下方向保持不变的向量。",
  section_counts: {
    overview: 1,
    definitions: 1,
    theorems: 0,
    formulas: 1,
    properties: 0,
    methods: 0,
    pitfalls: 1,
  },
  source_count: 2,
  available_source_count: 2,
  source_message_count: 4,
  review_state: "clean",
  version: 2,
  updated_at: "2026-08-18T08:00:00Z",
};

const DETAIL: KnowledgeSummaryDetailResponse = {
  summary_id: SUMMARY.summary_id,
  topic_group_title: "线性代数",
  topic_title: "特征值的几何意义",
  status: "active",
  review_state: "clean",
  version: 2,
  content_schema_version: 1,
  content: {
    schema_version: 1,
    overview: { item_id: "00000000-0000-4000-8000-000000000001", text: "概览", origin: "ai", source_ids: [] },
    definitions: [
      { item_id: "00000000-0000-4000-8000-000000000002", text: "定义甲", origin: "ai", source_ids: [] },
      { item_id: "00000000-0000-4000-8000-000000000003", text: "定义乙", origin: "ai", source_ids: [] },
    ],
    theorems: [],
    formulas: [],
    properties: [],
    methods: [],
    pitfalls: [],
  },
  protected_sections: ["definitions"],
  source_count: 2,
  available_source_count: 2,
  source_message_count: 4,
  last_generated_at: "2026-08-18T08:00:00Z",
  created_at: "2026-08-18T08:00:00Z",
  updated_at: "2026-08-18T08:00:00Z",
  pending_review_count: 0,
  pending_reviews: [],
  possible_duplicates: [],
};

function installDetailHandlers(onPatch: (body: unknown) => void) {
  server.use(
    http.get(`*/memory-api/api/v1/knowledge-summaries/${SUMMARY.summary_id}`, () => HttpResponse.json(DETAIL)),
    http.get(`*/memory-api/api/v1/knowledge-summaries/${SUMMARY.summary_id}/sources`, () =>
      HttpResponse.json({ items: [], next_cursor: null, has_more: false }),
    ),
    http.patch(`*/memory-api/api/v1/knowledge-summaries/${SUMMARY.summary_id}`, async ({ request }) => {
      onPatch(await request.json());
      return HttpResponse.json(DETAIL);
    }),
  );
}


describe("知识总结 Phase 5 前端", () => {
  it("列表请求使用冻结的搜索、筛选和 cursor 参数", async () => {
    server.use(
      http.get("*/memory-api/api/v1/knowledge-summaries", ({ request }) => {
        const url = new URL(request.url);
        expect(url.searchParams.get("query")).toBe("特征值");
        expect(url.searchParams.get("topic_group")).toBe("线性代数");
        expect(url.searchParams.get("review_state")).toBe("conflict");
        expect(url.searchParams.get("sort")).toBe("relevance_desc");
        expect(url.searchParams.get("cursor")).toBe("cursor-1");
        return HttpResponse.json({ items: [SUMMARY], next_cursor: null, has_more: false });
      }),
    );
    const response = await listKnowledgeSummaries({
      query: "特征值",
      topicGroup: "线性代数",
      reviewState: "conflict",
      sort: "relevance_desc",
      cursor: "cursor-1",
    });
    expect(response.items[0].summary_id).toBe(SUMMARY.summary_id);
  });

  it("手动生成请求携带新的 client_request_id 和 force 语义", async () => {
    server.use(
      http.post("*/memory-api/api/v1/conversations/thread-1/turns/turn-1/knowledge-summary-generations", async ({ request }) => {
        expect(await request.json()).toEqual({ client_request_id: "client-2", force: true });
        return HttpResponse.json({
          generation_id: "22222222-2222-4222-8222-222222222222",
          trigger: "manual_refresh",
          status: "pending",
          status_path: "/api/v1/knowledge-summary-generations/22222222-2222-4222-8222-222222222222",
        }, { status: 202 });
      }),
    );
    const response = await createKnowledgeSummaryGeneration("thread-1", "turn-1", {
      client_request_id: "client-2",
      force: true,
    });
    expect(response.trigger).toBe("manual_refresh");
  });

  it("总结卡显示来源数量和冲突提示，不显示旧收藏语义", async () => {
    render(<KnowledgeSummaryCard item={{ ...SUMMARY, review_state: "conflict" }} onOpen={() => {}} onChat={() => {}} />);
    await waitFor(() => expect(screen.getByText("待确认")).toBeInTheDocument());
    expect(screen.getByText("2 个来源")).toBeInTheDocument();
    const legacyNotebookLabel = ["错", "题", "本"].join("");
    expect(screen.queryByText(legacyNotebookLabel)).not.toBeInTheDocument();
  });

  it("删除中间条目后仍按原 item_id 提交其余条目", async () => {
    let requestBody: unknown;
    installDetailHandlers((body) => { requestBody = body; });
    render(<KnowledgeSummaryDetail summaryId={SUMMARY.summary_id} onBack={() => {}} onOpenChat={() => {}} onDeleted={() => {}} />);

    await screen.findByRole("heading", { name: DETAIL.topic_title });
    fireEvent.click(screen.getByRole("button", { name: "编辑" }));
    fireEvent.click(screen.getAllByLabelText("删除定义条目")[0]);
    fireEvent.click(screen.getByRole("button", { name: "保存" }));

    await waitFor(() => {
      expect(requestBody).toMatchObject({
        expected_version: 2,
        sections: {
          definitions: [{ item_id: "00000000-0000-4000-8000-000000000003", text: "定义乙" }],
        },
      });
    });
  });

  it("受保护章节可单独请求允许 AI 继续更新", async () => {
    let requestBody: unknown;
    installDetailHandlers((body) => { requestBody = body; });
    render(<KnowledgeSummaryDetail summaryId={SUMMARY.summary_id} onBack={() => {}} onOpenChat={() => {}} onDeleted={() => {}} />);

    await screen.findByRole("heading", { name: DETAIL.topic_title });
    fireEvent.click(screen.getByRole("button", { name: "编辑" }));
    fireEvent.click(screen.getByRole("button", { name: "允许 AI 继续更新" }));
    fireEvent.click(screen.getByRole("button", { name: "保存" }));

    await waitFor(() => {
      expect(requestBody).toEqual({ expected_version: 2, unlock_sections: ["definitions"] });
    });
  });
});

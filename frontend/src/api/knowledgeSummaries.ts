/** 知识总结 REST 客户端：所有请求经过统一鉴权/刷新层，保持 API 与页面解耦。 */
import { request } from "./client";
import type {
  CreateKnowledgeSummaryGenerationRequest,
  KnowledgeSummaryDetailResponse,
  KnowledgeSummaryGenerationResponse,
  KnowledgeSummaryGenerationStatusResponse,
  KnowledgeSummaryListResponse,
  KnowledgeSummarySourcePage,
  KnowledgeSummaryStats,
  KnowledgeSummaryTopicGroupResponse,
  KnowledgeSummaryPatchRequest,
  CurrentTurnKnowledgeSummaryGenerationResponse,
} from "../types/knowledgeSummary";

function queryFrom(values: Record<string, string | undefined>): Record<string, string> {
  return Object.fromEntries(Object.entries(values).filter(([, value]) => value !== undefined)) as Record<
    string,
    string
  >;
}

export function listKnowledgeSummaries(options: {
  query?: string;
  topicGroup?: string;
  reviewState?: "clean" | "possible_duplicate" | "conflict";
  sort?: "relevance_desc" | "updated_desc" | "title_asc";
  cursor?: string;
  limit?: number;
} = {}): Promise<KnowledgeSummaryListResponse> {
  return request("GET", "/knowledge-summaries", {
    query: queryFrom({
      query: options.query,
      topic_group: options.topicGroup,
      review_state: options.reviewState,
      sort: options.sort,
      cursor: options.cursor,
      limit: String(options.limit ?? 20),
    }),
  });
}

export function listKnowledgeSummaryTopicGroups(
  query?: string,
): Promise<KnowledgeSummaryTopicGroupResponse> {
  return request("GET", "/knowledge-summaries/topic-groups", {
    query: queryFrom({ query, limit: "50" }),
  });
}

export function getKnowledgeSummaryStats(): Promise<KnowledgeSummaryStats> {
  return request("GET", "/knowledge-summaries/stats");
}

export function getKnowledgeSummary(summaryId: string): Promise<KnowledgeSummaryDetailResponse> {
  return request("GET", `/knowledge-summaries/${summaryId}`);
}

export function listKnowledgeSummarySources(
  summaryId: string,
  cursor?: string,
): Promise<KnowledgeSummarySourcePage> {
  return request("GET", `/knowledge-summaries/${summaryId}/sources`, {
    query: queryFrom({ cursor, limit: "20" }),
  });
}

export function patchKnowledgeSummary(
  summaryId: string,
  body: KnowledgeSummaryPatchRequest,
): Promise<KnowledgeSummaryDetailResponse> {
  return request("PATCH", `/knowledge-summaries/${summaryId}`, { body });
}

export function deleteKnowledgeSummary(summaryId: string, expectedVersion: number): Promise<void> {
  return request("DELETE", `/knowledge-summaries/${summaryId}`, {
    query: { expected_version: String(expectedVersion) },
  });
}

export function createKnowledgeSummaryGeneration(
  threadId: string,
  turnId: string,
  body: CreateKnowledgeSummaryGenerationRequest,
): Promise<KnowledgeSummaryGenerationResponse> {
  return request("POST", `/conversations/${threadId}/turns/${turnId}/knowledge-summary-generations`, {
    body,
  });
}

export function getCurrentTurnKnowledgeSummaryGeneration(
  threadId: string,
  turnId: string,
): Promise<CurrentTurnKnowledgeSummaryGenerationResponse> {
  return request(
    "GET",
    `/conversations/${threadId}/turns/${turnId}/knowledge-summary-generation`,
  );
}

export function getKnowledgeSummaryGeneration(
  generationId: string,
): Promise<KnowledgeSummaryGenerationStatusResponse> {
  return request("GET", `/knowledge-summary-generations/${generationId}`);
}

export function dismissKnowledgeSummaryReview(
  generationId: string,
  reviewId: string,
): Promise<void> {
  return request("POST", `/knowledge-summary-generations/${generationId}/dismiss-review`, {
    body: { review_id: reviewId },
  });
}

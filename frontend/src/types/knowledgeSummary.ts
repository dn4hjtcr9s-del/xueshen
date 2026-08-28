/**
 * 知识总结 Phase 1 只读 API 类型（知识总结方案 §15）。
 * 来源 API 使用 Turn 聚合契约：source_turn_id 是来源卡身份，不是消息级 source_id。
 */

export type KnowledgeSummaryReviewState = "clean" | "possible_duplicate" | "conflict";
export type KnowledgeSummarySection =
  | "overview"
  | "definitions"
  | "theorems"
  | "formulas"
  | "properties"
  | "methods"
  | "pitfalls";
export type KnowledgeSummaryArraySection = Exclude<KnowledgeSummarySection, "overview">;
export type SourceRole = "user" | "assistant";
export type KnowledgeSummaryItemOrigin = "ai" | "user";
export type KnowledgeSummaryGenerationTrigger =
  | "auto"
  | "manual"
  | "manual_retry"
  | "manual_refresh"
  | "ops_retry";
export type KnowledgeSummaryGenerationStatus =
  | "pending"
  | "processing"
  | "retry_wait"
  | "succeeded"
  | "no_change"
  | "needs_review"
  | "dead_letter"
  | "cancelled";

export interface KnowledgeSummaryItem {
  item_id: string;
  text: string;
  origin: KnowledgeSummaryItemOrigin;
  source_ids: string[];
}

export interface KnowledgeSummaryContent {
  schema_version: 1;
  overview: KnowledgeSummaryItem | null;
  definitions: KnowledgeSummaryItem[];
  theorems: KnowledgeSummaryItem[];
  formulas: KnowledgeSummaryItem[];
  properties: KnowledgeSummaryItem[];
  methods: KnowledgeSummaryItem[];
  pitfalls: KnowledgeSummaryItem[];
}

export interface KnowledgeSummaryListItem {
  summary_id: string;
  topic_group_title: string;
  topic_title: string;
  overview_excerpt: string | null;
  section_counts: Record<KnowledgeSummarySection, number>;
  source_count: number;
  available_source_count: number;
  source_message_count: number;
  review_state: KnowledgeSummaryReviewState;
  version: number;
  updated_at: string;
}

export interface KnowledgeSummaryListResponse {
  items: KnowledgeSummaryListItem[];
  next_cursor: string | null;
  has_more: boolean;
}

export interface KnowledgeSummaryTopicGroup {
  key: string;
  title: string;
  summary_count: number;
  updated_at: string;
}

export interface KnowledgeSummaryTopicGroupResponse {
  items: KnowledgeSummaryTopicGroup[];
  next_cursor: string | null;
  has_more: boolean;
}

export interface KnowledgeSummaryStats {
  active_count: number;
  updated_last_7_days: number;
  pending_review_count: number;
  available_source_count: number;
}

export interface PendingReviewView {
  review_id: string;
  generation_id: string;
  reason_code: string;
  proposed_topic_title: string;
  proposed_sections: Partial<Record<KnowledgeSummaryArraySection, string[]>>;
  source_turn_id: string;
  created_at: string;
}

export interface PossibleDuplicateView {
  duplicate_id: string;
  summary_id: string;
  possible_target_summary_id: string;
  topic_group_title: string;
  topic_title: string;
  match_score: number;
  status: "pending" | "dismissed" | "merged" | "resolved";
  created_at: string;
}

export interface KnowledgeSummaryItemEditInput {
  item_id: string | null;
  text: string;
}

export interface OverviewEditInput {
  item_id: string | null;
  text: string;
}

export interface KnowledgeSummaryPatchRequest {
  expected_version: number;
  topic_group_title?: string;
  topic_title?: string;
  overview?: OverviewEditInput | null;
  sections?: Partial<Record<KnowledgeSummaryArraySection, KnowledgeSummaryItemEditInput[]>>;
  unlock_sections?: KnowledgeSummarySection[];
}

export interface CreateKnowledgeSummaryGenerationRequest {
  client_request_id: string;
  force: boolean;
}

export interface AffectedKnowledgeSummary {
  summary_id: string;
  topic_group_title: string;
  topic_title: string;
}

export interface KnowledgeSummaryGenerationResponse {
  generation_id: string;
  trigger: KnowledgeSummaryGenerationTrigger;
  status: KnowledgeSummaryGenerationStatus;
  status_path: string;
}

export interface KnowledgeSummaryGenerationStatusResponse {
  generation_id: string;
  thread_id: string;
  turn_id: string;
  trigger: KnowledgeSummaryGenerationTrigger;
  status: KnowledgeSummaryGenerationStatus;
  affected_summaries: AffectedKnowledgeSummary[];
  warning_codes: string[];
  review_reason_codes: string[];
  retryable: boolean;
  created_at: string;
  updated_at: string;
  completed_at: string | null;
}

export interface CurrentTurnKnowledgeSummaryGenerationResponse {
  generation: KnowledgeSummaryGenerationStatusResponse | null;
}

export interface KnowledgeSummaryDetailResponse {
  summary_id: string;
  topic_group_title: string;
  topic_title: string;
  status: "active";
  review_state: KnowledgeSummaryReviewState;
  version: number;
  content_schema_version: 1;
  content: KnowledgeSummaryContent;
  protected_sections: KnowledgeSummarySection[];
  source_count: number;
  available_source_count: number;
  source_message_count: number;
  last_generated_at: string | null;
  created_at: string;
  updated_at: string;
  pending_review_count: number;
  pending_reviews: PendingReviewView[];
  possible_duplicates: PossibleDuplicateView[];
}

export interface KnowledgeSummarySourceView {
  source_turn_id: string;
  thread_id: string;
  turn_id: string;
  support_message_ids: string[];
  support_roles: SourceRole[];
  question_excerpt: string | null;
  status: "available" | "unavailable";
  occurred_at: string;
}

export interface KnowledgeSummarySourcePage {
  items: KnowledgeSummarySourceView[];
  next_cursor: string | null;
  has_more: boolean;
}

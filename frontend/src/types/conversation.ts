// Conversation API 契约镜像类型（后端 backend/conversation/contracts/api.py）。

export type ThreadStatus = "active" | "archived" | "deleting" | "deleted";
export type TurnStatusValue =
  | "accepted"
  | "running"
  | "cancelling"
  | "completed"
  | "failed"
  | "cancelled";

export interface Citation {
  citation_id: string;
  corpus_id: string;
  chunk_ids: string[];
  book_id: string;
  book_name: string;
  chapter_path: string[];
  page_start: number | null;
  page_end: number | null;
  snippet: string;
  source_refs: Array<Record<string, unknown>>;
  matched_subquery_ids: string[];
}

export interface ConversationThread {
  thread_id: string;
  title: string;
  status: ThreadStatus;
  version: number;
  updated_at: string;
}

export interface ConversationListPage {
  items: ConversationThread[];
  next_cursor: string | null;
  has_more: boolean;
}

export interface ConversationMessage {
  message_id: string;
  thread_id: string;
  turn_id: string;
  role: "user" | "assistant";
  content: string;
  status: "completed" | "cancelled" | "failed" | "deleted";
  sequence: number;
  occurred_at: string;
  completed_at: string | null;
}

export interface ConversationDetail {
  thread_id: string;
  title: string;
  version: number;
  status: ThreadStatus;
  messages: ConversationMessage[];
  next_cursor: string | null;
  has_more: boolean;
}

export interface CreateTurnRequest {
  client_request_id: string;
  content: string;
  expected_thread_version: number;
}

export interface CreateTurnResponse {
  thread_id: string;
  turn_id: string;
  user_message_id: string;
  thread_version: number;
  status: TurnStatusValue;
  event_stream_path: string;
}

export interface TurnStatus {
  turn_id: string;
  thread_id: string;
  status: TurnStatusValue;
  thread_version: number;
  assistant_message_id: string | null;
  error: { code: string; message: string; retryable: boolean; trace_id: string } | null;
  event_stream_path: string;
}

// ---------------------------------------------------------------------------
// SSE 事件（§17.4）
// ---------------------------------------------------------------------------

export type ConversationEventType =
  | "turn.accepted"
  | "turn.started"
  | "turn.progress"
  | "answer.delta"
  | "citation.available"
  | "turn.degraded"
  | "memory.submission"
  | "answer.completed"
  | "turn.failed"
  | "turn.cancelled";

export interface SSEEnvelope {
  schema_version: "1";
  event_id: string;
  sequence: number;
  event_type: ConversationEventType;
  request_id: string;
  thread_id: string;
  turn_id: string;
  run_id: string;
  occurred_at: string;
  data: Record<string, unknown>;
}

export type TurnProgressStage =
  | "context"
  | "memory"
  | "rewrite"
  | "retrieval"
  | "rerank"
  | "evidence"
  | "answer";

export type TurnProgressStatus = "started" | "completed" | "skipped" | "degraded";

export interface TurnProgressData {
  stage: TurnProgressStage;
  status: TurnProgressStatus;
  title: string;
  detail?: string | null;
  metadata: Record<string, string | number | boolean | null>;
}

export interface TurnProgressItem extends TurnProgressData {
  eventId: string;
  sequence: number;
  occurredAt: string;
}

export interface AnswerCompletedData {
  assistant_message_id: string;
  thread_version: number;
  answer: string;
  citations: Citation[];
  followups: string[];
  degraded_flags: string[];
}

export interface TurnFailedData {
  error: { code: string; message: string; retryable: boolean; trace_id: string };
}

export interface TurnCancelledData {
  status: "cancelled";
  partial_answer_available: boolean;
}

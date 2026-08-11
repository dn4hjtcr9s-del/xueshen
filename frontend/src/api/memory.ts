// Memory API 客户端（规格 §19 / §20.4）。
// 基址来自 VITE_MEMORY_API_BASE_URL（默认 /memory-api，由 Vite proxy 转发）；
// 前端代码不注入任何 Dev Auth Header（开发环境由 Vite proxy 注入 X-Dev-User-Id）。

const API_BASE: string = import.meta.env.VITE_MEMORY_API_BASE_URL ?? "/memory-api";
const V1 = `${API_BASE}/api/v1`;

// ---------------------------------------------------------------------------
// 契约镜像类型（后端 backend/memory/contracts 的公开结构）
// ---------------------------------------------------------------------------

export type MemoryType = "learner" | "mastery";
export type GraphStatus = "learning" | "proficient" | "expert";
export type OperationStatus =
  | "queued"
  | "running"
  | "retry_wait"
  | "succeeded"
  | "needs_review"
  | "dead_letter"
  | "cancelled";

export interface PublicError {
  code: string;
  message: string;
  retryable: boolean;
  field: string | null;
  trace_id: string;
}

export interface CursorPage<T> {
  items: T[];
  next_cursor: string | null;
  has_more: boolean;
}

export interface LearnerMemoryView {
  memory_type: "learner";
  memory_id: "learner";
  version: number;
  preferences: string[];
  goals: string[];
  plans: string[];
  evidence_refs: string[];
  confidence: number | null;
  updated_at: string;
}

export interface MasteryMemoryView {
  memory_type: "mastery";
  memory_id: string;
  topic_key: string;
  topic_title: string;
  version: number;
  overview: string;
  understood: string[];
  difficulties: string[];
  review_advice: string[];
  evidence_refs: string[];
  confidence: number | null;
  updated_at: string;
}

export interface MemoryIndexEntryView {
  memory_id: string;
  memory_type: MemoryType;
  topic_key: string | null;
  title: string;
  version: number;
  updated_at: string;
}

export interface MemoryIndexView {
  version: number;
  entries: MemoryIndexEntryView[];
  updated_at: string | null;
  stale: boolean;
}

export interface DeletedMemoryItem {
  memory_id: string;
  memory_type: MemoryType;
  topic_key: string | null;
  title: string;
  deleted_version: number;
  deleted_at: string;
  restore_until: string;
}

export interface CandidateContentView {
  memory_type: MemoryType;
  topic_key: string | null;
  topic_title: string | null;
  overview: string | null;
  preferences: string[];
  goals: string[];
  plans: string[];
  understood: string[];
  difficulties: string[];
  review_advice: string[];
}

export interface ReviewCandidateView {
  candidate_id: string;
  candidate_type: "learner" | "mastery" | "topic_conflict" | "version_conflict";
  base_memory_id: string | null;
  base_version: number | null;
  topic_key: string | null;
  candidate_content: CandidateContentView;
  evidence_refs: string[];
  confidence: number;
  status: "pending" | "accepted" | "corrected" | "rejected" | "expired";
  resolution_target: "merge_existing" | "create_new_topic" | null;
  target_memory_id: string | null;
  resolved_operation_id: string | null;
  reviewed_at: string | null;
  created_at: string;
  updated_at: string;
}

export interface GraphNodeView {
  node_id: string;
  title: string;
  group_key: string | null;
  metadata: Record<string, unknown>;
}

export interface GraphEdgeView {
  from_node_id: string;
  to_node_id: string;
  relation_type: "prerequisite";
}

export interface KnowledgeGraphSnapshot {
  nodes: GraphNodeView[];
  edges: GraphEdgeView[];
  manifest_checksum: string;
  synced_at: string;
}

export interface GraphOverlayView {
  node_id: string;
  status: GraphStatus | null;
  version: number | null;
  status_source: "user" | "summary_memory" | "system_recompute" | null;
  updated_at: string | null;
}

export interface GraphStateExplanation {
  node_id: string;
  current_status: GraphStatus | null;
  explanation_available: boolean;
  summary: string | null;
  reason_codes: string[];
  source_type: "user" | "summary_memory" | "system_recompute" | null;
  source_memory_id: string | null;
  source_memory_version: number | null;
  evidence_refs: string[];
  changed_at: string | null;
}

export interface GraphRecommendation {
  node_id: string;
  title: string;
  status: GraphStatus | null;
  reason_codes: string[];
  prerequisite_node_ids: string[];
  related_memory_ids: string[];
  updated_at: string | null;
}

export interface MutationResult {
  mutation_id: string;
  memory_id: string;
  action: "create" | "merge" | "replace" | "append_evidence" | "forget" | "restore";
  before_version: number | null;
  after_version: number | null;
}

export interface GraphStateChangeView {
  node_id: string;
  before_status: GraphStatus | null;
  after_status: GraphStatus | null;
  before_version: number | null;
  after_version: number | null;
  source_type: "user" | "summary_memory" | "system_recompute";
  reason_codes: string[];
  changed_at: string;
}

export interface MemoryOperationResult {
  operation_id: string;
  status: OperationStatus;
  operation_type: string;
  created_at: string;
  updated_at: string;
  completed_at: string | null;
  cancelled_at: string | null;
  mutations: MutationResult[];
  review_candidate_ids: string[];
  graph_state_changes: GraphStateChangeView[];
  warnings: string[];
  error: PublicError | null;
}

export interface MemoryNotification {
  notification_id: string;
  event_type: string;
  title: string;
  body: string;
  aggregate_type: string;
  aggregate_id: string;
  read_at: string | null;
  created_at: string;
}

export interface MemoryNotificationPage extends CursorPage<MemoryNotification> {
  unread_count: number;
}

// ---------------------------------------------------------------------------
// 写请求体（公开字段；user_id/kind 等由 Gateway 注入）
// ---------------------------------------------------------------------------

export interface LearnerReplacement {
  replacement_type: "learner";
  preferences: string[];
  goals: string[];
  plans: string[];
}

export interface MasteryReplacement {
  replacement_type: "mastery";
  topic_title: string;
  overview: string;
  understood: string[];
  difficulties: string[];
  review_advice: string[];
  evidence_refs: string[];
}

export type MemoryReplacement = LearnerReplacement | MasteryReplacement;

export interface CorrectMemoryRequest {
  memory_id: string;
  expected_version: number;
  replacement: MemoryReplacement;
  reason?: string;
}

export interface ReviewDecisionRequest {
  decision: "accept" | "correct" | "reject";
  resolution_target?: "merge_existing" | "create_new_topic" | null;
  target_memory_id?: string | null;
  corrected_content?: MemoryReplacement | null;
  reason?: string;
}

// ---------------------------------------------------------------------------
// 错误与请求基础设施
// ---------------------------------------------------------------------------

export class MemoryApiError extends Error {
  readonly status: number;
  readonly code: string;
  readonly retryable: boolean;
  readonly field: string | null;
  readonly traceId: string | null;

  constructor(status: number, error: Partial<PublicError> | undefined, fallback: string) {
    super(error?.message ?? fallback);
    this.status = status;
    this.code = error?.code ?? "INTERNAL_ERROR";
    this.retryable = error?.retryable ?? false;
    this.field = error?.field ?? null;
    this.traceId = error?.trace_id ?? null;
  }
}

async function request<T>(
  method: string,
  path: string,
  options: { body?: unknown; idempotencyKey?: string; query?: Record<string, string> } = {},
): Promise<T> {
  const headers: Record<string, string> = {};
  if (options.body !== undefined) headers["Content-Type"] = "application/json";
  if (options.idempotencyKey) headers["Idempotency-Key"] = options.idempotencyKey;
  const url = options.query
    ? `${V1}${path}?${new URLSearchParams(options.query).toString()}`
    : `${V1}${path}`;
  const response = await fetch(url, {
    method,
    headers,
    body: options.body !== undefined ? JSON.stringify(options.body) : undefined,
  });
  if (!response.ok) {
    let error: Partial<PublicError> | undefined;
    try {
      error = (await response.json()).error;
    } catch {
      // 非 JSON 错误体：用 HTTP 状态兜底
    }
    throw new MemoryApiError(response.status, error, `请求失败（HTTP ${response.status}）`);
  }
  return (await response.json()) as T;
}

function idempotencyKey(): string {
  return crypto.randomUUID();
}

// ---------------------------------------------------------------------------
// 总结记忆读接口（§19.4）
// ---------------------------------------------------------------------------

/** learner 不存在时返回 null（404 MEMORY_NOT_FOUND）。 */
export async function getLearner(): Promise<LearnerMemoryView | null> {
  try {
    return await request<LearnerMemoryView>("GET", "/memory/learner");
  } catch (error) {
    if (error instanceof MemoryApiError && error.status === 404) return null;
    throw error;
  }
}

export function getMemoryIndex(): Promise<MemoryIndexView> {
  return request<MemoryIndexView>("GET", "/memory/index");
}

export function getMastery(topicKey: string): Promise<MasteryMemoryView> {
  return request<MasteryMemoryView>("GET", `/memory/mastery/${encodeURIComponent(topicKey)}`);
}

export function listDeletedMemories(limit = 50): Promise<CursorPage<DeletedMemoryItem>> {
  return request<CursorPage<DeletedMemoryItem>>("GET", "/memory/deleted", {
    query: { limit: String(limit) },
  });
}

export function listReviewCandidates(
  status: ReviewCandidateView["status"] = "pending",
  limit = 50,
): Promise<CursorPage<ReviewCandidateView>> {
  return request<CursorPage<ReviewCandidateView>>("GET", "/memory/review-candidates", {
    query: { status, limit: String(limit) },
  });
}

// ---------------------------------------------------------------------------
// 用户记忆命令（§19.2；P0 快速路径，202 时由调用方轮询 operation）
// ---------------------------------------------------------------------------

export function correctMemory(command: CorrectMemoryRequest): Promise<MemoryOperationResult> {
  return request<MemoryOperationResult>("POST", "/memory/commands/correct", {
    body: command,
    idempotencyKey: idempotencyKey(),
  });
}

export function forgetMemory(command: {
  memory_id: string;
  expected_version: number;
  reason?: string;
}): Promise<MemoryOperationResult> {
  return request<MemoryOperationResult>("POST", "/memory/commands/forget", {
    body: command,
    idempotencyKey: idempotencyKey(),
  });
}

export function restoreMemory(command: {
  memory_id: string;
  deleted_version: number;
}): Promise<MemoryOperationResult> {
  return request<MemoryOperationResult>("POST", "/memory/commands/restore", {
    body: command,
    idempotencyKey: idempotencyKey(),
  });
}

export function decideReviewCandidate(
  candidateId: string,
  decision: ReviewDecisionRequest,
): Promise<MemoryOperationResult> {
  return request<MemoryOperationResult>(
    "POST",
    `/memory/review-candidates/${candidateId}/decision`,
    { body: decision, idempotencyKey: idempotencyKey() },
  );
}

// ---------------------------------------------------------------------------
// 知识图谱（§19.5）
// ---------------------------------------------------------------------------

export function getKnowledgeGraph(): Promise<KnowledgeGraphSnapshot> {
  return request<KnowledgeGraphSnapshot>("GET", "/knowledge-graph/nodes");
}

export function getMyGraphStates(): Promise<GraphOverlayView[]> {
  return request<GraphOverlayView[]>("GET", "/knowledge-graph/me/nodes");
}

export function setGraphState(
  nodeId: string,
  action: "mark_unfamiliar" | "mark_familiar",
  expectedVersion?: number | null,
): Promise<MemoryOperationResult> {
  return request<MemoryOperationResult>("PUT", `/knowledge-graph/me/nodes/${nodeId}/state`, {
    body: expectedVersion != null ? { action, expected_version: expectedVersion } : { action },
    idempotencyKey: idempotencyKey(),
  });
}

export function clearGraphState(
  nodeId: string,
  expectedVersion?: number | null,
): Promise<MemoryOperationResult> {
  const query: Record<string, string> = {};
  if (expectedVersion != null) query.expected_version = String(expectedVersion);
  return request<MemoryOperationResult>(
    "DELETE",
    `/knowledge-graph/me/nodes/${nodeId}/state`,
    { idempotencyKey: idempotencyKey(), query },
  );
}

export function getGraphStateExplanation(nodeId: string): Promise<GraphStateExplanation> {
  return request<GraphStateExplanation>(
    "GET",
    `/knowledge-graph/me/nodes/${nodeId}/explanation`,
  );
}

// ---------------------------------------------------------------------------
// Operation 查询与通知（§19.3 / §19.6）
// ---------------------------------------------------------------------------

export function getOperation(operationId: string): Promise<MemoryOperationResult> {
  return request<MemoryOperationResult>("GET", `/memory/operations/${operationId}`);
}

export function listNotifications(limit = 50): Promise<MemoryNotificationPage> {
  return request<MemoryNotificationPage>("GET", "/memory/notifications", {
    query: { limit: String(limit) },
  });
}

export function markNotificationRead(notificationId: string): Promise<MemoryNotification> {
  return request<MemoryNotification>(
    "POST",
    `/memory/notifications/${notificationId}/read`,
    { body: {}, idempotencyKey: idempotencyKey() },
  );
}

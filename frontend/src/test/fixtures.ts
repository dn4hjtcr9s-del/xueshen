// 测试 fixtures：后端公开契约的最小载荷。
import type {
  DeletedMemoryItem,
  GraphOverlayView,
  KnowledgeGraphSnapshot,
  LearnerMemoryView,
  MasteryMemoryView,
  MemoryIndexView,
  MemoryOperationResult,
  ReviewCandidateView,
} from "../api/memory";

export const USER_ID = "11111111-2222-3333-4444-555555555555";

export function operationResult(overrides: Partial<MemoryOperationResult> = {}): MemoryOperationResult {
  return {
    operation_id: "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee",
    status: "succeeded",
    operation_type: "correct_memory",
    created_at: "2026-08-11T08:00:00Z",
    updated_at: "2026-08-11T08:00:01Z",
    completed_at: "2026-08-11T08:00:01Z",
    cancelled_at: null,
    mutations: [],
    review_candidate_ids: [],
    graph_state_changes: [],
    warnings: [],
    error: null,
    ...overrides,
  };
}

export function learnerView(overrides: Partial<LearnerMemoryView> = {}): LearnerMemoryView {
  return {
    memory_type: "learner",
    memory_id: "learner",
    version: 2,
    preferences: ["例题驱动"],
    goals: ["期末 90 分"],
    plans: ["每天一节"],
    evidence_refs: ["conv:t1:m1"],
    confidence: 0.8,
    updated_at: "2026-08-10T08:00:00Z",
    ...overrides,
  };
}

export function masteryView(overrides: Partial<MasteryMemoryView> = {}): MasteryMemoryView {
  return {
    memory_type: "mastery",
    memory_id: "mastery:topic-a",
    topic_key: "topic-a",
    topic_title: "一次函数",
    version: 3,
    overview: "整体掌握良好。",
    understood: ["定义"],
    difficulties: ["证明"],
    review_advice: ["重做例题"],
    evidence_refs: ["conv:t1:m1"],
    confidence: 0.9,
    updated_at: "2026-08-10T08:00:00Z",
    ...overrides,
  };
}

export function indexView(overrides: Partial<MemoryIndexView> = {}): MemoryIndexView {
  return {
    version: 1,
    entries: [
      {
        memory_id: "mastery:topic-a",
        memory_type: "mastery",
        topic_key: "topic-a",
        title: "一次函数",
        version: 3,
        updated_at: "2026-08-10T08:00:00Z",
      },
    ],
    updated_at: "2026-08-10T08:00:00Z",
    stale: false,
    ...overrides,
  };
}

export function deletedItem(overrides: Partial<DeletedMemoryItem> = {}): DeletedMemoryItem {
  return {
    memory_id: "mastery:topic-b",
    memory_type: "mastery",
    topic_key: "topic-b",
    title: "二次函数",
    deleted_version: 2,
    deleted_at: "2026-08-09T08:00:00Z",
    restore_until: "2026-09-08T08:00:00Z",
    ...overrides,
  };
}

export function candidateView(overrides: Partial<ReviewCandidateView> = {}): ReviewCandidateView {
  return {
    candidate_id: "cccccccc-1111-2222-3333-444444444444",
    candidate_type: "mastery",
    base_memory_id: null,
    base_version: null,
    topic_key: null,
    candidate_content: {
      memory_type: "mastery",
      topic_key: null,
      topic_title: "三角函数",
      overview: "候选概况",
      preferences: [],
      goals: [],
      plans: [],
      understood: ["正弦定义"],
      difficulties: [],
      review_advice: [],
    },
    evidence_refs: ["conv:t2:m3"],
    confidence: 0.55,
    status: "pending",
    resolution_target: null,
    target_memory_id: null,
    resolved_operation_id: null,
    reviewed_at: null,
    created_at: "2026-08-10T09:00:00Z",
    updated_at: "2026-08-10T09:00:00Z",
    ...overrides,
  };
}

export function graphSnapshot(
  overrides: Partial<KnowledgeGraphSnapshot> = {},
): KnowledgeGraphSnapshot {
  return {
    nodes: [
      { node_id: "n001", title: "集合", group_key: "代数", metadata: {} },
      { node_id: "n002", title: "函数", group_key: "代数", metadata: {} },
      { node_id: "n003", title: "极限", group_key: "分析", metadata: {} },
      { node_id: "n004", title: "导数", group_key: "分析", metadata: {} },
    ],
    edges: [
      { from_node_id: "n001", to_node_id: "n002", relation_type: "prerequisite" },
      { from_node_id: "n002", to_node_id: "n003", relation_type: "prerequisite" },
      { from_node_id: "n003", to_node_id: "n004", relation_type: "prerequisite" },
    ],
    manifest_checksum: "a".repeat(64),
    synced_at: "2026-08-10T00:00:00Z",
    ...overrides,
  };
}

export function overlay(overrides: Partial<GraphOverlayView> = {}): GraphOverlayView {
  return {
    node_id: "n002",
    status: "learning",
    version: 1,
    status_source: "user",
    updated_at: "2026-08-10T08:00:00Z",
    ...overrides,
  };
}

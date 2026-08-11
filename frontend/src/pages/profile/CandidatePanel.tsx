// 待确认候选面板（规格 §20.1 / §6.3）：接受、修改或拒绝；
// topic_conflict 必须由用户明确“合并到已有主题”或“创建新主题”，后端不猜测。
import { useState } from "react";
import type {
  MemoryIndexEntryView,
  MemoryReplacement,
  ReviewCandidateView,
  ReviewDecisionRequest,
} from "../../api/memory";
import { formatTime } from "./useMemoryCommand";

const TYPE_LABEL: Record<ReviewCandidateView["candidate_type"], string> = {
  learner: "学习者档案候选",
  mastery: "掌握档案候选",
  topic_conflict: "主题冲突",
  version_conflict: "版本冲突",
};

function contentSummary(candidate: ReviewCandidateView): string {
  const content = candidate.candidate_content;
  if (content.memory_type === "mastery") {
    const pieces = [content.topic_title, content.overview].filter(Boolean);
    return pieces.join(" — ") || "（无内容）";
  }
  const pieces = [...content.goals, ...content.preferences].filter(Boolean);
  return pieces.slice(0, 3).join("；") || "（无内容）";
}

function CandidateCorrectForm({
  candidate,
  pending,
  onSubmit,
  onCancel,
}: {
  candidate: ReviewCandidateView;
  pending: boolean;
  onSubmit: (corrected: MemoryReplacement) => void;
  onCancel: () => void;
}) {
  const content = candidate.candidate_content;
  const isMastery = content.memory_type === "mastery";
  const [topicTitle, setTopicTitle] = useState(content.topic_title ?? "");
  const [overview, setOverview] = useState(content.overview ?? "");
  const [goals, setGoals] = useState(content.goals.join("\n"));
  const [preferences, setPreferences] = useState(content.preferences.join("\n"));
  const [understood, setUnderstood] = useState(content.understood.join("\n"));
  const [difficulties, setDifficulties] = useState(content.difficulties.join("\n"));
  const [reviewAdvice, setReviewAdvice] = useState(content.review_advice.join("\n"));

  const toList = (text: string) =>
    text
      .split("\n")
      .map((line) => line.trim())
      .filter(Boolean);

  return (
    <div className="memory-editor">
      {isMastery ? (
        <>
          <label className="editor-field">
            <span className="editor-label">主题名称</span>
            <input value={topicTitle} onChange={(e) => setTopicTitle(e.target.value)} maxLength={120} />
          </label>
          <label className="editor-field">
            <span className="editor-label">掌握概况</span>
            <textarea rows={3} value={overview} onChange={(e) => setOverview(e.target.value)} maxLength={1200} />
          </label>
          <label className="editor-field">
            <span className="editor-label">已掌握（每行一条）</span>
            <textarea rows={2} value={understood} onChange={(e) => setUnderstood(e.target.value)} />
          </label>
          <label className="editor-field">
            <span className="editor-label">仍有困难（每行一条）</span>
            <textarea rows={2} value={difficulties} onChange={(e) => setDifficulties(e.target.value)} />
          </label>
          <label className="editor-field">
            <span className="editor-label">建议复习（每行一条）</span>
            <textarea rows={2} value={reviewAdvice} onChange={(e) => setReviewAdvice(e.target.value)} />
          </label>
        </>
      ) : (
        <>
          <label className="editor-field">
            <span className="editor-label">学习目标（每行一条）</span>
            <textarea rows={2} value={goals} onChange={(e) => setGoals(e.target.value)} />
          </label>
          <label className="editor-field">
            <span className="editor-label">学习偏好（每行一条）</span>
            <textarea rows={2} value={preferences} onChange={(e) => setPreferences(e.target.value)} />
          </label>
        </>
      )}
      <div className="editor-actions">
        <button
          className="btn btn-red"
          disabled={pending || (isMastery && topicTitle.trim().length === 0)}
          onClick={() =>
            onSubmit(
              isMastery
                ? {
                    replacement_type: "mastery",
                    topic_title: topicTitle.trim(),
                    overview,
                    understood: toList(understood),
                    difficulties: toList(difficulties),
                    review_advice: toList(reviewAdvice),
                    evidence_refs: [],
                  }
                : {
                    replacement_type: "learner",
                    preferences: toList(preferences),
                    goals: toList(goals),
                    plans: content.plans,
                  },
            )
          }
        >
          提交修改
        </button>
        <button className="btn btn-ghost" disabled={pending} onClick={onCancel}>
          取消
        </button>
      </div>
    </div>
  );
}

export function CandidatePanel({
  candidates,
  masteryTargets,
  pending,
  onDecide,
}: {
  candidates: ReviewCandidateView[];
  masteryTargets: MemoryIndexEntryView[];
  pending: boolean;
  onDecide: (candidateId: string, decision: ReviewDecisionRequest) => void;
}) {
  const [editingId, setEditingId] = useState<string | null>(null);
  const [resolution, setResolution] = useState<Record<string, string>>({});
  const [mergeTarget, setMergeTarget] = useState<Record<string, string>>({});

  if (candidates.length === 0) return null;

  const conflictMode = (candidate: ReviewCandidateView) => resolution[candidate.candidate_id] ?? "";
  const canAccept = (candidate: ReviewCandidateView) => {
    if (candidate.candidate_type !== "topic_conflict") return true;
    const mode = conflictMode(candidate);
    if (mode === "create_new_topic") return true;
    if (mode === "merge_existing") return Boolean(mergeTarget[candidate.candidate_id]);
    return false;
  };
  const acceptDecision = (candidate: ReviewCandidateView): ReviewDecisionRequest => {
    if (candidate.candidate_type !== "topic_conflict") return { decision: "accept" };
    const mode = conflictMode(candidate);
    return mode === "merge_existing"
      ? {
          decision: "accept",
          resolution_target: "merge_existing",
          target_memory_id: mergeTarget[candidate.candidate_id],
        }
      : { decision: "accept", resolution_target: "create_new_topic" };
  };

  return (
    <div style={{ marginTop: 26 }}>
      <div className="section-note" style={{ marginBottom: 8 }}>
        待你确认的记忆候选（低置信内容不会直接写入）
      </div>
      {candidates.map((candidate) => (
        <div key={candidate.candidate_id} className="memory-item" style={{ display: "block" }}>
          <div style={{ display: "flex", gap: 14, alignItems: "baseline" }}>
            <span className="tag gold">{TYPE_LABEL[candidate.candidate_type]}</span>
            <span className="memory-text">{contentSummary(candidate)}</span>
            <span className="memory-time">{formatTime(candidate.created_at)}</span>
          </div>
          {candidate.candidate_type === "topic_conflict" && (
            <div className="candidate-resolution">
              <label>
                <input
                  type="radio"
                  name={`resolution-${candidate.candidate_id}`}
                  checked={conflictMode(candidate) === "merge_existing"}
                  onChange={() =>
                    setResolution((prev) => ({ ...prev, [candidate.candidate_id]: "merge_existing" }))
                  }
                />
                合并到已有主题
              </label>
              {conflictMode(candidate) === "merge_existing" && (
                <select
                  aria-label="合并目标主题"
                  value={mergeTarget[candidate.candidate_id] ?? ""}
                  onChange={(e) =>
                    setMergeTarget((prev) => ({ ...prev, [candidate.candidate_id]: e.target.value }))
                  }
                >
                  <option value="">选择主题…</option>
                  {masteryTargets.map((entry) => (
                    <option key={entry.memory_id} value={entry.memory_id}>
                      {entry.title}
                    </option>
                  ))}
                </select>
              )}
              <label>
                <input
                  type="radio"
                  name={`resolution-${candidate.candidate_id}`}
                  checked={conflictMode(candidate) === "create_new_topic"}
                  onChange={() =>
                    setResolution((prev) => ({ ...prev, [candidate.candidate_id]: "create_new_topic" }))
                  }
                />
                创建新主题
              </label>
            </div>
          )}
          {editingId === candidate.candidate_id ? (
            <CandidateCorrectForm
              candidate={candidate}
              pending={pending}
              onCancel={() => setEditingId(null)}
              onSubmit={(corrected) => {
                setEditingId(null);
                onDecide(candidate.candidate_id, { decision: "correct", corrected_content: corrected });
              }}
            />
          ) : (
            <div className="editor-actions" style={{ marginTop: 8 }}>
              <button
                className="btn btn-red"
                disabled={pending || !canAccept(candidate)}
                onClick={() => onDecide(candidate.candidate_id, acceptDecision(candidate))}
              >
                接受
              </button>
              <button
                className="btn btn-ghost"
                disabled={pending}
                onClick={() => setEditingId(candidate.candidate_id)}
              >
                修改
              </button>
              <button
                className="btn btn-ghost"
                disabled={pending}
                onClick={() => onDecide(candidate.candidate_id, { decision: "reject" })}
              >
                拒绝
              </button>
            </div>
          )}
        </div>
      ))}
    </div>
  );
}

// Profile「AI 记住了我什么」区域（规格 §20.1）：唯一接真实 API 的 Profile 区域。
// 加载 learner 与 mastery 列表、查看结构化内容、纠正（携带 expected_version）、
// 删除、30 天内恢复、待确认候选的接受/修改/拒绝；409 冲突刷新后提示重新确认。
// 不展示模型 reasoning、内部 Prompt、文件路径与内部字段（§20.1）。
import { useCallback, useEffect, useState } from "react";
import { ChevronDown, ChevronRight, Pencil, ShieldCheck, Trash2 } from "lucide-react";
import {
  correctMemory,
  decideReviewCandidate,
  forgetMemory,
  getLearner,
  getMastery,
  getMemoryIndex,
  listDeletedMemories,
  listReviewCandidates,
  MemoryApiError,
  restoreMemory,
  type DeletedMemoryItem,
  type LearnerMemoryView,
  type MasteryMemoryView,
  type MemoryIndexView,
  type MemoryReplacement,
  type ReviewCandidateView,
  type ReviewDecisionRequest,
} from "../../api/memory";
import { CandidatePanel } from "./CandidatePanel";
import { DeletedPanel } from "./DeletedPanel";
import { LearnerCorrectEditor, MasteryCorrectEditor } from "./CorrectEditors";
import { formatTime, useMemoryCommand } from "./useMemoryCommand";

function ListBlock({ label, items }: { label: string; items: string[] }) {
  if (items.length === 0) return null;
  return (
    <div className="memory-block">
      <span className="editor-label">{label}</span>
      <ul>
        {items.map((item, index) => (
          <li key={index}>{item}</li>
        ))}
      </ul>
    </div>
  );
}

export function MemorySection() {
  const [learner, setLearner] = useState<LearnerMemoryView | null>(null);
  const [index, setIndex] = useState<MemoryIndexView | null>(null);
  const [deleted, setDeleted] = useState<DeletedMemoryItem[]>([]);
  const [candidates, setCandidates] = useState<ReviewCandidateView[]>([]);
  const [details, setDetails] = useState<Record<string, MasteryMemoryView>>({});
  const [expanded, setExpanded] = useState<string | null>(null);
  const [editing, setEditing] = useState<string | null>(null);
  const [confirmingDelete, setConfirmingDelete] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  const [loadError, setLoadError] = useState<string | null>(null);

  const load = useCallback(async () => {
    setLoadError(null);
    try {
      const [learnerView, indexView, deletedPage, candidatePage] = await Promise.all([
        getLearner(),
        getMemoryIndex(),
        listDeletedMemories(),
        listReviewCandidates("pending"),
      ]);
      setLearner(learnerView);
      setIndex(indexView);
      setDeleted(deletedPage.items);
      setCandidates(candidatePage.items);
      setDetails({});
      setEditing(null);
      setConfirmingDelete(null);
    } catch (error) {
      setLoadError(error instanceof MemoryApiError ? error.message : "记忆数据加载失败，请稍后重试。");
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    void load();
  }, [load]);

  const { submit, pending, notice, dismissNotice } = useMemoryCommand(load);

  const masteryEntries = (index?.entries ?? []).filter((entry) => entry.memory_type === "mastery");

  const toggleExpand = async (topicKey: string) => {
    if (expanded === topicKey) {
      setExpanded(null);
      return;
    }
    setExpanded(topicKey);
    if (!details[topicKey]) {
      try {
        const view = await getMastery(topicKey);
        setDetails((prev) => ({ ...prev, [topicKey]: view }));
      } catch {
        // 展开失败在详情区提示，不影响列表
      }
    }
  };

  const submitCorrect = (memoryId: string, version: number) =>
    (replacement: MemoryReplacement, reason?: string) =>
      void submit(() =>
        correctMemory({ memory_id: memoryId, expected_version: version, replacement, reason }),
      );

  const submitForget = (memoryId: string, version: number) =>
    void submit(() => forgetMemory({ memory_id: memoryId, expected_version: version }));

  const submitRestore = (item: DeletedMemoryItem) =>
    void submit(() =>
      restoreMemory({ memory_id: item.memory_id, deleted_version: item.deleted_version }),
    );

  const submitDecision = (candidateId: string, decision: ReviewDecisionRequest) =>
    void submit(() => decideReviewCandidate(candidateId, decision));

  if (loading) return <div className="section-note">记忆加载中…</div>;
  if (loadError) {
    return (
      <div className="memory-banner" role="alert">
        <ShieldCheck size={16} style={{ flexShrink: 0, marginTop: 1 }} />
        <span>{loadError}</span>
        <button className="link-btn" onClick={() => void load()}>
          重试
        </button>
      </div>
    );
  }

  return (
    <div>
      {notice && (
        <div className={`memory-banner notice-${notice.kind}`} role="status">
          <span>{notice.text}</span>
          <button className="link-btn" onClick={dismissNotice}>
            知道了
          </button>
        </div>
      )}

      {learner && (
        <div className="memory-item" style={{ display: "block" }} data-testid="memory-learner">
          <div style={{ display: "flex", gap: 14, alignItems: "baseline" }}>
            <span className="tag green">学习者档案</span>
            <span className="memory-text">你的学习偏好、目标与当前计划</span>
            <span className="memory-time">{formatTime(learner.updated_at)}</span>
            <span className="memory-actions">
              <button
                aria-label="纠正学习者档案"
                disabled={pending}
                onClick={() => setEditing(editing === "learner" ? null : "learner")}
              >
                <Pencil size={13} />
              </button>
              <button
                aria-label="删除学习者档案"
                disabled={pending}
                onClick={() => setConfirmingDelete("learner")}
              >
                <Trash2 size={13} />
              </button>
            </span>
          </div>
          {confirmingDelete === "learner" && (
            <div className="editor-actions" style={{ marginTop: 8 }}>
              <span className="section-note">删除后立即不可见，30 天内可恢复。确认删除？</span>
              <button className="btn btn-red" disabled={pending} onClick={() => submitForget("learner", learner.version)}>
                确认删除
              </button>
              <button className="btn btn-ghost" disabled={pending} onClick={() => setConfirmingDelete(null)}>
                取消
              </button>
            </div>
          )}
          {editing === "learner" ? (
            <LearnerCorrectEditor
              memory={learner}
              pending={pending}
              onCancel={() => setEditing(null)}
              onSubmit={submitCorrect("learner", learner.version)}
            />
          ) : (
            <div style={{ marginTop: 8 }}>
              <ListBlock label="学习偏好" items={learner.preferences} />
              <ListBlock label="学习目标" items={learner.goals} />
              <ListBlock label="当前计划" items={learner.plans} />
            </div>
          )}
        </div>
      )}

      {masteryEntries.map((entry) => {
        const detail = details[entry.topic_key ?? ""];
        const isExpanded = expanded === entry.topic_key;
        const memoryId = entry.memory_id;
        return (
          <div key={memoryId} className="memory-item" style={{ display: "block" }} data-testid={`memory-${memoryId}`}>
            <div style={{ display: "flex", gap: 14, alignItems: "baseline" }}>
              <span className="tag gold">掌握档案</span>
              <button
                className="link-btn memory-text"
                style={{ textAlign: "left" }}
                onClick={() => entry.topic_key && void toggleExpand(entry.topic_key)}
              >
                {isExpanded ? <ChevronDown size={12} /> : <ChevronRight size={12} />} {entry.title}
              </button>
              <span className="memory-time">{formatTime(entry.updated_at)}</span>
              <span className="memory-actions">
                <button
                  aria-label={`纠正 ${entry.title}`}
                  disabled={pending || !detail}
                  onClick={() => setEditing(editing === memoryId ? null : memoryId)}
                >
                  <Pencil size={13} />
                </button>
                <button
                  aria-label={`删除 ${entry.title}`}
                  disabled={pending}
                  onClick={() => setConfirmingDelete(memoryId)}
                >
                  <Trash2 size={13} />
                </button>
              </span>
            </div>
            {confirmingDelete === memoryId && (
              <div className="editor-actions" style={{ marginTop: 8 }}>
                <span className="section-note">删除后立即不可见，30 天内可恢复。确认删除？</span>
                <button className="btn btn-red" disabled={pending} onClick={() => submitForget(memoryId, entry.version)}>
                  确认删除
                </button>
                <button className="btn btn-ghost" disabled={pending} onClick={() => setConfirmingDelete(null)}>
                  取消
                </button>
              </div>
            )}
            {isExpanded && !detail && <div className="section-note">内容加载中…</div>}
            {detail && editing === memoryId && (
              <MasteryCorrectEditor
                memory={detail}
                pending={pending}
                onCancel={() => setEditing(null)}
                onSubmit={submitCorrect(memoryId, detail.version)}
              />
            )}
            {detail && isExpanded && editing !== memoryId && (
              <div style={{ marginTop: 8 }}>
                {detail.overview && <div className="memory-text" style={{ marginBottom: 6 }}>{detail.overview}</div>}
                <ListBlock label="已掌握" items={detail.understood} />
                <ListBlock label="仍有困难" items={detail.difficulties} />
                <ListBlock label="建议复习" items={detail.review_advice} />
              </div>
            )}
          </div>
        );
      })}

      {!learner && masteryEntries.length === 0 && (
        <div className="section-note">还没有记忆。与 AI 学习对话后，这里会逐步积累。</div>
      )}

      <DeletedPanel items={deleted} pending={pending} onRestore={submitRestore} />
      <CandidatePanel
        candidates={candidates}
        masteryTargets={masteryEntries}
        pending={pending}
        onDecide={submitDecision}
      />
    </div>
  );
}

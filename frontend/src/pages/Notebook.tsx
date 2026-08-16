// 错题本页：新用户为空 + 可操作引导；有收藏时保持原有大序号条目 +
// 标签筛选，并让“开始今日复习”真实可用。
import { useMemo, useState } from "react";
import { ArrowRight, ArrowUpRight, Check, Eye, RotateCcw, Trash2, X } from "lucide-react";
import { loadNotes, removeNote, reviewNote } from "../notebookStore";
import type { NoteItem } from "../data";
import { SectionHead } from "../ui";

const FILTERS = ["全部", "今天到期", "薄弱", "巩固中", "已掌握"] as const;

const NEW_QUESTION_PROMPT =
  "我想找一道我容易出错的数学题来检验自己。请先问我最近学过的内容，再给我一道由浅入深的题目，并在我作答后讲解。";

const GUIDE_STEPS = [
  {
    id: "ask",
    kind: "练",
    title: "去 AI 对话找一道题",
    description: "把你答错、卡住或想反复巩固的题目发给 AI。",
  },
  {
    id: "save",
    kind: "学",
    title: "点击“存入错题本”",
    description: "回答生成后，点击回答下方的收藏按钮；收藏会写入本地错题本。",
  },
  {
    id: "review",
    kind: "复习",
    title: "回到这里按节奏复习",
    description: "“今天到期”的条目会进入复习队列，按记忆结果自动安排下次复习。",
  },
] as const;

function ReviewCard({
  note,
  index,
  total,
  onDecide,
  onExit,
}: {
  note: NoteItem;
  index: number;
  total: number;
  onDecide: (noteId: string, outcome: "again" | "good") => void;
  onExit: () => void;
}) {
  const [revealed, setRevealed] = useState(false);

  return (
    <div className="review-session rise">
      <div className="review-session-head">
        <span className="tag red">今天到期</span>
        <span className="review-progress">
          {index + 1} / {total}
        </span>
        <button className="link-btn" onClick={onExit}>
          <X size={13} /> 退出复习
        </button>
      </div>
      <div className="review-question">{note.question}</div>
      {revealed ? (
        <div className="review-answer">{note.answerExcerpt}</div>
      ) : (
        <button className="btn btn-ghost" onClick={() => setRevealed(true)}>
          <Eye size={14} /> 显示答案
        </button>
      )}
      <div className="review-actions">
        <button className="btn btn-ghost" onClick={() => onDecide(note.id, "again")}>
          没记住 · 明天再复习
        </button>
        <button className="btn btn-red" onClick={() => onDecide(note.id, "good")}>
          <Check size={14} /> 记住了 · 进入下一轮
        </button>
      </div>
    </div>
  );
}

export function NotebookPage({
  goChat = () => {},
}: {
  goChat?: (prompt?: string) => void;
}) {
  const [filter, setFilter] = useState<(typeof FILTERS)[number]>("全部");
  const [notes, setNotes] = useState<NoteItem[]>(() => loadNotes());
  const [queue, setQueue] = useState<NoteItem[] | null>(null);
  const [queueTotal, setQueueTotal] = useState(0);

  const dueNotes = useMemo(() => notes.filter((n) => n.nextReview === "今天"), [notes]);
  const filtered = useMemo(() => {
    if (filter === "全部") return notes;
    if (filter === "今天到期") return notes.filter((n) => n.nextReview === "今天");
    return notes.filter((n) => n.mastery === filter);
  }, [filter, notes]);

  const startReview = () => {
    setQueue(dueNotes);
    setQueueTotal(dueNotes.length);
  };
  const exitReview = () => setQueue(null);
  const decideReview = (noteId: string, outcome: "again" | "good") => {
    setNotes(reviewNote(noteId, outcome));
    setQueue((current) => current?.filter((note) => note.id !== noteId) ?? null);
  };
  const deleteNote = (noteId: string) => setNotes(removeNote(noteId));

  return (
    <>
      <SectionHead
        num="01"
        title="收藏条目"
        note={`${dueNotes.length} 条今天到期 · 按间隔重复安排复习`}
        action={
          queue !== null ? (
            <button className="btn btn-ghost" onClick={exitReview}>
              <X size={14} /> 退出复习
            </button>
          ) : (
            <button className="btn btn-red" disabled={dueNotes.length === 0} onClick={startReview}>
              <RotateCcw size={14} /> 开始今日复习（{dueNotes.length}）
            </button>
          )
        }
      />

      <div className="notebook-head rise">
        {FILTERS.map((f) => (
          <button
            key={f}
            className={`filter-chip ${filter === f ? "active" : ""}`}
            onClick={() => setFilter(f)}
            aria-pressed={filter === f}
          >
            {f}
          </button>
        ))}
        <span className="section-note" style={{ marginLeft: "auto" }}>
          {notes.length === 0
            ? "新错题本默认空白 · 从 AI 对话中一键收藏"
            : "全部来自 AI 对话中的一键收藏"}
        </span>
      </div>

      {queue !== null ? (
        queue.length === 0 ? (
          <div className="review-session rise" style={{ animationDelay: "0.08s" }}>
            <div className="review-session-head">
              <span className="tag green">今日复习完成</span>
              <span className="review-progress">{queueTotal} 条已全部过完</span>
            </div>
            <div className="review-question">今天的到期条目已经复习完毕。</div>
            <div className="review-answer">
              薄弱条目会在明天再次出现；记住的条目会按间隔重复规则进入下一轮。
            </div>
            <div className="review-actions">
              <button className="btn btn-red" onClick={exitReview}>
                <Check size={14} /> 返回错题本
              </button>
            </div>
          </div>
        ) : (
          <ReviewCard
            note={queue[0]}
            index={queueTotal - queue.length}
            total={queueTotal}
            onDecide={decideReview}
            onExit={exitReview}
          />
        )
      ) : notes.length === 0 ? (
        <>
          <div className="rise" style={{ animationDelay: "0.08s" }}>
            {GUIDE_STEPS.map((step, index) => (
              <button
                key={step.id}
                className="t-item new-user-task"
                type="button"
                onClick={() => goChat(NEW_QUESTION_PROMPT)}
              >
                <span className="t-idx">{String(index + 1).padStart(2, "0")}</span>
                <span className="t-body">
                  <span className="t-title">{step.title}</span>
                  <span className="t-description">{step.description}</span>
                </span>
                <span className="t-meta">
                  <span className={`tag ${step.kind === "复习" ? "red" : step.kind === "练" ? "gold" : ""}`}>
                    {step.kind}
                  </span>
                  <ArrowUpRight size={13} className="new-user-task-arrow" />
                </span>
              </button>
            ))}
          </div>

          <button
            className="continue-strip rise"
            type="button"
            style={{ animationDelay: "0.14s" }}
            onClick={() => goChat(NEW_QUESTION_PROMPT)}
          >
            <ArrowRight size={18} style={{ flexShrink: 0 }} />
            <div style={{ flex: 1, minWidth: 0 }}>
              <div className="continue-title">去问第一道题，把错题本用起来</div>
              <div className="continue-preview">
                收藏后回到这里，条目会自动进入“今天到期”复习队列。
              </div>
            </div>
            <ArrowUpRight size={16} style={{ opacity: 0.6 }} />
          </button>
        </>
      ) : (
        <div className="rise" style={{ animationDelay: "0.08s" }}>
          {filtered.map((n, i) => (
            <div key={n.id} className="note-entry">
              <span className="note-idx">{String(i + 1).padStart(2, "0")}</span>
              <div className="note-main">
                <div className="note-q">{n.question}</div>
                <div className="note-a">{n.answerExcerpt}</div>
                <div className="note-foot">
                  {n.tags.map((t) => (
                    <span key={t} className="tag">{t}</span>
                  ))}
                  <span className={`tag ${n.mastery === "已掌握" ? "green" : n.mastery === "巩固中" ? "gold" : "red"}`}>
                    {n.mastery}
                  </span>
                  <span className="src">来自 {n.source} · 第 {n.reviewStage} 轮复习</span>
                  <span className="right">
                    <button
                      className="link-btn note-delete"
                      aria-label={`删除错题：${n.question}`}
                      onClick={() => deleteNote(n.id)}
                    >
                      <Trash2 size={13} />
                    </button>
                    {n.nextReview === "今天" ? (
                      <span className="review-due">今天到期</span>
                    ) : (
                      <span className="review-later">下次复习 {n.nextReview}</span>
                    )}
                  </span>
                </div>
              </div>
            </div>
          ))}
          {filtered.length === 0 && (
            <div style={{ textAlign: "center", color: "var(--ink-faint)", fontSize: 13.5, padding: "40px 0" }}>
              这个筛选下暂时没有内容。
            </div>
          )}
        </div>
      )}
    </>
  );
}

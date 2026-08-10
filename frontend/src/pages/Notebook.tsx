// 错题本页：大序号条目（索引/词汇表式）+ 间隔重复复习队列 + 标签筛选。
import { useState } from "react";
import { RotateCcw } from "lucide-react";
import { notes } from "../data";
import { SectionHead } from "../ui";

const FILTERS = ["全部", "今天到期", "薄弱", "巩固中", "已掌握"] as const;

export function NotebookPage() {
  const [filter, setFilter] = useState<(typeof FILTERS)[number]>("全部");
  const dueCount = notes.filter((n) => n.nextReview === "今天").length;

  const filtered = notes.filter((n) => {
    if (filter === "全部") return true;
    if (filter === "今天到期") return n.nextReview === "今天";
    return n.mastery === filter;
  });

  return (
    <>
      <SectionHead
        num="01"
        title="收藏条目"
        note={`${dueCount} 条今天到期 · 按间隔重复安排复习`}
        action={
          <button className="btn btn-red">
            <RotateCcw size={14} /> 开始今日复习（{dueCount}）
          </button>
        }
      />

      <div className="notebook-head rise">
        {FILTERS.map((f) => (
          <button
            key={f}
            className={`filter-chip ${filter === f ? "active" : ""}`}
            onClick={() => setFilter(f)}
          >
            {f}
          </button>
        ))}
        <span className="section-note" style={{ marginLeft: "auto" }}>
          全部来自 AI 对话中的一键收藏
        </span>
      </div>

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
    </>
  );
}

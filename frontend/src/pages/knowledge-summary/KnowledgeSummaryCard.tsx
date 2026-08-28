/** 知识总结列表卡片：只展示服务端快照，不创建本地收藏或复习状态。 */
import { ArrowRight, MessageCircle } from "lucide-react";
import type { KnowledgeSummaryListItem } from "../../types/knowledgeSummary";

const sectionLabels: Record<string, string> = {
  overview: "概览",
  definitions: "定义",
  theorems: "定理",
  formulas: "公式",
  properties: "性质",
  methods: "方法",
  pitfalls: "易混点",
};

export function KnowledgeSummaryCard({
  item,
  onOpen,
  onChat,
}: {
  item: KnowledgeSummaryListItem;
  onOpen: () => void;
  onChat: () => void;
}) {
  const sectionCount = Object.entries(item.section_counts).filter(([, count]) => count > 0).length;
  const reviewLabel =
    item.review_state === "conflict"
      ? "待确认"
      : item.review_state === "possible_duplicate"
        ? "可能重复"
        : null;

  return (
    <article className={`summary-card card rise ${reviewLabel ? "has-review" : ""}`}>
      <div className="summary-card-top">
        <span className="tag">{item.topic_group_title}</span>
        {reviewLabel && <span className="tag red">{reviewLabel}</span>}
      </div>
      <button className="summary-card-title" onClick={onOpen} type="button">
        {item.topic_title}
      </button>
      <p className="summary-card-excerpt">{item.overview_excerpt || "暂无概览，打开详情继续整理。"}</p>
      <div className="summary-card-meta">
        <span>{sectionCount} 个章节</span>
        <span>{item.source_count} 个来源</span>
        <span>{new Date(item.updated_at).toLocaleDateString("zh-CN")}</span>
      </div>
      <div className="summary-card-actions">
        <button className="link-btn" onClick={onOpen} type="button">
          查看详情 <ArrowRight size={13} />
        </button>
        <button className="link-btn" onClick={onChat} type="button">
          <MessageCircle size={13} /> 继续提问
        </button>
      </div>
      <div className="summary-card-sections">
        {Object.entries(item.section_counts)
          .filter(([, count]) => count > 0)
          .map(([section, count]) => (
            <span key={section}>{sectionLabels[section] ?? section} {count}</span>
          ))}
      </div>
    </article>
  );
}

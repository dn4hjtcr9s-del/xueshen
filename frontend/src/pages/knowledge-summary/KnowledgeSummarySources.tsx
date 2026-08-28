/** 来源区域：展示 Turn 聚合来源卡，并把可用来源跳转回对应 Conversation Turn。 */
import { ExternalLink, MessageSquare } from "lucide-react";
import { listKnowledgeSummarySources } from "../../api/knowledgeSummaries";
import { MemoryApiError } from "../../api/client";
import type { KnowledgeSummarySourceView } from "../../types/knowledgeSummary";
import { useCallback, useEffect, useState } from "react";

export function KnowledgeSummarySources({
  summaryId,
  onOpenChat,
}: {
  summaryId: string;
  onOpenChat: (threadId: string, turnId: string) => void;
}) {
  const [items, setItems] = useState<KnowledgeSummarySourceView[]>([]);
  const [cursor, setCursor] = useState<string | null>(null);
  const [hasMore, setHasMore] = useState(false);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  const loadFirstPage = useCallback(async () => {
    setError(null);
    setLoading(true);
    try {
      const page = await listKnowledgeSummarySources(summaryId);
      setItems(page.items);
      setCursor(page.next_cursor);
      setHasMore(page.has_more);
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : "来源加载失败");
    } finally {
      setLoading(false);
    }
  }, [summaryId]);

  const loadMore = useCallback(async () => {
    if (!cursor) return;
    setError(null);
    try {
      const page = await listKnowledgeSummarySources(summaryId, cursor);
      setItems((previous) => [...previous, ...page.items]);
      setCursor(page.next_cursor);
      setHasMore(page.has_more);
    } catch (cause) {
      if (cause instanceof MemoryApiError && cause.status === 422) {
        // summary 版本变化会使旧 cursor 失效；清空旧卡片并从第一页恢复。
        setItems([]);
        setCursor(null);
        setHasMore(false);
        await loadFirstPage();
        return;
      }
      setError(cause instanceof Error ? cause.message : "来源加载失败");
    }
  }, [cursor, loadFirstPage, summaryId]);

  // summary 切换时必须从第一页重新请求。
  useEffect(() => {
    setItems([]);
    setCursor(null);
    setHasMore(false);
    void loadFirstPage();
  }, [loadFirstPage]);

  return (
    <section className="summary-sources">
      <div className="summary-detail-section-head">
        <div>
          <span className="eyebrow">SOURCES · 来源</span>
          <h3>来自哪些问答</h3>
        </div>
        <span className="section-note">按问答发生时间排序</span>
      </div>
      {loading && <div className="skeleton-block" role="status">正在加载来源…</div>}
      {error && (
        <div className="inline-error">
          <span>{error}</span>
          <button className="link-btn" onClick={() => void loadFirstPage()} type="button">重试</button>
        </div>
      )}
      {!loading && !error && items.length === 0 && <div className="empty-state compact">暂无来源</div>}
      <div className="source-cards">
        {items.map((source) => (
          <div className="source-card" key={source.source_turn_id}>
            <div className="source-card-icon"><MessageSquare size={16} /></div>
            <div className="source-card-body">
              <div className="source-card-title">
                {source.question_excerpt || "本轮问答"}
                <span className={`source-status ${source.status}`}>
                  {source.status === "available" ? "可用" : "原对话已删除"}
                </span>
              </div>
              <div className="source-card-meta">
                {new Date(source.occurred_at).toLocaleString("zh-CN")} · 支撑消息 {source.support_message_ids.length} 条
              </div>
            </div>
            {source.status === "available" && (
              <button
                className="icon-btn"
                title="打开原对话"
                aria-label="打开原对话"
                onClick={() => onOpenChat(source.thread_id, source.turn_id)}
              >
                <ExternalLink size={14} />
              </button>
            )}
          </div>
        ))}
      </div>
      {hasMore && (
        <button className="btn btn-ghost" onClick={() => void loadMore()} type="button">
          加载更多来源
        </button>
      )}
    </section>
  );
}

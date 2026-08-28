/** 知识总结主页面：服务端分页列表 + 详情路由状态，不读取旧版本地收藏数据。 */
import { useEffect, useState } from "react";
import { RefreshCw, Search, SlidersHorizontal } from "lucide-react";
import { MemoryApiError } from "../api/client";
import { useKnowledgeSummaries } from "../hooks/useKnowledgeSummaries";
import { SectionHead } from "../ui";
import { KnowledgeSummaryCard } from "./knowledge-summary/KnowledgeSummaryCard";
import { KnowledgeSummaryDetail } from "./knowledge-summary/KnowledgeSummaryDetail";

export function KnowledgeSummariesPage({
  onOpenChat,
  onFeatureUnavailable,
  targetSummaryId = null,
  onTargetConsumed = () => {},
}: {
  onOpenChat: (threadId: string, turnId: string) => void;
  onFeatureUnavailable: () => void;
  targetSummaryId?: string | null;
  onTargetConsumed?: () => void;
}) {
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const state = useKnowledgeSummaries();
  const filteredEmpty = !state.loading && state.items.length === 0 && Boolean(state.query || state.topicGroup || state.reviewState);
  const errorMessage = state.error instanceof Error ? state.error.message : "知识总结加载失败";

  useEffect(() => {
    if (state.error instanceof MemoryApiError && state.error.status === 404) onFeatureUnavailable();
  }, [onFeatureUnavailable, state.error]);

  // Chat 的 needs_review 直接打开受影响总结的详情，消费目标后不影响用户返回列表。
  useEffect(() => {
    if (!targetSummaryId) return;
    setSelectedId(targetSummaryId);
    onTargetConsumed();
  }, [onTargetConsumed, targetSummaryId]);

  if (selectedId) {
    return (
      <KnowledgeSummaryDetail
        summaryId={selectedId}
        onBack={() => setSelectedId(null)}
        onOpenChat={onOpenChat}
        onDeleted={() => { setSelectedId(null); void state.reload(); }}
      />
    );
  }

  return (
    <div className="knowledge-summaries-page">
      <SectionHead
        num="01"
        title="知识总结"
        note="对话沉淀 · 可复用、可编辑、可追溯"
        action={<button className="btn btn-ghost" onClick={() => void state.reload()} type="button"><RefreshCw size={14} /> 刷新</button>}
      />
      <div className="summary-intro rise">
        <div><span className="eyebrow">KNOWLEDGE SUMMARY · 对话沉淀</span><h2>把问答整理成自己的数学知识</h2><p>回答完成后，AI 会在后台提炼定义、定理、公式和方法；你可以继续编辑，也可以回到原问答核对来源。</p></div>
        <div className="summary-intro-mark">∴</div>
      </div>
      <div className="summary-toolbar rise">
        <label className="summary-search"><Search size={15} /><input value={state.query} onChange={(e) => state.setQuery(e.target.value)} placeholder="搜索知识点、公式、方法…" /></label>
        <label className="summary-select"><SlidersHorizontal size={14} /><select value={state.topicGroup} onChange={(e) => state.setTopicGroup(e.target.value)}><option value="">全部大主题</option>{state.topicGroups.map((group) => <option key={group.key} value={group.title}>{group.title}（{group.summary_count}）</option>)}</select></label>
        <div className="summary-filter-group" role="group" aria-label="状态筛选">
          {[["", "全部"], ["conflict", "待确认"], ["possible_duplicate", "可能重复"]].map(([value, label]) => <button key={value} className={`filter-chip ${state.reviewState === value ? "active" : ""}`} onClick={() => state.setReviewState(value as typeof state.reviewState)} type="button">{label}</button>)}
        </div>
      </div>
      {state.loading && <div className="summary-list-loading" role="status"><div className="skeleton-block">正在加载知识总结…</div><div className="skeleton-block">正在加载知识总结…</div></div>}
      {Boolean(state.error) && <div className="inline-error"><span>{errorMessage}</span><button className="link-btn" onClick={() => void state.reload()} type="button">重试</button></div>}
      {!state.loading && !state.error && filteredEmpty && <div className="empty-state summary-empty"><strong>没有匹配的知识总结</strong><span>可以清除筛选，或回到 AI 对话继续提问。</span><button className="btn btn-ghost" onClick={() => { state.setQuery(""); state.setTopicGroup(""); state.setReviewState(""); }} type="button">清除筛选</button></div>}
      {!state.loading && !state.error && !filteredEmpty && state.items.length === 0 && <div className="empty-state summary-empty"><strong>还没有知识总结</strong><span>从 AI 对话提出一个数学问题，回答完成后 AI 会自动提炼可复用知识。</span><button className="btn btn-red" onClick={() => onOpenChat("", "")} type="button">去问一个数学问题</button></div>}
      {!state.loading && !state.error && state.items.length > 0 && <>
        <div className="summary-list">{state.items.map((item) => <KnowledgeSummaryCard key={item.summary_id} item={item} onOpen={() => setSelectedId(item.summary_id)} onChat={() => onOpenChat("", "")} />)}</div>
        {state.hasMore && <div className="summary-load-more"><button className="btn btn-ghost" disabled={state.loadingMore} onClick={() => void state.loadMore()} type="button">{state.loadingMore ? "加载中…" : "加载更多"}</button></div>}
        {state.loadMoreError && <div className="inline-error summary-load-more-error"><span>{state.loadMoreError instanceof Error ? state.loadMoreError.message : "加载更多知识总结失败"}</span><button className="link-btn" onClick={() => void state.loadMore()} type="button">重试</button></div>}
      </>}
    </div>
  );
}

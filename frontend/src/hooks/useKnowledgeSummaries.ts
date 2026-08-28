/** 知识总结列表数据钩子：管理首屏、游标续页和筛选，页面不直接处理请求细节。 */
import { useCallback, useEffect, useState } from "react";
import { MemoryApiError } from "../api/client";
import {
  listKnowledgeSummaries,
  listKnowledgeSummaryTopicGroups,
} from "../api/knowledgeSummaries";
import type {
  KnowledgeSummaryListItem,
  KnowledgeSummaryTopicGroup,
} from "../types/knowledgeSummary";

export function useKnowledgeSummaries() {
  const [items, setItems] = useState<KnowledgeSummaryListItem[]>([]);
  const [topicGroups, setTopicGroups] = useState<KnowledgeSummaryTopicGroup[]>([]);
  const [query, setQuery] = useState("");
  const [topicGroup, setTopicGroup] = useState("");
  const [reviewState, setReviewState] = useState<"" | "possible_duplicate" | "conflict">("");
  const [cursor, setCursor] = useState<string | null>(null);
  const [hasMore, setHasMore] = useState(false);
  const [loading, setLoading] = useState(true);
  const [loadingMore, setLoadingMore] = useState(false);
  const [error, setError] = useState<unknown>(null);
  const [loadMoreError, setLoadMoreError] = useState<unknown>(null);

  const loadFirstPage = useCallback(async () => {
    setLoading(true);
    setError(null);
    setLoadMoreError(null);
    try {
      const response = await listKnowledgeSummaries({
        query: query.trim() || undefined,
        topicGroup: topicGroup || undefined,
        reviewState: reviewState || undefined,
        sort: query.trim() ? "relevance_desc" : "updated_desc",
        limit: 20,
      });
      setItems(response.items);
      setCursor(response.next_cursor);
      setHasMore(response.has_more);
    } catch (cause) {
      setError(cause);
    } finally {
      setLoading(false);
    }
  }, [query, reviewState, topicGroup]);

  const loadMore = useCallback(async () => {
    if (!cursor || loadingMore) return;
    setLoadingMore(true);
    setLoadMoreError(null);
    try {
      const response = await listKnowledgeSummaries({
        query: query.trim() || undefined,
        topicGroup: topicGroup || undefined,
        reviewState: reviewState || undefined,
        sort: query.trim() ? "relevance_desc" : "updated_desc",
        cursor,
        limit: 20,
      });
      setItems((previous) => [...previous, ...response.items]);
      setCursor(response.next_cursor);
      setHasMore(response.has_more);
    } catch (cause) {
      if (cause instanceof MemoryApiError && cause.status === 422) {
        // 版本变化会使旧 cursor 失效；清空分页结果并按当前筛选重新加载第一页。
        setItems([]);
        setCursor(null);
        setHasMore(false);
        await loadFirstPage();
        return;
      }
      // 续页失败仅保留在底部，不能遮住已经成功加载的知识总结卡片。
      setLoadMoreError(cause);
    } finally {
      setLoadingMore(false);
    }
  }, [cursor, loadFirstPage, loadingMore, query, reviewState, topicGroup]);

  useEffect(() => {
    void loadFirstPage();
  }, [loadFirstPage]);

  useEffect(() => {
    void listKnowledgeSummaryTopicGroups().then((response) => setTopicGroups(response.items)).catch(() => {
      setTopicGroups([]);
    });
  }, []);

  return {
    items,
    topicGroups,
    query,
    setQuery,
    topicGroup,
    setTopicGroup,
    reviewState,
    setReviewState,
    loading,
    loadingMore,
    error,
    loadMoreError,
    hasMore,
    reload: loadFirstPage,
    loadMore,
  };
}

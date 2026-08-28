/** Chat 生成状态钩子：按 Turn 查询当前 Job，采用 REST polling，不改 Conversation SSE。 */
import { useCallback, useEffect, useRef, useState } from "react";
import {
  createKnowledgeSummaryGeneration,
  getCurrentTurnKnowledgeSummaryGeneration,
} from "../api/knowledgeSummaries";
import type { KnowledgeSummaryGenerationStatusResponse } from "../types/knowledgeSummary";

const ACTIVE_STATUSES = new Set(["pending", "processing", "retry_wait"]);

export function useKnowledgeSummaryGeneration(threadId: string, turnId: string) {
  const [generation, setGeneration] = useState<KnowledgeSummaryGenerationStatusResponse | null>(null);
  const [unavailable, setUnavailable] = useState(false);
  const [writeUnavailable, setWriteUnavailable] = useState(false);
  const [loading, setLoading] = useState(false);
  const [creating, setCreating] = useState(false);
  const [pollStartedAt, setPollStartedAt] = useState<number | null>(null);
  const timerRef = useRef<number | null>(null);
  const unavailableRef = useRef(false);

  const refresh = useCallback(async () => {
    if (!threadId || !turnId || unavailableRef.current) return;
    setLoading(true);
    try {
      const response = await getCurrentTurnKnowledgeSummaryGeneration(threadId, turnId);
      setGeneration(response.generation);
      setUnavailable(false);
      if (response.generation && ACTIVE_STATUSES.has(response.generation.status)) {
        setPollStartedAt((previous) => previous ?? Date.now());
      } else {
        setPollStartedAt(null);
      }
    } catch (cause) {
      if (cause instanceof Error && "status" in cause && (cause as { status?: number }).status === 404) {
        // 后端未挂载 Generation 路由时停止后续轮询，避免发布错配无限请求。
        unavailableRef.current = true;
        setUnavailable(true);
      }
    } finally {
      setLoading(false);
    }
  }, [threadId, turnId]);

  useEffect(() => {
    unavailableRef.current = false;
    setGeneration(null);
    setUnavailable(false);
    setWriteUnavailable(false);
    setPollStartedAt(null);
    void refresh();
  }, [threadId, turnId, refresh]);

  useEffect(() => {
    if (!pollStartedAt || !generation || !ACTIVE_STATUSES.has(generation.status)) return;
    const elapsed = Date.now() - pollStartedAt;
    if (elapsed >= 60_000) return;
    const delay = elapsed < 6_000 ? 2_000 : 5_000;
    timerRef.current = window.setTimeout(() => void refresh(), delay);
    return () => {
      if (timerRef.current !== null) window.clearTimeout(timerRef.current);
    };
  }, [generation, pollStartedAt, refresh]);

  useEffect(() => {
    const onFocus = () => void refresh();
    window.addEventListener("focus", onFocus);
    return () => window.removeEventListener("focus", onFocus);
  }, [refresh]);

  const create = useCallback(async (force: boolean) => {
    setCreating(true);
    try {
      await createKnowledgeSummaryGeneration(threadId, turnId, {
        client_request_id: crypto.randomUUID(),
        force,
      });
      setWriteUnavailable(false);
      await refresh();
    } catch (cause) {
      if (cause instanceof Error && "status" in cause && (cause as { status?: number }).status === 404) {
        // 发布错配只关闭写入口，不能清空已读取的历史状态。
        setWriteUnavailable(true);
      } else {
        throw cause;
      }
    } finally {
      setCreating(false);
    }
  }, [refresh, threadId, turnId]);

  return { generation, unavailable, writeUnavailable, loading, creating, refresh, create };
}

// useConversation：会话列表 + 详情 + 发送/取消的状态钩子（方案 §18.1/§18.2）。
import { useCallback, useEffect, useRef, useState } from "react";
import {
  cancelTurn,
  createConversation,
  createTurn,
  deleteConversation,
  getConversation,
  listConversations,
} from "../api/conversations";
import { idempotencyKey } from "../api/client";
import type {
  ConversationDetail,
  ConversationMessage,
  ConversationThread,
  CreateTurnResponse,
} from "../types/conversation";

export interface UseConversationResult {
  threads: ConversationThread[];
  activeThreadId: string | null;
  detail: ConversationDetail | null;
  loading: boolean;
  error: string | null;
  sending: boolean;
  openThread: (threadId: string) => Promise<void>;
  newThread: () => Promise<void>;
  send: (content: string) => Promise<CreateTurnResponse | null>;
  cancel: (threadId: string, turnId: string) => Promise<void>;
  remove: (threadId: string) => Promise<void>;
  refreshList: () => Promise<void>;
}

export function useConversation(): UseConversationResult {
  const [threads, setThreads] = useState<ConversationThread[]>([]);
  const [activeThreadId, setActiveThreadId] = useState<string | null>(null);
  const [detail, setDetail] = useState<ConversationDetail | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [sending, setSending] = useState(false);
  const pendingIdempotencyKeyRef = useRef<string | null>(null);

  const refreshList = useCallback(async () => {
    try {
      const page = await listConversations();
      setThreads(page.items);
    } catch {
      // 列表失败静默（主对话仍可用）
    }
  }, []);

  useEffect(() => {
    void refreshList();
  }, [refreshList]);

  const openThread = useCallback(async (threadId: string) => {
    setActiveThreadId(threadId);
    setLoading(true);
    setError(null);
    try {
      const d = await getConversation(threadId);
      setDetail(d);
    } catch (e) {
      setError(e instanceof Error ? e.message : "加载会话失败");
    } finally {
      setLoading(false);
    }
  }, []);

  const newThread = useCallback(async () => {
    setSending(true);
    try {
      const created = await createConversation();
      setActiveThreadId(created.thread_id);
      setDetail({
        thread_id: created.thread_id,
        title: "",
        version: 0,
        status: "active",
        messages: [],
        next_cursor: null,
        has_more: false,
      });
      await refreshList();
    } catch (e) {
      setError(e instanceof Error ? e.message : "创建会话失败");
    } finally {
      setSending(false);
    }
  }, [refreshList]);

  const send = useCallback(
    async (content: string): Promise<CreateTurnResponse | null> => {
      if (!activeThreadId || !detail) {
        setError("请先创建或选择一个会话");
        return null;
      }
      setSending(true);
      setError(null);
      const clientRequestId = pendingIdempotencyKeyRef.current ?? idempotencyKey();
      pendingIdempotencyKeyRef.current = clientRequestId;
      let created: CreateTurnResponse | null = null;
      try {
        created = await createTurn(activeThreadId, {
          client_request_id: clientRequestId,
          content,
          expected_thread_version: detail.version,
        });
        pendingIdempotencyKeyRef.current = null;
        // P2（评审）：发送成功后用响应的 thread_version 更新本地版本，
        // 后续发送不再使用过期版本（否则必然 409）。
        setDetail((prev) =>
          prev ? { ...prev, version: created?.thread_version ?? prev.version } : prev,
        );
        // 乐观更新用户消息
        const userMessage: ConversationMessage = {
          message_id: created.user_message_id,
          thread_id: activeThreadId,
          turn_id: created.turn_id,
          role: "user",
          content,
          status: "completed",
          sequence: detail.messages.length + 1,
          occurred_at: new Date().toISOString(),
          completed_at: new Date().toISOString(),
        };
        setDetail((prev) => (prev ? { ...prev, messages: [...prev.messages, userMessage] } : prev));
        return created;
      } catch (e) {
        // P2（评审）：409 THREAD_VERSION_CONFLICT 时用响应携带的 current_version
        // 刷新本地版本并提示（附录 A.4）。
        const conflict = e as { code?: string; currentVersion?: number | null; message?: string };
        if (conflict?.code === "THREAD_VERSION_CONFLICT" && conflict.currentVersion != null) {
          setDetail((prev) =>
            prev ? { ...prev, version: conflict.currentVersion ?? prev.version } : prev,
          );
          setError("会话版本已变化，请重试");
        } else {
          setError(e instanceof Error ? e.message : "发送失败");
        }
        return null;
      } finally {
        setSending(false);
      }
    },
    [activeThreadId, detail],
  );

  const cancel = useCallback(async (threadId: string, turnId: string) => {
    try {
      await cancelTurn(threadId, turnId);
    } catch {
      // 取消失败不阻塞 UI
    }
  }, []);

  const remove = useCallback(
    async (threadId: string) => {
      try {
        await deleteConversation(threadId);
        if (activeThreadId === threadId) {
          setActiveThreadId(null);
          setDetail(null);
        }
        await refreshList();
      } catch (e) {
        setError(e instanceof Error ? e.message : "删除会话失败");
      }
    },
    [activeThreadId, refreshList],
  );

  return {
    threads,
    activeThreadId,
    detail,
    loading,
    error,
    sending,
    openThread,
    newThread,
    send,
    cancel,
    remove,
    refreshList,
  };
}

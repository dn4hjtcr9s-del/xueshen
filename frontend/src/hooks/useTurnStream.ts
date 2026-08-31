// useTurnStream：SSE 回答与可观察流水线状态钩子（方案 §18.2 / §18.3）。
import { useCallback, useEffect, useRef, useState } from "react";
import { startTurnEventStream, type StreamEvent } from "../api/turnEvents";
import type {
  Citation,
  TurnProgressData,
  TurnProgressItem,
} from "../types/conversation";

export type StreamStatus =
  | "idle"
  | "connecting"
  | "streaming"
  | "completed"
  | "failed"
  | "cancelled";

export interface TurnStreamState {
  status: StreamStatus;
  answer: string;
  citations: Citation[];
  followups: string[];
  assistantMessageId: string | null;
  degradedFlags: string[];
  error: { code: string; message: string } | null;
  memorySubmission: string | null;
  progress: TurnProgressItem[];
}

export const initialTurnStreamState: TurnStreamState = {
  status: "idle",
  answer: "",
  citations: [],
  followups: [],
  assistantMessageId: null,
  degradedFlags: [],
  error: null,
  memorySubmission: null,
  progress: [],
};

function appendUnique<T>(current: T[], incoming: T[]): T[] {
  return Array.from(new Set([...current, ...incoming]));
}

function progressOperationKey(item: TurnProgressData): string {
  const subqueryId = item.metadata?.subquery_id;
  return `${item.stage}:${typeof subqueryId === "string" ? subqueryId : ""}`;
}

function appendProgressItem(
  progress: TurnProgressItem[],
  item: TurnProgressItem,
): TurnProgressItem[] {
  if (item.status !== "started") {
    const operationKey = progressOperationKey(item);
    let pendingIndex = -1;
    for (let index = progress.length - 1; index >= 0; index -= 1) {
      const current = progress[index];
      if (current.status === "started" && progressOperationKey(current) === operationKey) {
        pendingIndex = index;
        break;
      }
    }
    if (pendingIndex >= 0) {
      const updated = [...progress];
      updated[pendingIndex] = item;
      return updated.slice(-40);
    }
  }
  return [...progress, item].slice(-40);
}

/** 纯 reducer 便于验证连续 SSE 事件不会因 React 批处理而丢失。 */
export function reduceTurnStreamState(
  state: TurnStreamState,
  event: StreamEvent,
): TurnStreamState {
  switch (event.event_type) {
    case "turn.accepted":
    case "turn.started":
      return { ...state, status: "streaming" };
    case "turn.progress": {
      const data = event.data as unknown as TurnProgressData;
      if (!data.stage || !data.status || !data.title) return state;
      if (state.progress.some((item) => item.eventId === event.event_id)) return state;
      const item: TurnProgressItem = {
        ...data,
        metadata: data.metadata ?? {},
        eventId: event.event_id,
        sequence: event.sequence,
        occurredAt: event.occurred_at,
      };
      return {
        ...state,
        status: "streaming",
        // 同一操作的完成事件替换进行中项，避免流程结束后仍显示旋转状态。
        // 同时保留上限，防止异常重试无限增长浏览器内存。
        progress: appendProgressItem(state.progress, item),
      };
    }
    case "answer.delta":
      return {
        ...state,
        status: "streaming",
        answer: state.answer + String((event.data as { text_delta?: string }).text_delta ?? ""),
      };
    case "citation.available": {
      const citation = (event.data as { citation?: Citation }).citation;
      if (!citation || state.citations.some((item) => item.citation_id === citation.citation_id)) {
        return state;
      }
      return { ...state, citations: [...state.citations, citation] };
    }
    case "turn.degraded": {
      const flags = (event.data as { flags?: string[] }).flags ?? [];
      return { ...state, degradedFlags: appendUnique(state.degradedFlags, flags) };
    }
    case "memory.submission":
      return {
        ...state,
        memorySubmission: (event.data as { status?: string }).status ?? null,
      };
    case "answer.completed": {
      const data = event.data as {
        assistant_message_id?: string;
        answer?: string;
        citations?: Citation[];
        followups?: string[];
        degraded_flags?: string[];
      };
      return {
        ...state,
        status: "completed",
        answer: data.answer ?? state.answer,
        citations: data.citations ?? state.citations,
        followups: data.followups ?? [],
        assistantMessageId: data.assistant_message_id ?? null,
        degradedFlags: data.degraded_flags ?? state.degradedFlags,
      };
    }
    case "turn.failed":
      return {
        ...state,
        status: "failed",
        error: (event.data as { error?: { code: string; message: string } }).error ?? null,
      };
    case "turn.cancelled":
      return { ...state, status: "cancelled" };
  }
}

export function useTurnStream(
  streamUrl: string | null,
  options: { resumeSequence?: number | null; onAuthFailure?: () => Promise<boolean> } = {},
) {
  const [state, setState] = useState<TurnStreamState>(initialTurnStreamState);
  const stopRef = useRef<(() => void) | null>(null);

  const stop = useCallback(() => {
    stopRef.current?.();
    stopRef.current = null;
  }, []);

  const reset = useCallback(() => {
    stop();
    setState(initialTurnStreamState);
  }, [stop]);

  useEffect(() => {
    if (!streamUrl) return;
    setState({ ...initialTurnStreamState, status: "connecting" });
    stopRef.current = startTurnEventStream({
      url: streamUrl,
      onEvent: (event) => setState((current) => reduceTurnStreamState(current, event)),
      resumeSequence: options.resumeSequence ?? null,
      onAuthFailure: options.onAuthFailure,
      onError: (error) => {
        if (error.message === "EVENT_REPLAY_EXPIRED") {
          setState((current) => ({
            ...current,
            status: "failed",
            error: { code: "EVENT_REPLAY_EXPIRED", message: "事件流已过期，请刷新" },
          }));
        }
      },
    });
    return stop;
    // options 由页面按会话生命周期固定；只在 URL 切换时重新建流。
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [streamUrl, stop]);

  return { state, stop, reset };
}

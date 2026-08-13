// useTurnStream：SSE 事件流状态钩子（方案 §18.2 / §18.3）。
import { useCallback, useEffect, useRef, useState } from "react";
import { startTurnEventStream, type StreamEvent } from "../api/turnEvents";
import type { Citation } from "../types/conversation";

export type StreamStatus = "idle" | "connecting" | "streaming" | "completed" | "failed" | "cancelled";

export interface TurnStreamState {
  status: StreamStatus;
  answer: string;
  citations: Citation[];
  followups: string[];
  assistantMessageId: string | null;
  degradedFlags: string[];
  error: { code: string; message: string } | null;
  memorySubmission: string | null;
}

const initialState: TurnStreamState = {
  status: "idle",
  answer: "",
  citations: [],
  followups: [],
  assistantMessageId: null,
  degradedFlags: [],
  error: null,
  memorySubmission: null,
};

export function useTurnStream(
  streamUrl: string | null,
  options: { resumeSequence?: number | null; onAuthFailure?: () => Promise<boolean> } = {},
) {
  const [state, setState] = useState<TurnStreamState>(initialState);
  const stopRef = useRef<(() => void) | null>(null);
  const stateRef = useRef(state);
  stateRef.current = state;

  const stop = useCallback(() => {
    stopRef.current?.();
    stopRef.current = null;
  }, []);

  useEffect(() => {
    if (!streamUrl) {
      setState(initialState);
      return;
    }
    setState({ ...initialState, status: "connecting" });
    const handleEvent = (event: StreamEvent) => {
      const prev = stateRef.current;
      switch (event.event_type) {
        case "turn.accepted":
        case "turn.started":
          setState({ ...prev, status: "streaming" });
          break;
        case "answer.delta":
          setState({
            ...prev,
            status: "streaming",
            answer: prev.answer + String((event.data as { text_delta?: string }).text_delta ?? ""),
          });
          break;
        case "citation.available":
          setState({
            ...prev,
            citations: [...prev.citations, (event.data as { citation: Citation }).citation],
          });
          break;
        case "turn.degraded": {
          const flags = (event.data as { flags?: string[] }).flags ?? [];
          setState({ ...prev, degradedFlags: [...prev.degradedFlags, ...flags] });
          break;
        }
        case "memory.submission":
          setState({
            ...prev,
            memorySubmission: (event.data as { status?: string }).status ?? null,
          });
          break;
        case "answer.completed": {
          const data = event.data as {
            assistant_message_id?: string;
            answer?: string;
            citations?: Citation[];
            followups?: string[];
            degraded_flags?: string[];
          };
          setState({
            ...prev,
            status: "completed",
            answer: data.answer ?? prev.answer,
            citations: data.citations ?? prev.citations,
            followups: data.followups ?? [],
            assistantMessageId: data.assistant_message_id ?? null,
            degradedFlags: data.degraded_flags ?? prev.degradedFlags,
          });
          break;
        }
        case "turn.failed":
          setState({
            ...prev,
            status: "failed",
            error: (event.data as { error?: { code: string; message: string } }).error ?? null,
          });
          break;
        case "turn.cancelled":
          setState({ ...prev, status: "cancelled" });
          break;
      }
    };
    stopRef.current = startTurnEventStream({
      url: streamUrl,
      onEvent: handleEvent,
      resumeSequence: options.resumeSequence ?? null,
      onAuthFailure: options.onAuthFailure,
      onError: (error) => {
        if (error.message === "EVENT_REPLAY_EXPIRED") {
          setState((prevState) => ({ ...prevState, status: "failed", error: { code: "EVENT_REPLAY_EXPIRED", message: "事件流已过期，请刷新" } }));
        }
      },
    });
    return stop;
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [streamUrl]);

  return { state, stop };
}

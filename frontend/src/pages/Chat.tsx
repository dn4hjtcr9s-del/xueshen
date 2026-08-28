/** AI 对话页：Conversation SSE 主链路 + 知识总结 Generation REST 状态。 */
import { useCallback, useEffect, useRef, useState } from "react";
import ReactMarkdown from "react-markdown";
import remarkMath from "remark-math";
import rehypeKatex from "rehype-katex";
import { Copy, Plus, SendHorizontal, ThumbsDown, ThumbsUp } from "lucide-react";
import { useConversation } from "../hooks/useConversation";
import { useKnowledgeSummaryGeneration } from "../hooks/useKnowledgeSummaryGeneration";
import { useTurnStream } from "../hooks/useTurnStream";
import type { ConversationMessage } from "../types/conversation";

export function ChatPage({
  initialPrompt = "",
  chatTarget = null,
  onOpenSummary = () => {},
}: {
  initialPrompt?: string;
  chatTarget?: { threadId: string; turnId: string } | null;
  onOpenSummary?: (summaryId?: string) => void;
}) {
  const {
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
  } = useConversation();
  const [input, setInput] = useState("");
  const [streamUrl, setStreamUrl] = useState<string | null>(null);
  const [activeTurnId, setActiveTurnId] = useState<string | null>(null);
  const [messages, setMessages] = useState<ConversationMessage[]>([]);
  const [pendingUser, setPendingUser] = useState<string | null>(null);
  const inputRef = useRef<HTMLTextAreaElement>(null);
  const { state: stream } = useTurnStream(streamUrl);

  useEffect(() => {
    if (!initialPrompt) return;
    setInput(initialPrompt);
    inputRef.current?.focus();
  }, [initialPrompt]);

  useEffect(() => {
    if (chatTarget?.threadId) void openThread(chatTarget.threadId);
  }, [chatTarget?.threadId, openThread]);

  useEffect(() => {
    if (detail) setMessages(detail.messages);
    else setMessages([]);
    setPendingUser(null);
    setStreamUrl(null);
    setActiveTurnId(null);
  }, [detail]);

  useEffect(() => {
    if (!chatTarget?.turnId || !detail) return;
    const target = document.querySelector(`[data-turn-id="${chatTarget.turnId}"]`);
    target?.scrollIntoView({ behavior: "smooth", block: "center" });
  }, [chatTarget?.turnId, detail]);

  useEffect(() => {
    if (!['completed', 'failed', 'cancelled'].includes(stream.status)) return;
    setStreamUrl(null);
    setActiveTurnId(null);
    setPendingUser(null);
    if (activeThreadId) void openThread(activeThreadId);
  }, [stream.status, activeThreadId, openThread]);

  const handleSend = useCallback(async () => {
    const content = input.trim();
    if (!content || sending) return;
    setInput("");
    setPendingUser(content);
    const response = await send(content);
    if (response) {
      setActiveTurnId(response.turn_id);
      setStreamUrl(response.event_stream_path);
    }
    inputRef.current?.focus();
  }, [input, sending, send]);

  const handleFollowup = useCallback((text: string) => {
    setInput(text);
    inputRef.current?.focus();
  }, []);

  const handleCancel = useCallback(() => {
    if (activeThreadId && activeTurnId) {
      void cancel(activeThreadId, activeTurnId);
      setPendingUser(null);
    }
  }, [activeThreadId, activeTurnId, cancel]);

  return (
    <div className="chat-layout" style={{ maxWidth: 1160, margin: "0 auto" }}>
      <div className="conv-list rise">
        <button className="btn btn-primary conv-new" onClick={() => void newThread()}>
          <Plus size={15} /> 新对话
        </button>
        {threads.map((thread) => (
          <button
            key={thread.thread_id}
            className={`conv-item ${thread.thread_id === activeThreadId ? "active" : ""}`}
            onClick={() => void openThread(thread.thread_id)}
            type="button"
          >
            <div className="conv-line">
              <span className="conv-title">{thread.title || "新对话"}</span>
              <span className="conv-leader" />
              <span className="conv-time">{new Date(thread.updated_at).toLocaleDateString()}</span>
            </div>
          </button>
        ))}
        {activeThreadId && <button className="chip-btn conv-remove" onClick={() => void remove(activeThreadId)}>删除当前会话</button>}
      </div>

      <div className="chat-main rise" style={{ animationDelay: "0.08s" }}>
        <div className="chat-scroll">
          {loading && <div className="loading-hint">加载会话…</div>}
          {error && <div className="error-hint">{error}</div>}
          {messages.map((message) => (
            <MessageRow
              key={message.message_id}
              message={message}
              threadId={message.thread_id}
              onOpenSummary={onOpenSummary}
            />
          ))}
          {pendingUser && <div className="msg user"><div className="msg-body"><div className="msg-text">{pendingUser}</div></div></div>}
          {stream.status !== "idle" && (
            <div className="msg assistant">
              <div className="msg-body">
                <div className="msg-role">学神 AI · 讲解模式</div>
                {stream.status === "connecting" && <div className="loading-hint">正在连接…</div>}
                {stream.answer && <div className="msg-text md"><ReactMarkdown remarkPlugins={[remarkMath]} rehypePlugins={[rehypeKatex]}>{stream.answer}</ReactMarkdown></div>}
                {stream.citations.length > 0 && <div className="source-box"><div className="source-head">注释 · NOTES & SOURCES</div>{stream.citations.map((source, index) => <div key={source.citation_id} className="source-item"><span className="source-num">[{index + 1}]</span><div><div className="source-cite">{source.book_name} · {source.chapter_path.join("/")} <span className="page">P.{source.page_start ?? "—"}</span></div><div className="source-snippet">「{source.snippet}」</div></div></div>)}</div>}
                {stream.status === "streaming" && <button className="chip-btn" onClick={handleCancel}>停止生成</button>}
                {stream.status === "failed" && <div className="error-hint">{stream.error?.message ?? "回答失败，请重试"}</div>}
                {stream.status === "cancelled" && <div className="loading-hint">已取消</div>}
                {stream.memorySubmission === "accepted" && <div className="memory-hint">记忆请求已接收</div>}
                {stream.memorySubmission === "retrying" && <div className="memory-hint">记忆请求尚未确认，系统将重试</div>}
                {stream.status === "completed" && activeThreadId && activeTurnId && <KnowledgeSummaryGenerationActions threadId={activeThreadId} turnId={activeTurnId} onOpenSummary={onOpenSummary} />}
                <div className="msg-actions"><button className="chip-btn" onClick={() => void navigator.clipboard?.writeText(stream.answer)}><Copy size={13} /> 复制</button><button className="chip-btn"><ThumbsUp size={13} /></button><button className="chip-btn"><ThumbsDown size={13} /></button></div>
              </div>
            </div>
          )}
        </div>
        {stream.status === "completed" && stream.followups.length > 0 && <div className="followups">{stream.followups.map((followup) => <button key={followup} className="followup-btn" onClick={() => handleFollowup(followup)}>{followup}</button>)}</div>}
        <div className="chat-input-row">
          <textarea ref={inputRef} className="chat-input" rows={2} value={input} onChange={(event) => setInput(event.target.value)} onKeyDown={(event) => { if (event.key === "Enter" && !event.shiftKey) { event.preventDefault(); void handleSend(); } }} placeholder="继续追问，或开始一个新问题… 支持 LaTeX，如 $\int_0^1 x^2 dx$" />
          <button className="btn btn-red" style={{ alignSelf: "flex-end" }} onClick={() => void handleSend()} disabled={sending}><SendHorizontal size={15} /> 发送</button>
        </div>
      </div>
    </div>
  );
}

function MessageRow({
  message,
  threadId,
  onOpenSummary,
}: {
  message: ConversationMessage;
  threadId: string;
  onOpenSummary: (summaryId?: string) => void;
}) {
  return (
    <div className={`msg ${message.role}`} data-turn-id={message.turn_id}>
      <div className="msg-body">
        <div className="msg-role" style={message.role === "user" ? { textAlign: "right" } : undefined}>{message.role === "user" ? "你 · 提问" : "学神 AI · 讲解模式"}</div>
        {message.role === "user" ? <div className="msg-text">{message.content}</div> : <div className="msg-text md"><ReactMarkdown remarkPlugins={[remarkMath]} rehypePlugins={[rehypeKatex]}>{message.content}</ReactMarkdown></div>}
        {message.role === "assistant" && <><KnowledgeSummaryGenerationActions threadId={threadId} turnId={message.turn_id} onOpenSummary={onOpenSummary} /><div className="msg-actions"><button className="chip-btn" onClick={() => void navigator.clipboard?.writeText(message.content)}><Copy size={13} /> 复制</button><button className="chip-btn"><ThumbsUp size={13} /></button><button className="chip-btn"><ThumbsDown size={13} /></button></div></>}
      </div>
    </div>
  );
}

function KnowledgeSummaryGenerationActions({
  threadId,
  turnId,
  onOpenSummary = () => {},
}: {
  threadId: string;
  turnId: string;
  onOpenSummary?: (summaryId?: string) => void;
}) {
  const readEnabled = import.meta.env.VITE_KNOWLEDGE_SUMMARY_ENABLED === "true";
  const generationEnabled = import.meta.env.VITE_KNOWLEDGE_SUMMARY_GENERATION_ENABLED === "true";
  const { generation, unavailable, writeUnavailable, loading, creating, create } =
    useKnowledgeSummaryGeneration(readEnabled ? threadId : "", readEnabled ? turnId : "");
  if (!readEnabled || unavailable || (loading && !generation)) return null;

  const canWrite = generationEnabled && !writeUnavailable;
  const action = async (force: boolean) => {
    if (!canWrite) return;
    try {
      await create(force);
    } catch {
      // 非路由发布错配的请求错误不改变已有只读状态，后续 polling 仍可刷新状态。
    }
  };

  if (!generation) {
    return canWrite ? (
      <div className="knowledge-generation">
        <button className="chip-btn" disabled={creating} onClick={() => void action(false)}>
          ✦ 总结本轮问答
        </button>
      </div>
    ) : null;
  }
  if (generation.status === "pending") {
    return <div className="knowledge-generation pending">等待提炼知识…</div>;
  }
  if (generation.status === "processing") {
    return <div className="knowledge-generation processing">正在提炼知识…</div>;
  }
  if (generation.status === "retry_wait") {
    return <div className="knowledge-generation pending">知识总结稍后重试</div>;
  }
  if (generation.status === "dead_letter") {
    return (
      <div className="knowledge-generation failed">
        <span>知识总结失败</span>
        {canWrite && <>
          <button className="link-btn" disabled={creating} onClick={() => void action(false)}>重试</button>
          <button className="link-btn" disabled={creating} onClick={() => void action(true)}>重新整理</button>
        </>}
      </div>
    );
  }
  if (generation.status === "no_change") {
    if (generation.trigger === "auto") return null;
    if (generation.warning_codes.includes("AMBIGUOUS_DELETED_TOPIC")) {
      return <div className="knowledge-generation">为避免恢复已删除的相似主题，本轮未自动更新知识总结</div>;
    }
    return <div className="knowledge-generation">本轮没有新的可复用知识</div>;
  }
  if (generation.status === "needs_review") {
    const reviewSummaryId = generation.affected_summaries[0]?.summary_id;
    return <div className="knowledge-generation review"><span>有知识更新待确认</span><button className="link-btn" onClick={() => onOpenSummary(reviewSummaryId)}>查看总结</button></div>;
  }
  if (generation.status === "succeeded") {
    return (
      <div className="knowledge-generation success">
        <span>已更新 {generation.affected_summaries.length} 条知识总结</span>
        <button className="link-btn" onClick={() => onOpenSummary()}>查看总结</button>
        {canWrite && <button className="link-btn" disabled={creating} onClick={() => void action(true)}>重新整理</button>}
      </div>
    );
  }
  return null;
}

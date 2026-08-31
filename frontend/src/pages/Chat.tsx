/** AI 对话页：欢迎态、会话历史与 Conversation SSE 主链路。 */
import { useCallback, useEffect, useRef, useState } from "react";
import ReactMarkdown from "react-markdown";
import remarkMath from "remark-math";
import rehypeKatex from "rehype-katex";
import {
  ArrowUp,
  BookOpen,
  CalendarDays,
  Compass,
  Copy,
  GraduationCap,
  MessageSquarePlus,
  Sparkles,
  ThumbsDown,
  ThumbsUp,
} from "lucide-react";
import { useConversation } from "../hooks/useConversation";
import { useKnowledgeSummaryGeneration } from "../hooks/useKnowledgeSummaryGeneration";
import { useTurnStream } from "../hooks/useTurnStream";
import type { ConversationMessage, TurnProgressItem } from "../types/conversation";

const STARTER_PROMPTS = [
  {
    label: "告诉我你掌握的知识范围",
    prompt:
      "请根据已有的学习记录、知识图谱和相关资料，告诉我目前已经掌握了哪些知识，分别属于哪些领域，并指出还不够完整的部分。",
    icon: BookOpen,
    tone: "blue",
  },
  {
    label: "探索你喜欢的学习内容",
    prompt: "请根据我的学习记录和已掌握的知识，推荐一些适合我继续探索的学习内容，并说明推荐理由。",
    icon: Compass,
    tone: "violet",
  },
  {
    label: "为自己设置学习计划",
    prompt:
      "请根据我的学习情况和学习目标，帮我制定一个合理、循序渐进的学习计划，包括学习主题、顺序和建议安排。",
    icon: CalendarDays,
    tone: "green",
  },
  {
    label: "测试一下我的知识水平",
    prompt:
      "请根据我的学习记录和已掌握的知识，设计一组适合我的测试题，先不要直接给出答案，并根据我的回答评估知识掌握情况。",
    icon: GraduationCap,
    tone: "orange",
  },
] as const;

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
    refreshList,
  } = useConversation();
  const [input, setInput] = useState("");
  const [streamUrl, setStreamUrl] = useState<string | null>(null);
  const [activeTurnId, setActiveTurnId] = useState<string | null>(null);
  const [messages, setMessages] = useState<ConversationMessage[]>([]);
  const [pendingUser, setPendingUser] = useState<string | null>(null);
  const inputRef = useRef<HTMLTextAreaElement>(null);
  const { state: stream, reset: resetStream } = useTurnStream(streamUrl);
  const hasHistory = threads.length > 0;
  const showLiveAssistant = stream.status !== "idle";
  const visibleMessages =
    showLiveAssistant && activeTurnId
      ? messages.filter(
          (message) => message.turn_id !== activeTurnId || message.role !== "assistant",
        )
      : messages;
  const showWelcome =
    !loading &&
    messages.length === 0 &&
    !pendingUser &&
    !streamUrl &&
    !activeTurnId &&
    stream.status === "idle";

  useEffect(() => {
    if (!initialPrompt) return;
    setInput(initialPrompt);
    inputRef.current?.focus();
  }, [initialPrompt]);

  useEffect(() => {
    if (!chatTarget?.threadId) return;
    resetStream();
    setPendingUser(null);
    setStreamUrl(null);
    setActiveTurnId(null);
    void openThread(chatTarget.threadId);
  }, [chatTarget?.threadId, openThread, resetStream]);

  useEffect(() => {
    if (detail) {
      setMessages(detail.messages);
      return;
    }
    setMessages([]);
    resetStream();
    setPendingUser(null);
    setStreamUrl(null);
    setActiveTurnId(null);
  }, [detail, resetStream]);

  useEffect(() => {
    if (!chatTarget?.turnId || !detail) return;
    const target = document.querySelector(`[data-turn-id="${chatTarget.turnId}"]`);
    target?.scrollIntoView({ behavior: "smooth", block: "center" });
  }, [chatTarget?.turnId, detail]);

  useEffect(() => {
    if (!streamUrl || !["completed", "failed", "cancelled"].includes(stream.status)) return;
    // 终态时停止网络连接，但保留本地结果，确保失败提示和执行流程不会瞬间消失。
    setStreamUrl(null);
    setPendingUser(null);
    if (activeThreadId) {
      void openThread(activeThreadId);
      void refreshList();
    }
  }, [stream.status, streamUrl, activeThreadId, openThread, refreshList]);

  const handleSend = useCallback(async () => {
    const content = input.trim();
    if (!content || sending || loading) return;
    setInput("");
    resetStream();
    setStreamUrl(null);
    setActiveTurnId(null);
    setPendingUser(content);
    const response = await send(content);
    if (response) {
      setPendingUser(null);
      setActiveTurnId(response.turn_id);
      setStreamUrl(response.event_stream_path);
    } else {
      setInput(content);
      setPendingUser(null);
    }
    inputRef.current?.focus();
  }, [input, loading, resetStream, sending, send]);

  const handleFollowup = useCallback((text: string) => {
    setInput(text);
    inputRef.current?.focus();
  }, []);

  const handleOpenThread = useCallback(
    (threadId: string) => {
      if (loading || sending) return;
      if (activeThreadId && activeTurnId && ["connecting", "streaming"].includes(stream.status)) {
        void cancel(activeThreadId, activeTurnId);
      }
      resetStream();
      setPendingUser(null);
      setStreamUrl(null);
      setActiveTurnId(null);
      void openThread(threadId);
    },
    [activeThreadId, activeTurnId, cancel, loading, openThread, resetStream, sending, stream.status],
  );

  const handleNewThread = useCallback(() => {
    if (loading || sending) return;
    if (activeThreadId && activeTurnId && ["connecting", "streaming"].includes(stream.status)) {
      void cancel(activeThreadId, activeTurnId);
    }
    resetStream();
    newThread();
    setInput("");
    setMessages([]);
    setPendingUser(null);
    setStreamUrl(null);
    setActiveTurnId(null);
    requestAnimationFrame(() => inputRef.current?.focus());
  }, [activeThreadId, activeTurnId, cancel, loading, newThread, resetStream, sending, stream.status]);

  const handleCancel = useCallback(() => {
    if (activeThreadId && activeTurnId && ["connecting", "streaming"].includes(stream.status)) {
      void cancel(activeThreadId, activeTurnId);
      setPendingUser(null);
    }
  }, [activeThreadId, activeTurnId, cancel, stream.status]);

  return (
    <div className={`chat-layout ${hasHistory ? "" : "chat-layout--solo"}`}>
      {hasHistory ? (
        <aside className="conv-list rise" aria-label="历史对话">
          <button
            className="chat-new-button"
            onClick={handleNewThread}
            disabled={loading || sending}
            type="button"
          >
            <MessageSquarePlus size={17} strokeWidth={1.8} />
            <span>新对话</span>
          </button>
          <div className="conv-history">
            {threads.map((thread) => (
              <button
                key={thread.thread_id}
                className={`conv-item ${thread.thread_id === activeThreadId ? "active" : ""}`}
                onClick={() => handleOpenThread(thread.thread_id)}
                disabled={loading || sending}
                type="button"
              >
                <span className="conv-title">{thread.title || "新对话"}</span>
                <span className="conv-time">{new Date(thread.updated_at).toLocaleDateString()}</span>
              </button>
            ))}
          </div>
          {activeThreadId && (
            <button
              className="chip-btn conv-remove"
              onClick={() => void remove(activeThreadId)}
              disabled={loading || sending || ["connecting", "streaming"].includes(stream.status)}
              type="button"
            >
              删除当前会话
            </button>
          )}
        </aside>
      ) : (
        <div className="chat-toolbar rise">
          <button
            className="chat-new-button"
            onClick={handleNewThread}
            disabled={loading || sending}
            type="button"
          >
            <MessageSquarePlus size={17} strokeWidth={1.8} />
            <span>新对话</span>
          </button>
        </div>
      )}

      <main className="chat-main rise" style={{ animationDelay: "0.08s" }}>
        <div className={`chat-scroll ${showWelcome ? "chat-scroll--welcome" : ""}`}>
          {loading && <div className="loading-hint">加载会话…</div>}
          {error && <div className="error-hint">{error}</div>}
          {showWelcome ? (
            <WelcomePanel onSelectPrompt={handleFollowup} />
          ) : (
            <>
              {visibleMessages.map((message) => (
                <MessageRow
                  key={message.message_id}
                  message={message}
                  threadId={message.thread_id}
                  onOpenSummary={onOpenSummary}
                />
              ))}
              {pendingUser && (
                <div className="msg user">
                  <div className="msg-body">
                    <div className="msg-text">{pendingUser}</div>
                  </div>
                </div>
              )}
              {showLiveAssistant && (
                <div className="msg assistant" aria-live="polite">
                  <div className="msg-body">
                    <div className="msg-role">学神 AI · 讲解模式</div>
                    {stream.progress.length > 0 && (
                      <ThinkingTimeline items={stream.progress} status={stream.status} />
                    )}
                    {stream.status === "connecting" && (
                      <div className="loading-hint">正在连接…</div>
                    )}
                    {stream.answer && (
                      <div className="msg-text md">
                        <ReactMarkdown remarkPlugins={[remarkMath]} rehypePlugins={[rehypeKatex]}>
                          {stream.answer}
                        </ReactMarkdown>
                      </div>
                    )}
                    {stream.citations.length > 0 && (
                      <div className="source-box">
                        <div className="source-head">注释 · NOTES & SOURCES</div>
                        {stream.citations.map((source, index) => (
                          <div key={source.citation_id} className="source-item">
                            <span className="source-num">[{index + 1}]</span>
                            <div>
                              <div className="source-cite">
                                {source.book_name} · {source.chapter_path.join("/")} {" "}
                                <span className="page">P.{source.page_start ?? "—"}</span>
                              </div>
                              <div className="source-snippet">「{source.snippet}」</div>
                            </div>
                          </div>
                        ))}
                      </div>
                    )}
                    {stream.status === "streaming" && (
                      <button className="chip-btn" onClick={handleCancel} type="button">
                        停止生成
                      </button>
                    )}
                    {stream.status === "failed" && (
                      <div className="error-hint">{stream.error?.message ?? "回答失败，请重试"}</div>
                    )}
                    {stream.status === "cancelled" && <div className="loading-hint">已取消</div>}
                    {stream.memorySubmission === "accepted" && (
                      <div className="memory-hint">记忆请求已接收</div>
                    )}
                    {stream.memorySubmission === "retrying" && (
                      <div className="memory-hint">记忆请求尚未确认，系统将重试</div>
                    )}
                    {stream.status === "completed" && activeThreadId && activeTurnId && (
                      <KnowledgeSummaryGenerationActions
                        threadId={activeThreadId}
                        turnId={activeTurnId}
                        onOpenSummary={onOpenSummary}
                      />
                    )}
                    <div className="msg-actions">
                      <button
                        className="chip-btn"
                        onClick={() => void navigator.clipboard?.writeText(stream.answer)}
                        disabled={!stream.answer}
                        type="button"
                      >
                        <Copy size={13} /> 复制
                      </button>
                      <button className="chip-btn" type="button" aria-label="回答有帮助">
                        <ThumbsUp size={13} />
                      </button>
                      <button className="chip-btn" type="button" aria-label="回答没有帮助">
                        <ThumbsDown size={13} />
                      </button>
                    </div>
                  </div>
                </div>
              )}
            </>
          )}
        </div>

        {stream.status === "completed" && stream.followups.length > 0 && (
          <div className="followups">
            {stream.followups.map((followup) => (
              <button
                key={followup}
                className="followup-btn"
                onClick={() => handleFollowup(followup)}
                type="button"
              >
                {followup}
              </button>
            ))}
          </div>
        )}

        <div className="chat-composer-wrap">
          <div className="chat-input-row">
            <textarea
              ref={inputRef}
              className="chat-input"
              rows={2}
              value={input}
              onChange={(event) => setInput(event.target.value)}
              onKeyDown={(event) => {
                if (event.key === "Enter" && !event.shiftKey) {
                  event.preventDefault();
                  void handleSend();
                }
              }}
              placeholder="随心输入你想学习的问题"
              aria-label="对话输入"
            />
            <button
              className="chat-send-button"
              onClick={() => void handleSend()}
              disabled={loading || sending || !input.trim()}
              type="button"
              aria-label="发送"
              title="发送"
            >
              <ArrowUp size={20} strokeWidth={2.1} />
            </button>
          </div>
        </div>
      </main>
    </div>
  );
}

function WelcomePanel({ onSelectPrompt }: { onSelectPrompt: (prompt: string) => void }) {
  return (
    <section className="chat-welcome" aria-labelledby="chat-welcome-title">
      <div className="chat-welcome-mark" aria-hidden="true">
        <Sparkles size={24} strokeWidth={1.6} />
      </div>
      <h1 id="chat-welcome-title">要在 xueshen 里学习什么？</h1>
      <div className="starter-grid" aria-label="推荐的学习方式">
        {STARTER_PROMPTS.map(({ label, prompt, icon: Icon, tone }, index) => (
          <button
            key={label}
            className={`starter-card starter-card--${tone}`}
            style={{ animationDelay: `${0.08 + index * 0.06}s` }}
            onClick={() => onSelectPrompt(prompt)}
            type="button"
          >
            <Icon className="starter-icon" size={19} strokeWidth={1.8} />
            <span>{label}</span>
          </button>
        ))}
      </div>
    </section>
  );
}

function ThinkingTimeline({
  items,
  status,
}: {
  items: TurnProgressItem[];
  status: string;
}) {
  const active = status === "connecting" || status === "streaming";
  return (
    <section className="thinking-panel" aria-label="AI 思考流程" aria-live="polite">
      <div className="thinking-panel-head">
        <span className="thinking-panel-title">AI WORKFLOW · 执行过程</span>
        {active && (
          <span className="thinking-live">
            <i />处理中
          </span>
        )}
      </div>
      <div className="thinking-steps">
        {items.map((item) => (
          <div key={item.eventId} className={`thinking-step thinking-step--${item.status}`}>
            <span className="thinking-step-mark" aria-hidden="true">
              {item.status === "started" && active ? (
                <i className="thinking-spinner" />
              ) : item.status === "started" || item.status === "degraded" ? (
                "!"
              ) : item.status === "skipped" ? (
                "–"
              ) : (
                "✓"
              )}
            </span>
            <div className="thinking-step-copy">
              <strong>{item.title}</strong>
              {item.detail && <span>{item.detail}</span>}
              <ProgressMetadata metadata={item.metadata} />
            </div>
          </div>
        ))}
      </div>
    </section>
  );
}

function ProgressMetadata({
  metadata,
}: {
  metadata: TurnProgressItem["metadata"];
}) {
  const parts: string[] = [];
  if (typeof metadata.subquery_count === "number") {
    parts.push(`${metadata.subquery_count} 个检索问题`);
  }
  if (typeof metadata.hit_count === "number") parts.push(`命中 ${metadata.hit_count} 条`);
  if (typeof metadata.raw_hit_count === "number") {
    parts.push(`候选 ${metadata.raw_hit_count} 条`);
  }
  if (typeof metadata.evidence_count === "number") {
    parts.push(`保留 ${metadata.evidence_count} 条证据`);
  }
  if (typeof metadata.evidence_tokens === "number") {
    parts.push(`${metadata.evidence_tokens} tokens`);
  }
  if (typeof metadata.history_messages === "number") {
    parts.push(`历史 ${metadata.history_messages} 条`);
  }
  if (metadata.assessment === "needs_more") parts.push("需要补检索");
  if (metadata.assessment === "insufficient") parts.push("资料有限");
  if (metadata.assessment === "sufficient") parts.push("检查通过");
  return parts.length > 0 ? <em>{parts.join(" · ")}</em> : null;
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

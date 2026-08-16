// AI 对话页：真实 Conversation API + Fetch SSE 流式接入（方案 §18 / §17）。
// 目录式会话列表 + 学报式回答流 + 脚注式引用（后端 Citation DTO）+ 追问建议。
import { useCallback, useEffect, useRef, useState } from "react";
import ReactMarkdown from "react-markdown";
import remarkMath from "remark-math";
import rehypeKatex from "rehype-katex";
import { Bookmark, Copy, Plus, SendHorizontal, ThumbsDown, ThumbsUp } from "lucide-react";
import { useConversation } from "../hooks/useConversation";
import { useTurnStream } from "../hooks/useTurnStream";
import { addNote } from "../notebookStore";
import type { ConversationMessage } from "../types/conversation";

function excerptFromAnswer(answer: string): string {
  return answer
    .replace(/```/g, "")
    .replace(/^#{1,6}\s+/gm, "")
    .replace(/\*\*|__|~~|\$\$/g, "")
    .replace(/\s+/g, " ")
    .trim()
    .slice(0, 280);
}

function questionBefore(messages: ConversationMessage[], index: number): string {
  for (let i = index - 1; i >= 0; i -= 1) {
    if (messages[i].role === "user") return messages[i].content;
  }
  return messages.find((message) => message.role === "user")?.content ?? "未命名问题";
}

export function ChatPage({ initialPrompt = "" }: { initialPrompt?: string }) {
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
  const [saved, setSaved] = useState(false);
  const [streamUrl, setStreamUrl] = useState<string | null>(null);
  const [activeTurnId, setActiveTurnId] = useState<string | null>(null);
  const [messages, setMessages] = useState<ConversationMessage[]>([]);
  const [pendingUser, setPendingUser] = useState<string | null>(null);
  const inputRef = useRef<HTMLTextAreaElement>(null);

  const { state: stream } = useTurnStream(streamUrl);

  const activeThreadTitle =
    threads.find((thread) => thread.thread_id === activeThreadId)?.title?.trim() || "AI 对话";

  const saveAnswer = useCallback(
    (question: string, answer: string) => {
      const created = addNote({
        question,
        answerExcerpt: excerptFromAnswer(answer),
        source: activeThreadTitle,
      });
      setSaved(created !== null);
      return created !== null;
    },
    [activeThreadTitle],
  );

  const currentQuestion = pendingUser ?? questionBefore(messages, messages.length);

  // 从新用户主页选择的预设问题只带入输入框，不自动创建会话或发送。
  useEffect(() => {
    if (!initialPrompt) return;
    setInput(initialPrompt);
    inputRef.current?.focus();
  }, [initialPrompt]);

  // 会话切换时载入历史消息
  useEffect(() => {
    if (detail) setMessages(detail.messages);
    else setMessages([]);
    setPendingUser(null);
    setStreamUrl(null);
    setActiveTurnId(null);
  }, [detail]);

  // 流结束（completed/failed/cancelled）后：关闭流式气泡并刷新会话详情
  // （§17.5 #7：completed 才完整）。评审 P2：streamUrl 置空使流式气泡消失，
  // 避免与刷新后的历史消息重复渲染；pendingUser 同步清除。
  useEffect(() => {
    if (
      stream.status === "completed" ||
      stream.status === "failed" ||
      stream.status === "cancelled"
    ) {
      setStreamUrl(null);
      setActiveTurnId(null);
      setPendingUser(null);
      if (activeThreadId) {
        void openThread(activeThreadId);
      }
    }
  }, [stream.status, activeThreadId, openThread]);

  const handleSend = useCallback(async () => {
    const content = input.trim();
    if (!content || sending) return;
    setInput("");
    setPendingUser(content);
    setSaved(false);
    const response = await send(content);
    if (response) {
      setActiveTurnId(response.turn_id);
      setStreamUrl(response.event_stream_path);
    }
    inputRef.current?.focus();
  }, [input, sending, send]);

  const handleFollowup = useCallback(
    (text: string) => {
      setInput(text);
      void handleSend();
    },
    [handleSend],
  );

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
        {threads.map((c) => (
          <div
            key={c.thread_id}
            className={`conv-item ${c.thread_id === activeThreadId ? "active" : ""}`}
            onClick={() => void openThread(c.thread_id)}
          >
            <div className="conv-line">
              <span className="conv-title">{c.title || "新对话"}</span>
              <span className="conv-leader" />
              <span className="conv-time">{new Date(c.updated_at).toLocaleDateString()}</span>
            </div>
          </div>
        ))}
        {activeThreadId && (
          <button className="chip-btn conv-remove" onClick={() => void remove(activeThreadId)}>
            删除当前会话
          </button>
        )}
      </div>

      <div className="chat-main rise" style={{ animationDelay: "0.08s" }}>
        <div className="chat-scroll">
          {loading && <div className="loading-hint">加载会话…</div>}
          {error && <div className="error-hint">{error}</div>}

          {messages.map((m, index) => (
            <MessageRow
              key={m.message_id}
              message={m}
              question={questionBefore(messages, index)}
              onSave={saveAnswer}
            />
          ))}

          {pendingUser && (
            <div className="msg user">
              <div className="msg-body">
                <div className="msg-text">{pendingUser}</div>
              </div>
            </div>
          )}

          {stream.status !== "idle" && (
            <div className="msg assistant">
              <div className="msg-body">
                <div className="msg-role">学神 AI · 讲解模式</div>
                {stream.status === "connecting" && <div className="loading-hint">正在连接…</div>}
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
                    {stream.citations.map((s, i) => (
                      <div key={s.citation_id} className="source-item">
                        <span className="source-num">[{i + 1}]</span>
                        <div>
                          <div className="source-cite">
                            {s.book_name} · {s.chapter_path.join("/")}{" "}
                            <span className="page">P.{s.page_start ?? "—"}</span>
                          </div>
                          <div className="source-snippet">「{s.snippet}」</div>
                        </div>
                      </div>
                    ))}
                  </div>
                )}
                {stream.status === "streaming" && (
                  <button className="chip-btn" onClick={handleCancel}>
                    停止生成
                  </button>
                )}
                {stream.status === "failed" && (
                  <div className="error-hint">
                    {stream.error?.message ?? "回答失败，请重试"}
                  </div>
                )}
                {stream.status === "cancelled" && <div className="loading-hint">已取消</div>}
                {stream.memorySubmission === "accepted" && (
                  <div className="memory-hint">记忆请求已接收</div>
                )}
                {stream.memorySubmission === "retrying" && (
                  <div className="memory-hint">记忆请求尚未确认，系统将重试</div>
                )}
                <div className="msg-actions">
                  <button
                    className="chip-btn"
                    onClick={() => saveAnswer(currentQuestion, stream.answer)}
                  >
                    <Bookmark size={13} fill={saved ? "currentColor" : "none"} />
                    {saved ? "已存入错题本" : "存入错题本"}
                  </button>
                  <button
                    className="chip-btn"
                    onClick={() => void navigator.clipboard?.writeText(stream.answer)}
                  >
                    <Copy size={13} /> 复制
                  </button>
                  <button className="chip-btn"><ThumbsUp size={13} /></button>
                  <button className="chip-btn"><ThumbsDown size={13} /></button>
                </div>
              </div>
            </div>
          )}
        </div>

        {stream.status === "completed" && stream.followups.length > 0 && (
          <div className="followups">
            {stream.followups.map((f) => (
              <button key={f} className="followup-btn" onClick={() => handleFollowup(f)}>
                {f}
              </button>
            ))}
          </div>
        )}

        <div className="chat-input-row">
          <textarea
            ref={inputRef}
            className="chat-input"
            rows={2}
            value={input}
            onChange={(e) => setInput(e.target.value)}
            onKeyDown={(e) => {
              if (e.key === "Enter" && !e.shiftKey) {
                e.preventDefault();
                void handleSend();
              }
            }}
            placeholder="继续追问，或开始一个新问题… 支持 LaTeX，如 $\int_0^1 x^2 dx$"
          />
          <button className="btn btn-red" style={{ alignSelf: "flex-end" }} onClick={() => void handleSend()} disabled={sending}>
            <SendHorizontal size={15} /> 发送
          </button>
        </div>
      </div>
    </div>
  );
}

function MessageRow({
  message,
  question,
  onSave,
}: {
  message: ConversationMessage;
  question: string;
  onSave: (question: string, answer: string) => boolean;
}) {
  const [saved, setSaved] = useState(false);

  return (
    <div className={`msg ${message.role}`}>
      <div className="msg-body">
        <div className="msg-role" style={message.role === "user" ? { textAlign: "right" } : undefined}>
          {message.role === "user" ? "你 · 提问" : "学神 AI · 讲解模式"}
        </div>
        {message.role === "user" ? (
          <div className="msg-text">{message.content}</div>
        ) : (
          <div className="msg-text md">
            <ReactMarkdown remarkPlugins={[remarkMath]} rehypePlugins={[rehypeKatex]}>
              {message.content}
            </ReactMarkdown>
          </div>
        )}
        {message.role === "assistant" && (
          <div className="msg-actions">
            <button
              className="chip-btn"
              onClick={() => {
                if (onSave(question, message.content)) setSaved(true);
              }}
            >
              <Bookmark size={13} fill={saved ? "currentColor" : "none"} />
              {saved ? "已存入错题本" : "存入错题本"}
            </button>
          </div>
        )}
      </div>
    </div>
  );
}

// AI 对话页：目录式会话列表（点线引导）+ 学报式回答流（首字下沉）+ 脚注式引用 + 追问建议。
import { useState } from "react";
import ReactMarkdown from "react-markdown";
import remarkMath from "remark-math";
import rehypeKatex from "rehype-katex";
import { Bookmark, Copy, Plus, SendHorizontal, ThumbsDown, ThumbsUp } from "lucide-react";
import { chatThread, conversations, followups } from "../data";

export function ChatPage() {
  const [saved, setSaved] = useState(false);

  return (
    <div className="chat-layout" style={{ maxWidth: 1160, margin: "0 auto" }}>
      <div className="conv-list rise">
        <button className="btn btn-primary conv-new">
          <Plus size={15} /> 新对话
        </button>
        {conversations.map((c) => (
          <div key={c.id} className={`conv-item ${c.active ? "active" : ""}`}>
            <div className="conv-line">
              <span className="conv-title">{c.title}</span>
              <span className="conv-leader" />
              <span className="conv-time">{c.time}</span>
            </div>
            <div className="conv-preview">{c.preview}</div>
          </div>
        ))}
      </div>

      <div className="chat-main rise" style={{ animationDelay: "0.08s" }}>
        <div className="chat-scroll">
          {chatThread.map((m) => (
            <div key={m.id} className={`msg ${m.role}`}>
              <div className="msg-body">
                <div className="msg-role" style={m.role === "user" ? { textAlign: "right" } : undefined}>
                  {m.role === "user" ? "你 · 提问" : "格物 AI · 讲解模式"}
                </div>
                {m.role === "user" ? (
                  <div className="msg-text">{m.text}</div>
                ) : (
                  <>
                    <div className="msg-text md">
                      <ReactMarkdown remarkPlugins={[remarkMath]} rehypePlugins={[rehypeKatex]}>
                        {m.markdown ?? ""}
                      </ReactMarkdown>
                    </div>

                    {m.sources && (
                      <div className="source-box">
                        <div className="source-head">注释 · NOTES & SOURCES</div>
                        {m.sources.map((s, i) => (
                          <div key={s.book} className="source-item">
                            <span className="source-num">[{i + 1}]</span>
                            <div>
                              <div className="source-cite">
                                {s.book} · {s.chapter} <span className="page">P.{s.page}</span>
                              </div>
                              <div className="source-snippet">「{s.snippet}」</div>
                            </div>
                          </div>
                        ))}
                      </div>
                    )}

                    <div className="msg-actions">
                      <button className="chip-btn" onClick={() => setSaved(true)}>
                        <Bookmark size={13} fill={saved ? "currentColor" : "none"} />
                        {saved ? "已存入错题本" : "存入错题本"}
                      </button>
                      <button className="chip-btn"><Copy size={13} /> 复制</button>
                      <button className="chip-btn"><ThumbsUp size={13} /></button>
                      <button className="chip-btn"><ThumbsDown size={13} /></button>
                    </div>
                  </>
                )}
              </div>
            </div>
          ))}
        </div>

        <div className="followups">
          {followups.map((f) => (
            <button key={f} className="followup-btn">{f}</button>
          ))}
        </div>

        <div className="chat-input-row">
          <textarea className="chat-input" rows={2} placeholder="继续追问，或开始一个新问题… 支持 LaTeX，如 $\int_0^1 x^2 dx$" />
          <button className="btn btn-red" style={{ alignSelf: "flex-end" }}>
            <SendHorizontal size={15} /> 发送
          </button>
        </div>
      </div>
    </div>
  );
}

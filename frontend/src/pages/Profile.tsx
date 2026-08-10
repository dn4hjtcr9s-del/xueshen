// 个人中心页：账号信息 + 学习统计 + 「AI 记住了我什么」记忆管理面板。
import { Pencil, ShieldCheck, Trash2 } from "lucide-react";
import { memories, user } from "../data";
import { SectionHead } from "../ui";

const CAT_CLASS: Record<string, string> = {
  目标: "red",
  掌握度: "gold",
  学习偏好: "green",
};

export function ProfilePage() {
  return (
    <div className="profile-grid">
      <div className="card profile-card rise">
        <div className="profile-avatar">{user.initials}</div>
        <div className="profile-name">{user.name}</div>
        <div className="profile-sub">加入 {user.joinedDays} 天 · 通用数学学习者</div>
        <div className="profile-stats">
          <div className="profile-stat">
            <div className="n">{user.streakDays}</div>
            <div className="l">连续学习（天）</div>
          </div>
          <div className="profile-stat">
            <div className="n">186</div>
            <div className="l">累计提问</div>
          </div>
          <div className="profile-stat">
            <div className="n">11</div>
            <div className="l">已掌握知识点</div>
          </div>
          <div className="profile-stat">
            <div className="n">5</div>
            <div className="l">错题收藏</div>
          </div>
        </div>
        <button className="btn btn-ghost" style={{ marginTop: 20, width: "100%", justifyContent: "center" }}>
          <Pencil size={13} /> 编辑资料
        </button>
      </div>

      <div>
        <SectionHead num="01" title="AI 记住了我什么" note="透明 · 可纠正 · 可删除" />
        <div style={{ height: 14 }} />
        <div className="memory-banner rise" style={{ animationDelay: "0.06s" }}>
          <ShieldCheck size={16} style={{ flexShrink: 0, marginTop: 1 }} />
          <span>
            格物 AI 只根据你的学习行为积累以下记忆，用来让讲解更贴合你。每一条都可以修改或删除，删除后立即生效。
          </span>
        </div>
        <div className="rise" style={{ animationDelay: "0.12s" }}>
          {memories.map((m, i) => (
            <div key={i} className="memory-item">
              <span className={`tag ${CAT_CLASS[m.category] ?? ""}`}>{m.category}</span>
              <span className="memory-text">{m.text}</span>
              <span className="memory-time">{m.updatedAt}</span>
              <span className="memory-actions">
                <button aria-label="纠正"><Pencil size={13} /></button>
                <button aria-label="删除"><Trash2 size={13} /></button>
              </span>
            </div>
          ))}
        </div>
      </div>
    </div>
  );
}

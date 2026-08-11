// 个人中心页：账号信息 + 学习统计 + 「AI 记住了我什么」记忆管理面板。
// 只有「AI 记住了我什么」接真实 API（§20.1）；其余区域为 Mock：
// development 显著标注“展示数据”，production 构建默认隐藏。
import { Pencil, ShieldCheck } from "lucide-react";
import { user } from "../data";
import { SectionHead } from "../ui";
import { MemorySection } from "./profile/MemorySection";

const SHOW_MOCK = import.meta.env.DEV;

export function ProfilePage() {
  return (
    <div className="profile-grid">
      {SHOW_MOCK && (
        <div className="card profile-card rise">
          <div className="mock-badge" data-testid="mock-badge">
            展示数据 · 非真实统计
          </div>
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
          <button
            className="btn btn-ghost"
            style={{ marginTop: 20, width: "100%", justifyContent: "center" }}
          >
            <Pencil size={13} /> 编辑资料
          </button>
        </div>
      )}

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
          <MemorySection />
        </div>
      </div>
    </div>
  );
}

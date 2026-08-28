// 个人中心页：账号信息 + 学习统计 + 「AI 记住了我什么」记忆管理面板。
// 只有「AI 记住了我什么」接真实 API（§20.1）；其余区域为 Mock：
// development 显著标注“展示数据”，production 构建默认隐藏。
// 用户名/邮箱/首字来自 /me 真实用户（方案 §9.2 / 附录 A.4 #15）；logout 按钮在此。
// 未登录（访客）时展示登录/注册表单——登录不是浏览前置条件，仅作为可选项。
import { useState } from "react";
import { LogOut, Pencil, ShieldCheck } from "lucide-react";
import { useEffect } from "react";
import { getKnowledgeSummaryStats } from "../api/knowledgeSummaries";
import { user } from "../data";
import { useAuth } from "../auth/AuthContext";
import { SectionHead } from "../ui";
import { MemorySection } from "./profile/MemorySection";
import { LoginPage } from "./Login";
import { RegisterPage } from "./Register";

const SHOW_MOCK = import.meta.env.DEV;

export function ProfilePage() {
  const { user: authUser, initials, logout } = useAuth();
  const [authMode, setAuthMode] = useState<"login" | "register">("login");
  const [summaryCount, setSummaryCount] = useState<number | null>(null);

  useEffect(() => {
    if (!authUser || import.meta.env.VITE_KNOWLEDGE_SUMMARY_ENABLED !== "true") return;
    void getKnowledgeSummaryStats().then((stats) => setSummaryCount(stats.active_count)).catch(() => setSummaryCount(null));
  }, [authUser]);

  // 访客：个人中心展示登录/注册表单（可切换），登录成功后自动回到已登录视图
  if (authUser === null) {
    return authMode === "login" ? (
      <LoginPage onGoRegister={() => setAuthMode("register")} />
    ) : (
      <RegisterPage onGoLogin={() => setAuthMode("login")} />
    );
  }

  return (
    <div className="profile-grid">
      <div className="card profile-card rise">
        {/* 评审 P2-9：真实用户名/邮箱/首字在生产环境也必须展示（§9.2 / A.4 #15 #16）；
            SHOW_MOCK 只控制统计数字、编辑资料等 mock 内容 */}
        <div className="profile-avatar">{initials}</div>
        <div className="profile-name">{authUser?.username ?? ""}</div>
        <div className="profile-sub">{authUser?.email ?? "未填写"}</div>
        {SHOW_MOCK && (
          <>
            <div className="mock-badge" data-testid="mock-badge">
              展示数据 · 非真实统计
            </div>
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
                <div className="n">{summaryCount ?? "—"}</div>
                <div className="l">知识总结</div>
              </div>
            </div>
            <button
              className="btn btn-ghost"
              style={{ marginTop: 20, width: "100%", justifyContent: "center" }}
            >
              <Pencil size={13} /> 编辑资料
            </button>
          </>
        )}
      </div>

      <div>
        <SectionHead
          num="01"
          title="AI 记住了我什么"
          note="透明 · 可纠正 · 可删除"
          action={
            <button
              className="link-btn"
              type="button"
              onClick={() => void logout()}
              aria-label="退出登录"
            >
              <LogOut size={13} /> 退出登录
            </button>
          }
        />
        <div style={{ height: 14 }} />
        <div className="memory-banner rise" style={{ animationDelay: "0.06s" }}>
          <ShieldCheck size={16} style={{ flexShrink: 0, marginTop: 1 }} />
          <span>
            学神 AI 只根据你的学习行为积累以下记忆，用来让讲解更贴合你。每一条都可以修改或删除，删除后立即生效。
          </span>
        </div>
        <div className="rise" style={{ animationDelay: "0.12s" }}>
          <MemorySection />
        </div>
      </div>
    </div>
  );
}

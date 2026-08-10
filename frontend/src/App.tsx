// 生产前端样例骨架：左侧图标窄轨 + 顶部工具条 + 页面切换 + 通知面板。
import { useState } from "react";
import {
  Bell,
  BookMarked,
  CalendarCheck,
  Home,
  MessageCircle,
  Network,
  Search,
  Users,
} from "lucide-react";
import type { PageKey } from "./data";
import { notifications, user } from "./data";
import { Masthead } from "./ui";
import { HomePage } from "./pages/Home";
import { ChatPage } from "./pages/Chat";
import { PlanPage } from "./pages/Plan";
import { KnowledgeMapPage } from "./pages/KnowledgeMap";
import { NotebookPage } from "./pages/Notebook";
import { CommunityPage } from "./pages/Community";
import { ProfilePage } from "./pages/Profile";

const NAV: { key: PageKey; label: string; icon: typeof Home; badge?: number }[] = [
  { key: "home", label: "今日", icon: Home },
  { key: "chat", label: "AI 对话", icon: MessageCircle },
  { key: "plan", label: "学习计划", icon: CalendarCheck },
  { key: "map", label: "知识地图", icon: Network },
  { key: "notebook", label: "错题本", icon: BookMarked, badge: 3 },
  { key: "community", label: "社区", icon: Users },
];

const MASTHEADS: Record<PageKey, { kicker: string; title: string; aside: string[] }> = {
  home: { kicker: "Tuesday · 星期二", title: "今日", aside: ["VOL.04 — 特征值与对角化", "第 12 天连续学习"] },
  chat: { kicker: "Ask · 有问必答", title: "AI 对话", aside: ["讲解模式", "引用可溯源"] },
  plan: { kicker: "Plan · 循序渐进", title: "学习计划", aside: ["WEEK 4 / 6", "每周日晚自动调整"] },
  map: { kicker: "Atlas · 了如指掌", title: "知识地图", aside: ["11 个知识点", "3 个领域"] },
  notebook: { kicker: "Notebook · 温故知新", title: "错题本", aside: ["5 篇收藏", "3 条今天到期"] },
  community: { kicker: "Community · 教学相长", title: "社区", aside: ["讨论区 · 小组 · 打卡"] },
  profile: { kicker: "Profile · 君子慎独", title: "个人中心", aside: ["记忆透明可管"] },
};

function NotifPanel({ onClose }: { onClose: () => void }) {
  return (
    <>
      <div style={{ position: "fixed", inset: 0, zIndex: 40 }} onClick={onClose} />
      <div className="card notif-panel">
        <div className="notif-head">
          <span className="t">通知</span>
          <div style={{ flex: 1 }} />
          <button className="link-btn" onClick={onClose}>全部已读</button>
        </div>
        {notifications.map((n) => (
          <div key={n.id} className={`notif-item ${n.read ? "" : "unread"}`}>
            <div className={`notif-ico ${n.kind}`}>
              <Bell size={14} />
            </div>
            <div>
              <div className="notif-text">{n.text}</div>
              <div className="notif-time">{n.time}</div>
            </div>
          </div>
        ))}
      </div>
    </>
  );
}

export default function App() {
  const [page, setPage] = useState<PageKey>("home");
  const [showNotif, setShowNotif] = useState(false);
  const unread = notifications.some((n) => !n.read);

  // 页面间联动：知识地图/计划里的"去问 AI"跳转到对话页。
  const goChat = () => setPage("chat");
  const masthead = MASTHEADS[page];

  return (
    <div className="app">
      <aside className="sidebar">
        <div className="brand-seal" title="格物 · Math Studio">格</div>
        <nav className="nav">
          {NAV.map(({ key, label, icon: Icon, badge }) => (
            <button
              key={key}
              title={label}
              className={`nav-item ${page === key ? "active" : ""}`}
              onClick={() => setPage(key)}
            >
              <Icon size={18} strokeWidth={1.7} />
              {badge && <span className="nav-badge">{badge}</span>}
            </button>
          ))}
        </nav>
        <div className="sidebar-foot">格物致知</div>
      </aside>

      <div className="main">
        <header className="utility-bar">
          <div className="utility-date">2026.08.04 — GEWU MATH STUDIO</div>
          <div className="topbar-search">
            <Search size={13} />
            <span>搜索知识点、对话、笔记…</span>
          </div>
          <button className="icon-btn" onClick={() => setShowNotif((v) => !v)} aria-label="通知">
            <Bell size={15} strokeWidth={1.8} />
            {unread && <span className="dot" />}
          </button>
          <button className="avatar-btn" onClick={() => setPage("profile")} aria-label="个人中心">
            {user.initials}
          </button>
        </header>

        <main className="content">
          <div className="page">
            {page !== "home" && (
              <Masthead
                kicker={masthead.kicker}
                title={masthead.title}
                aside={masthead.aside.map((l) => (
                  <div key={l}>{l}</div>
                ))}
              />
            )}
            {page === "home" && <HomePage goChat={goChat} go={(p) => setPage(p)} />}
            {page === "chat" && <ChatPage />}
            {page === "plan" && <PlanPage goChat={goChat} />}
            {page === "map" && <KnowledgeMapPage goChat={goChat} />}
            {page === "notebook" && <NotebookPage />}
            {page === "community" && <CommunityPage />}
            {page === "profile" && <ProfilePage />}
          </div>
        </main>
      </div>

      {showNotif && <NotifPanel onClose={() => setShowNotif(false)} />}
    </div>
  );
}

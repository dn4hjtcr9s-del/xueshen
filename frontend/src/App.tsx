// 生产前端样例骨架：左侧图标窄轨 + 顶部工具条 + 页面切换 + 通知面板。
// 通知面板（方案 §6.5/§6.6）：Memory + Community 双域合并展示。
import { useCallback, useEffect, useRef, useState } from "react";
import {
  Bell,
  BookOpenText,
  CalendarCheck,
  Home,
  MessageCircle,
  Network,
  Search,
  Users,
} from "lucide-react";
import type { PageKey } from "./data";
import { useAuth } from "./auth/AuthContext";
import {
  listNotifications,
  markAllMemoryNotificationsRead,
  type MemoryNotification,
} from "./api/memory";
import {
  listCommunityNotifications,
  markAllCommunityNotificationsRead,
  type CommunityNotification,
} from "./api/community";
import { Masthead } from "./ui";
import { HomePage } from "./pages/Home";
import { ChatPage } from "./pages/Chat";
import { PlanPage } from "./pages/Plan";
import { KnowledgeMapPage } from "./pages/KnowledgeMap";
import { KnowledgeSummariesPage } from "./pages/KnowledgeSummaries";
import { CommunityPage } from "./pages/community";
import { ProfilePage } from "./pages/Profile";

const KNOWLEDGE_SUMMARY_ENABLED = import.meta.env.VITE_KNOWLEDGE_SUMMARY_ENABLED === "true";

const NAV: { key: PageKey; label: string; icon: typeof Home }[] = [
  { key: "home", label: "今日", icon: Home },
  { key: "chat", label: "AI 对话", icon: MessageCircle },
  { key: "plan", label: "学习计划", icon: CalendarCheck },
  { key: "map", label: "知识地图", icon: Network },
  { key: "summaries", label: "知识总结", icon: BookOpenText },
  { key: "community", label: "社区", icon: Users },
];

function formatStudioDate(now: Date): string {
  const year = now.getFullYear();
  const month = String(now.getMonth() + 1).padStart(2, "0");
  const day = String(now.getDate()).padStart(2, "0");
  return `${year}.${month}.${day} — XUESHEN MATH STUDIO`;
}

const MASTHEADS: Record<PageKey, { kicker: string; title: string; aside: string[] }> = {
  home: { kicker: "Tuesday · 星期二", title: "今日", aside: ["VOL.04 — 特征值与对角化", "第 12 天连续学习"] },
  chat: { kicker: "Ask · 有问必答", title: "AI 对话", aside: ["讲解模式", "引用可溯源"] },
  plan: { kicker: "Plan · 循序渐进", title: "学习计划", aside: ["等待你的第一个目标", "由 AI 生成并动态调整"] },
  map: { kicker: "Atlas · 了如指掌", title: "知识地图", aside: ["11 个知识点", "3 个领域"] },
  summaries: { kicker: "Knowledge Summary · 对话沉淀", title: "知识总结", aside: ["定义 · 定理 · 公式", "来源可追溯"] },
  community: { kicker: "Community · 教学相长", title: "社区", aside: ["讨论区 · 小组 · 打卡"] },
  profile: { kicker: "Profile · 君子慎独", title: "个人中心", aside: ["记忆透明可管"] },
};

// §6.6 冻结的统一展示模型
type UnifiedNotification = {
  source: "memory" | "community";
  notification_id: string;
  event_type: string;
  title: string;
  body: string;
  read_at: string | null;
  created_at: string;
  post_id?: string;
  reply_id?: string;
};

function toUnified(
  n: MemoryNotification,
  source: "memory",
): UnifiedNotification;
function toUnified(
  n: CommunityNotification,
  source: "community",
): UnifiedNotification;
function toUnified(
  n: MemoryNotification | CommunityNotification,
  source: "memory" | "community",
): UnifiedNotification {
  const community = source === "community" ? (n as CommunityNotification) : undefined;
  return {
    source,
    notification_id: n.notification_id,
    event_type: n.event_type,
    title: n.title,
    body: n.body,
    read_at: n.read_at,
    created_at: n.created_at,
    post_id: community?.post_id ?? undefined,
    reply_id: community?.reply_id ?? undefined,
  };
}

// 通知面板（§6.5）：双域并行加载、局部失败仍展示另一域、按 created_at DESC 稳定排序。
function NotifPanel({
  items,
  error,
  onClose,
  onReadAll,
  onOpenPost,
}: {
  items: UnifiedNotification[];
  error: string | null;
  onClose: () => void;
  onReadAll: () => void;
  onOpenPost: (postId: string) => void;
}) {
  return (
    <>
      <div style={{ position: "fixed", inset: 0, zIndex: 40 }} onClick={onClose} />
      <div className="card notif-panel">
        <div className="notif-head">
          <span className="t">通知</span>
          <div style={{ flex: 1 }} />
          <button className="link-btn" onClick={onReadAll}>
            全部已读
          </button>
        </div>
        {error && <div className="notif-time">{error}</div>}
        {!error && items.length === 0 && <div className="notif-time">暂无通知</div>}
        {items.map((n) => (
          <div
            key={`${n.source}:${n.notification_id}`}
            className={`notif-item ${n.read_at ? "" : "unread"}`}
            style={n.source === "community" && n.post_id ? { cursor: "pointer" } : undefined}
            onClick={() => {
              if (n.source === "community" && n.post_id) onOpenPost(n.post_id);
            }}
          >
            <div className="notif-ico">
              <Bell size={14} />
            </div>
            <div>
              <div className="notif-text">
                {n.title}
                {n.body ? ` — ${n.body}` : ""}
              </div>
              <div className="notif-time">
                {n.source === "community" ? "社区 · " : ""}
                {new Date(n.created_at).toLocaleString("zh-CN", { hour12: false })}
              </div>
            </div>
          </div>
        ))}
      </div>
    </>
  );
}

export default function App() {
  const { initials, user } = useAuth();
  const [page, setPage] = useState<PageKey>("home");
  const [chatDraft, setChatDraft] = useState("");
  const [showNotif, setShowNotif] = useState(false);
  const [notifications, setNotifications] = useState<UnifiedNotification[]>([]);
  const [notifError, setNotifError] = useState<string | null>(null);
  const [unreadTotal, setUnreadTotal] = useState(0);
  // §6.5：Community 通知点击 → 切社区 Tab 并打开详情；详情打开/关闭后清空
  const [communityTargetPostId, setCommunityTargetPostId] = useState<string | null>(null);
  // §九 行为①：社区页访问后保持挂载（隐藏不卸载），登录跳转后子视图状态保留；
  // loginReturnPending 标记"因社区写操作跳登录"，登录成功后自动切回社区
  const [communityVisited, setCommunityVisited] = useState(false);
  const loginReturnPending = useRef(false);
  const [chatTarget, setChatTarget] = useState<{ threadId: string; turnId: string } | null>(null);
  const [summaryTargetId, setSummaryTargetId] = useState<string | null>(null);
  const [summaryFeatureUnavailable, setSummaryFeatureUnavailable] = useState(false);
  const summaryDiagnosticRecorded = useRef(false);
  const knowledgeSummaryAvailable = KNOWLEDGE_SUMMARY_ENABLED && !summaryFeatureUnavailable;

  const loadNotifications = useCallback(async () => {
    // §6.5：Promise.allSettled 并行读取；任一域失败仍展示另一域 + 局部错误提示
    const [memoryResult, communityResult] = await Promise.allSettled([
      listNotifications(),
      listCommunityNotifications(),
    ]);
    const memoryOk = memoryResult.status === "fulfilled";
    const communityOk = communityResult.status === "fulfilled";
    const memoryItems = memoryOk ? memoryResult.value.items : [];
    const communityItems = communityOk ? communityResult.value.items : [];
    const merged = [
      ...memoryItems.map((n) => toUnified(n, "memory")),
      ...communityItems.map((n) => toUnified(n, "community")),
    ].sort((a, b) => b.created_at.localeCompare(a.created_at));
    setNotifications(merged);
    // 未读红点使用两个 API 返回的 unread_count 之和（§6.5 #3）
    const memoryUnread = memoryOk ? memoryResult.value.unread_count : 0;
    const communityUnread = communityOk ? communityResult.value.unread_count : 0;
    setUnreadTotal(memoryUnread + communityUnread);
    if (memoryOk && communityOk) {
      setNotifError(null);
    } else {
      setNotifError(
        (memoryOk ? "" : "Memory 通知加载失败；") +
          (communityOk ? "" : "社区通知加载失败；") +
          "已展示部分结果",
      );
    }
  }, []);

  useEffect(() => {
    void loadNotifications();
  }, [loadNotifications]);

  const toggleNotif = () => {
    setShowNotif((v) => {
      if (!v) void loadNotifications();
      return !v;
    });
  };

  const readAll = async () => {
    // §6.5 #4：并发调用两个域的 read-all；部分失败后刷新两域并提示
    const [memoryResult, communityResult] = await Promise.allSettled([
      markAllMemoryNotificationsRead(),
      markAllCommunityNotificationsRead(),
    ]);
    await loadNotifications();
    if (memoryResult.status === "rejected" || communityResult.status === "rejected") {
      setNotifError("部分通知标记失败");
    }
  };

  // 页面间联动：知识地图/计划里的"去问 AI"跳转到对话页。
  const goChat = (prompt = "") => {
    setChatDraft(prompt);
    setChatTarget(null);
    setPage("chat");
  };

  const openChatTarget = (threadId: string, turnId: string) => {
    setChatDraft("");
    setChatTarget(threadId && turnId ? { threadId, turnId } : null);
    setPage("chat");
  };

  const openKnowledgeSummary = (summaryId?: string) => {
    setSummaryTargetId(summaryId ?? null);
    setPage("summaries");
  };

  const handleSummaryFeatureUnavailable = useCallback(() => {
    if (!summaryDiagnosticRecorded.current) {
      summaryDiagnosticRecorded.current = true;
      // 当前项目没有 telemetry 上报端点，按冻结方案记录一次可检索的发布错配事件。
      console.warn("knowledge_summary.feature_unavailable", { reason: "backend_404" });
    }
    setSummaryFeatureUnavailable(true);
    setPage("home");
    setChatTarget(null);
    setSummaryTargetId(null);
  }, []);
  const masthead = MASTHEADS[page];

  const openCommunityPost = (postId: string) => {
    setShowNotif(false);
    setPage("community");
    setCommunityTargetPostId(postId);
  };

  // §九：进入社区页后标记已访问（保持挂载，子视图状态跨页面切换保留）
  useEffect(() => {
    if (page === "community") setCommunityVisited(true);
  }, [page]);

  // §九 行为①：因社区写操作跳 profile 登录的，登录成功后自动切回社区原子视图
  useEffect(() => {
    if (user && loginReturnPending.current) {
      loginReturnPending.current = false;
      setPage("community");
    }
  }, [user]);

  const communityLoginRequired = useCallback(() => {
    loginReturnPending.current = true;
    setPage("profile");
  }, []);

  return (
    <div className="app">
      <aside className="sidebar">
        <div className="sidebar-brand" title="学神 · Math Studio">
          <div className="brand-seal">学</div>
          <div className="sidebar-brand-copy">
            <strong>学神数学</strong>
            <span>MATH STUDIO</span>
          </div>
        </div>
        <nav className="nav">
          {NAV.filter(({ key }) => key !== "summaries" || knowledgeSummaryAvailable).map(({ key, label, icon: Icon }) => (
            <button
              key={key}
              type="button"
              title={label}
              className={`nav-item ${page === key ? "active" : ""}`}
              onClick={() => setPage(key)}
              aria-current={page === key ? "page" : undefined}
            >
              <Icon size={18} strokeWidth={1.7} />
              <span className="nav-label">{label}</span>
            </button>
          ))}
        </nav>
        <div className="sidebar-foot">学神致知</div>
      </aside>

      <div className="main">
        <header className="utility-bar">
          <div className="utility-date">{formatStudioDate(new Date())}</div>
          <div className="topbar-search">
            <Search size={13} />
            <span>搜索知识点、对话、总结…</span>
          </div>
          <button className="icon-btn" onClick={toggleNotif} aria-label="通知">
            <Bell size={15} strokeWidth={1.8} />
            {unreadTotal > 0 && <span className="dot" />}
          </button>
          <button className="avatar-btn" onClick={() => setPage("profile")} aria-label="个人中心">
            {initials || "学"}
          </button>
        </header>

        <main className={`content ${page === "chat" ? "content-chat" : ""}`}>
          <div className={`page ${page === "chat" ? "page-chat" : ""}`}>
            {page !== "home" && page !== "chat" && (
              <Masthead
                kicker={masthead.kicker}
                title={masthead.title}
                aside={masthead.aside.map((l) => (
                  <div key={l}>{l}</div>
                ))}
              />
            )}
            {page === "home" && <HomePage goChat={goChat} go={(p) => setPage(p)} knowledgeSummaryAvailable={knowledgeSummaryAvailable} />}
            {page === "chat" && <ChatPage initialPrompt={chatDraft} chatTarget={chatTarget} onOpenSummary={openKnowledgeSummary} />}
            {page === "plan" && <PlanPage goChat={goChat} />}
            {page === "map" && <KnowledgeMapPage goChat={goChat} />}
            {page === "summaries" && knowledgeSummaryAvailable && (
              <KnowledgeSummariesPage
                onOpenChat={openChatTarget}
                targetSummaryId={summaryTargetId}
                onTargetConsumed={() => setSummaryTargetId(null)}
                onFeatureUnavailable={handleSummaryFeatureUnavailable}
              />
            )}
            {communityVisited && (
              <div style={page === "community" ? undefined : { display: "none" }}>
                <CommunityPage
                  targetPostId={communityTargetPostId}
                  onTargetConsumed={() => setCommunityTargetPostId(null)}
                  onLoginRequired={communityLoginRequired}
                />
              </div>
            )}
            {page === "profile" && <ProfilePage />}
          </div>
        </main>
      </div>

      {showNotif && (
        <NotifPanel
          items={notifications}
          error={notifError}
          onClose={() => setShowNotif(false)}
          onReadAll={() => void readAll()}
          onOpenPost={openCommunityPost}
        />
      )}
    </div>
  );
}

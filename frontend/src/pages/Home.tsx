// 首页：根据真实学习活动区分新用户引导与已有用户学习概览。
import { useEffect, useState } from "react";
import { ArrowRight, LogIn, MessageCircle, RotateCcw, Sparkles } from "lucide-react";
import { useAuth } from "../auth/AuthContext";
import { listConversations } from "../api/conversations";
import { getKnowledgeSummaryStats } from "../api/knowledgeSummaries";
import { getMemoryIndex, getMyGraphStates } from "../api/memory";
import { planMeta, todayTasks, user, weekMinutes } from "../data";
import type { PageKey } from "../data";
import { MasteryRadar, SectionHead } from "../ui";

const WEEKDAYS = ["五", "六", "日", "一", "二", "三", "四"];

const FIRST_DAY_TASKS = [
  {
    id: "question",
    title: "写下一个真正想弄懂的问题",
    description: "从最近卡住的一处开始，不必先决定章节。",
    kind: "探索",
    minutes: 5,
    prompt: "我想开始今天的数学学习。请先通过几个简短问题，帮我找到一个真正值得弄懂的数学问题，再带我开始。",
  },
  {
    id: "example",
    title: "用一个例子检验你的理解",
    description: "让定义落到数字、图形或具体步骤上。",
    kind: "练",
    minutes: 15,
    prompt: "我想用一个具体例子检验自己对数学概念的理解。请先让我输入概念，再给我一个由浅入深的例子，不要立刻公布答案。",
  },
  {
    id: "review",
    title: "用自己的话完成一次回顾",
    description: "把结论、理由和仍然困惑的地方留下来。",
    kind: "回顾",
    minutes: 5,
    prompt: "请带我做一次 5 分钟数学复盘，依次问我今天理解了什么、依据是什么、还有哪里不确定，并帮我整理成简短记录。",
  },
] as const;

type HomeActivityState = "loading" | "new" | "returning";

type HomePageProps = {
  goChat: (prompt?: string) => void;
  go: (page: PageKey) => void;
  knowledgeSummaryAvailable?: boolean;
};

function useHomeActivityState(): HomeActivityState {
  const { user: authUser, ready } = useAuth();
  const [state, setState] = useState<HomeActivityState>("loading");

  useEffect(() => {
    if (!ready) {
      setState("loading");
      return;
    }
    if (!authUser) {
      setState("new");
      return;
    }

    let cancelled = false;
    setState("loading");

    // 主页没有独立聚合接口，先用三个真实数据源判断用户是否已经开始学习。
    void Promise.allSettled([
      listConversations(undefined, 1),
      getMemoryIndex(),
      getMyGraphStates(),
    ]).then(([conversationResult, memoryResult, graphResult]) => {
      if (cancelled) return;
      const hasConversation =
        conversationResult.status === "fulfilled" && conversationResult.value.items.length > 0;
      const hasMemory =
        memoryResult.status === "fulfilled" && memoryResult.value.entries.length > 0;
      const hasGraphState =
        graphResult.status === "fulfilled" && graphResult.value.length > 0;
      setState(hasConversation || hasMemory || hasGraphState ? "returning" : "new");
    });

    return () => {
      cancelled = true;
    };
  }, [authUser, ready]);

  return state;
}

function HomeLoading() {
  return (
    <div className="home-loading rise" role="status" aria-live="polite">
      <span className="home-loading-mark">∴</span>
      <span>正在准备你的学习主页…</span>
    </div>
  );
}

function NewUserHome({ goChat, go }: HomePageProps) {
  const { user: authUser } = useAuth();
  const isLoggedIn = authUser !== null;
  const displayName = authUser?.username ?? "同学";
  const todayIndex = (new Date().getDay() + 2) % WEEKDAYS.length;

  const begin = (prompt?: string) => {
    if (isLoggedIn) {
      goChat(prompt);
    } else {
      go("profile");
    }
  };

  return (
    <div className="onboarding-home">
      <section className="onboarding-hero rise">
        <div className="onboarding-hero-copy">
          <div className="onboarding-kicker">
            {isLoggedIn ? "YOUR FIRST CHAPTER · 第一章" : "XUESHEN MATH STUDIO · 学神数学"}
          </div>
          <h1 className="onboarding-title">
            {isLoggedIn ? <>欢迎，{displayName}。</> : <>别急着看进度，</>}
            <br />
            从一个<span className="mark">真正的问题</span>开始。
          </h1>
          <p className="onboarding-lead">
            你的学习记录现在是空的，这正是它应有的样子。下面只提供一份可执行的首日学习单；完成后，主页才会逐步形成属于你的任务、知识状态与回顾线索。
          </p>
          <div className="hero-actions onboarding-actions">
            <button
              className="btn btn-red"
              type="button"
              onClick={() => begin(FIRST_DAY_TASKS[0].prompt)}
            >
              {isLoggedIn ? <Sparkles size={15} /> : <LogIn size={15} />}
              {isLoggedIn ? "开始今日学习" : "登录后开始学习"}
            </button>
            <a className="btn btn-ghost" href="#today-start">
              查看今日学习单 <ArrowRight size={14} />
            </a>
          </div>
          {!isLoggedIn && (
            <div className="onboarding-account-note">浏览无需登录；学习记录与进度会在登录后保存。</div>
          )}
        </div>
      </section>

      <div id="today-start" className="home-grid new-user-dashboard">
        <div>
          <SectionHead
            num="01"
            title="今日学习单"
            note={`0/${FIRST_DAY_TASKS.length} DONE`}
            action={
              <button className="link-btn" type="button" onClick={() => go(isLoggedIn ? "plan" : "profile")}>
                制定完整计划 <ArrowRight size={13} />
              </button>
            }
          />
          <div className="rise" style={{ animationDelay: "0.08s" }}>
            {FIRST_DAY_TASKS.map((task, index) => (
              <button
                key={task.id}
                className="t-item new-user-task"
                type="button"
                onClick={() => begin(task.prompt)}
                aria-label={`开始任务：${task.title}`}
              >
                <span className="t-idx">{String(index + 1).padStart(2, "0")}</span>
                <span className="t-body">
                  <span className="t-title">{task.title}</span>
                  <span className="t-description">{task.description}</span>
                </span>
                <span className="t-meta">
                  <span className={`tag ${task.kind === "回顾" ? "red" : task.kind === "练" ? "gold" : ""}`}>
                    {task.kind}
                  </span>
                  {task.minutes}min
                  <ArrowRight className="new-user-task-arrow" size={13} />
                </span>
              </button>
            ))}
          </div>

          <SectionHead num="02" title="开始学习" note="FIRST SESSION" />
          <button
            className="continue-strip rise"
            type="button"
            style={{ animationDelay: "0.14s" }}
            onClick={() => begin(FIRST_DAY_TASKS[0].prompt)}
          >
            <MessageCircle size={18} style={{ flexShrink: 0 }} />
            <div style={{ flex: 1, minWidth: 0 }}>
              <div className="continue-title">从第一项开始：找到今天的问题</div>
              <div className="continue-preview">完成后，下一次主页会从真实学习位置接住你。</div>
            </div>
            <ArrowRight size={16} style={{ opacity: 0.6 }} />
          </button>
        </div>

        <div>
          <SectionHead num="03" title="近 7 天" note="0 MIN" />
          <div className="rise" style={{ animationDelay: "0.1s" }}>
            <div className="week-bars new-user-week-bars">
              {WEEKDAYS.map((weekday, index) => (
                <div
                  key={weekday}
                  className={`week-bar ${index === todayIndex ? "today" : ""}`}
                  title={index === todayIndex ? "从今天开始记录" : "暂无学习记录"}
                >
                  <div className="bar" style={{ height: "5%" }} />
                  <span>{weekday}</span>
                </div>
              ))}
            </div>
            <div className="home-empty-note">从今天的第一分钟开始，学习节奏会在这里留下痕迹。</div>
          </div>

          <SectionHead num="04" title="掌握度" note="WAITING FOR EVIDENCE" />
          <div className="radar-box new-user-radar rise" style={{ animationDelay: "0.16s" }}>
            <MasteryRadar axes={[["代数", 0], ["几何", 0], ["分析", 0]]} />
            <div className="radar-empty-copy">完成首次学习后生成</div>
          </div>

          <div className="stat-strip rise" style={{ animationDelay: "0.22s" }}>
            <div className="stat-cell">
              <div className="stat-num">0<em>条</em></div>
              <div className="stat-label">本周提问</div>
            </div>
            <div className="stat-cell">
              <div className="stat-num">0<em>个</em></div>
              <div className="stat-label">已掌握知识点</div>
            </div>
            <div className="stat-cell">
              <div className="stat-num">0<em>篇</em></div>
              <div className="stat-label">知识总结</div>
            </div>
          </div>
        </div>
      </div>
    </div>
  );
}

function ReturningUserHome({ goChat, go, knowledgeSummaryAvailable = false }: HomePageProps) {
  const { user: authUser } = useAuth();
  const done = todayTasks.filter((task) => task.done).length;
  const maxMin = Math.max(...weekMinutes, 1);
  const [summaryStats, setSummaryStats] = useState<Awaited<ReturnType<typeof getKnowledgeSummaryStats>> | null>(null);

  useEffect(() => {
    if (!knowledgeSummaryAvailable) return;
    void getKnowledgeSummaryStats().then(setSummaryStats).catch(() => setSummaryStats(null));
  }, [knowledgeSummaryAvailable]);

  return (
    <>
      <div className="hero rise">
        <div className="hero-ghost">λ</div>
        <div className="hero-kicker">第 {user.streakDays} 天连续学习</div>
        <h1 className="hero-greet">
          欢迎回来，{authUser?.username ?? "同学"}。
          <br />
          今天继续攻克 <span className="mark">特征值与对角化</span>。
        </h1>
        <div className="hero-sub">
          「{planMeta.goal}」进行到{planMeta.weekLabel}，今日 {todayTasks.length} 项任务已完成 {done} 项。
          最近的问答会在这里沉淀成可复用知识，随时可以回来看。
        </div>
        <div className="hero-actions">
          <button className="btn btn-red" onClick={() => goChat()}>
            <MessageCircle size={15} /> 去问 AI
          </button>
          {knowledgeSummaryAvailable && (
            <button className="btn btn-ghost" onClick={() => go("summaries")}>
              <RotateCcw size={15} /> 查看最近更新
            </button>
          )}
        </div>

        <div className="hero-seal">
          <div className="seal">
            <div className="seal-num">
              {user.streakDays}
              <small>天连续</small>
            </div>
          </div>
          <div className="seal-label">超过 87% 的同学</div>
        </div>
      </div>

      <div className="home-grid">
        <div>
          <SectionHead
            num="01"
            title="今日任务"
            note={`${done}/${todayTasks.length} DONE`}
            action={
              <button className="link-btn" onClick={() => go("plan")}>
                完整计划 <ArrowRight size={13} />
              </button>
            }
          />
          <div className="rise" style={{ animationDelay: "0.1s" }}>
            {todayTasks.map((task, index) => (
              <div key={task.id} className={`t-item ${task.done ? "done" : ""}`}>
                <span className="t-idx">{task.done ? "✓" : String(index + 1).padStart(2, "0")}</span>
                <span className="t-body">
                  <span className="t-title">{task.title}</span>
                </span>
                <span className="t-meta">
                  <span className={`tag ${task.kind === "回顾" ? "red" : task.kind === "练" ? "gold" : ""}`}>
                    {task.kind}
                  </span>
                  {task.minutes}min
                </span>
              </div>
            ))}
          </div>

          <SectionHead num="02" title="继续学习" note="RESUME" />
          <button className="continue-strip rise" style={{ animationDelay: "0.16s" }} onClick={() => goChat()}>
            <MessageCircle size={18} style={{ flexShrink: 0 }} />
            <div style={{ flex: 1, minWidth: 0 }}>
              <div className="continue-title">特征值与特征向量</div>
              <div className="continue-preview">几何上，特征向量是变换下方向不变的直线……</div>
            </div>
            <ArrowRight size={16} style={{ opacity: 0.6 }} />
          </button>
        </div>

        <div>
          <SectionHead num="03" title="近 7 天" note="MIN / DAY" />
          <div className="rise" style={{ animationDelay: "0.12s" }}>
            <div className="week-bars">
              {weekMinutes.map((minutes, index) => (
                <div
                  key={WEEKDAYS[index]}
                  className={`week-bar ${minutes > 0 ? "filled" : ""} ${index === 4 ? "today" : ""}`}
                  title={`${minutes} 分钟`}
                >
                  <div className="bar" style={{ height: `${Math.max((minutes / maxMin) * 100, 5)}%` }} />
                  <span>{WEEKDAYS[index]}</span>
                </div>
              ))}
            </div>
          </div>

          <SectionHead num="04" title="掌握度" note="BY DOMAIN" />
          <div className="radar-box rise" style={{ animationDelay: "0.18s" }}>
            <MasteryRadar axes={[["线性代数", 0.62], ["微积分", 0.81], ["概率论", 0.4]]} />
          </div>

          <div className="stat-strip rise" style={{ animationDelay: "0.24s" }}>
            <div className="stat-cell">
              <div className="stat-num">47<em>条</em></div>
              <div className="stat-label">本周提问</div>
            </div>
            <div className="stat-cell">
              <div className="stat-num">11<em>个</em></div>
              <div className="stat-label">已掌握知识点</div>
            </div>
            <div className="stat-cell">
              <div className="stat-num">{summaryStats?.active_count ?? "—"}<em>篇</em></div>
              <div className="stat-label">知识总结</div>
            </div>
          </div>
        </div>
      </div>
    </>
  );
}

export function HomePage(props: HomePageProps) {
  const state = useHomeActivityState();

  if (state === "loading") return <HomeLoading />;
  if (state === "new") return <NewUserHome {...props} />;
  return <ReturningUserHome {...props} />;
}

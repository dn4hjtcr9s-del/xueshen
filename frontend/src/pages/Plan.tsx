// 学习计划页：新用户保持空白并展示引导；已有计划记忆时沿用原有版式
//（巨型百分比数字 + 逐日任务），其中“练”任务可联动 AI 对话。
import { useCallback, useEffect, useState } from "react";
import { ArrowRight, ArrowUpRight, MessageCircle, Pencil } from "lucide-react";
import { useAuth } from "../auth/AuthContext";
import { getLearner, MemoryApiError, type LearnerMemoryView } from "../api/memory";
import { SectionHead } from "../ui";

const NEW_PLAN_PROMPT =
  "我想制定一份数学学习计划。请先了解我的学习目标、每天可投入的时间和当前薄弱点，再给我一份可执行的周计划。";

const ADJUST_PLAN_PROMPT =
  "我想调整学习计划。请先和我确认新的学习目标、每周可投入时间与当前薄弱点，再更新我的周计划。";

const GUIDE_STEPS = [
  {
    id: "goal",
    title: "告诉 AI 你的学习目标",
    description: "例如：六周内建立线性代数的整体直觉，或优先补齐最近考卷上的薄弱章节。",
    kind: "练" as const,
    cta: true,
  },
  {
    id: "context",
    title: "让 AI 了解你的时间与基础",
    description: "在对话中说明每周能学习几天、单次时长，以及最近卡住的知识点。",
    kind: "学" as const,
    cta: false,
  },
  {
    id: "generate",
    title: "计划生成后在这里显示",
    description: "AI 会输出可执行任务；本页将显示完成百分比和逐日安排，不再预填示例计划。",
    kind: "复习" as const,
    cta: false,
  },
] as const;

type PlanTaskKind = "学" | "练" | "复习";

interface RenderedPlanTask {
  id: string;
  title: string;
  kind: PlanTaskKind;
  topic: string;
  done: boolean;
  minutes?: number;
}

interface RenderedPlanDay {
  id: string;
  weekday: string;
  date: string;
  isToday: boolean;
  tasks: RenderedPlanTask[];
}

function formatMonthDay(value: string): string {
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return "";
  return `${date.getMonth() + 1} 月 ${date.getDate()} 日`;
}

function buildPlanFromLearner(learner: LearnerMemoryView): {
  goal: string;
  progress: number;
  weekLabel: string;
  createdAt: string;
  days: RenderedPlanDay[];
} {
  const today = new Date();
  const weekday = `周${"日一二三四五六".charAt(today.getDay())}`;
  const tasks: RenderedPlanTask[] = learner.plans.map((plan, index) => ({
    id: `learner-plan-${index}`,
    title: plan,
    kind: "学",
    topic: "当前计划",
    done: false,
  }));

  return {
    goal: learner.goals[0] ?? "我的数学学习计划",
    progress: 0,
    weekLabel: "第 1 周 · 由 AI 学习记忆生成",
    createdAt: formatMonthDay(learner.updated_at),
    days: [
      {
        id: "learner-plan-today",
        weekday,
        date: `${today.getMonth() + 1}/${today.getDate()}`,
        isToday: true,
        tasks,
      },
    ],
  };
}

export function PlanPage({ goChat }: { goChat: (prompt?: string) => void }) {
  const { user: authUser, ready } = useAuth();
  const [learner, setLearner] = useState<LearnerMemoryView | null | undefined>(undefined);
  const [loadError, setLoadError] = useState<string | null>(null);

  const load = useCallback(async () => {
    setLoadError(null);
    try {
      setLearner(await getLearner());
    } catch (error) {
      setLoadError(
        error instanceof MemoryApiError ? error.message : "学习计划加载失败，请稍后重试。",
      );
    }
  }, []);

  useEffect(() => {
    if (!ready) return;
    if (!authUser) {
      setLearner(null);
      return;
    }
    void load();
  }, [authUser, ready, load]);

  if (!ready || learner === undefined) {
    return (
      <div role="status" className="section-note" style={{ padding: "18px 0" }}>
        正在读取你的计划记忆…
      </div>
    );
  }

  if (loadError) {
    return (
      <>
        <SectionHead num="01" title="本周安排" note="PLAN" />
        <div className="memory-banner" role="alert" style={{ marginTop: 18 }}>
          <span>{loadError}</span>
          <button className="link-btn" onClick={() => void load()}>
            重试
          </button>
        </div>
      </>
    );
  }

  if (!learner || learner.plans.length === 0) {
    return (
      <>
        <div className="plan-mast rise">
          <div className="plan-pct">
            0<sup>%</sup>
          </div>
          <div className="plan-mast-info">
            <div className="plan-goal">还没有学习计划</div>
            <div className="plan-week">新用户默认空白 · 等待你的第一个目标</div>
            <div className="plan-sub">
              计划由 AI 结合你的掌握度生成，并在每周日晚自动调整。这里不会预填示例任务。
            </div>
          </div>
          <button className="btn btn-ghost" onClick={() => goChat(NEW_PLAN_PROMPT)}>
            <MessageCircle size={14} /> 去制定计划
          </button>
        </div>

        <SectionHead num="01" title="如何开始" note="NEW USER GUIDE" />
        <div className="rise" style={{ animationDelay: "0.08s" }}>
          {GUIDE_STEPS.map((step, index) => (
            <div key={step.id} className="t-item">
              <span className="t-idx">{String(index + 1).padStart(2, "0")}</span>
              <span className="t-body">
                <span className="t-title">{step.title}</span>
                <span className="t-description">{step.description}</span>
              </span>
              <span className="t-meta">
                <span
                  className={`tag ${step.kind === "复习" ? "red" : step.kind === "练" ? "gold" : ""}`}
                >
                  {step.kind}
                </span>
                {step.cta && (
                  <button className="link-btn" onClick={() => goChat(NEW_PLAN_PROMPT)}>
                    去问 AI <ArrowUpRight size={12} />
                  </button>
                )}
              </span>
            </div>
          ))}
        </div>

        <button
          className="continue-strip rise"
          type="button"
          style={{ animationDelay: "0.14s" }}
          onClick={() => goChat(NEW_PLAN_PROMPT)}
        >
          <MessageCircle size={18} style={{ flexShrink: 0 }} />
          <div style={{ flex: 1, minWidth: 0 }}>
            <div className="continue-title">从第一个目标开始制定计划</div>
            <div className="continue-preview">
              对话完成后，AI 会把识别到的计划写入学习记忆，计划页再自动生成安排。
            </div>
          </div>
          <ArrowRight size={16} style={{ opacity: 0.6 }} />
        </button>
      </>
    );
  }

  const plan = buildPlanFromLearner(learner);
  let taskNo = 0;

  return (
    <>
      <div className="plan-mast rise">
        <div className="plan-pct">
          {Math.round(plan.progress * 100)}
          <sup>%</sup>
        </div>
        <div className="plan-mast-info">
          <div className="plan-goal">「{plan.goal}」</div>
          <div className="plan-week">{plan.weekLabel}</div>
          <div className="plan-sub">
            创建于 {plan.createdAt} · 由 AI 结合你的掌握度生成 · 每周日晚自动调整下周节奏
          </div>
        </div>
        <button className="btn btn-ghost" onClick={() => goChat(ADJUST_PLAN_PROMPT)}>
          <Pencil size={14} /> 调整目标
        </button>
      </div>

      <SectionHead num="01" title="本周安排" note="WEEK 1" />
      <div className="rise" style={{ animationDelay: "0.08s" }}>
        {plan.days.map((day) => (
          <div key={day.id} className={`plan-day ${day.isToday ? "today" : ""}`}>
            <div className="plan-day-head">
              <span className="wd">{day.weekday}</span>
              <span className="dt">{day.date}</span>
              <span className="right">
                {day.tasks.filter((t) => t.done).length}/{day.tasks.length} 完成 ·{" "}
                {day.tasks.reduce((s, t) => s + (t.minutes ?? 0), 0)} min
              </span>
            </div>
            <div>
              {day.tasks.map((t) => {
                taskNo += 1;
                return (
                  <div key={t.id} className={`t-item ${t.done ? "done" : ""}`}>
                    <span className="t-idx">{t.done ? "✓" : String(taskNo).padStart(2, "0")}</span>
                    <span className="t-body">
                      <span className="t-title">{t.title}</span>
                    </span>
                    <span className="t-meta">
                      <span
                        className={`tag ${t.kind === "复习" ? "red" : t.kind === "练" ? "gold" : ""}`}
                      >
                        {t.kind}
                      </span>
                      {typeof t.minutes === "number" ? `${t.minutes}min` : "时长待排"}
                      {t.kind === "练" && (
                        <button className="link-btn" onClick={() => goChat()}>
                          去问 AI <ArrowUpRight size={12} />
                        </button>
                      )}
                    </span>
                  </div>
                );
              })}
            </div>
          </div>
        ))}
      </div>
    </>
  );
}

// 学习计划页：巨型百分比数字 + 逐日任务（编辑式编号清单，"练"任务联动 AI 对话）。
import { ArrowUpRight, Pencil } from "lucide-react";
import { planMeta, weekPlan } from "../data";
import { SectionHead } from "../ui";

export function PlanPage({ goChat }: { goChat: () => void }) {
  let taskNo = 0;
  return (
    <>
      <div className="plan-mast rise">
        <div className="plan-pct">
          {Math.round(planMeta.progress * 100)}
          <sup>%</sup>
        </div>
        <div className="plan-mast-info">
          <div className="plan-goal">「{planMeta.goal}」</div>
          <div className="plan-week">{planMeta.weekLabel}</div>
          <div className="plan-sub">
            创建于 {planMeta.createdAt} · 由 AI 结合你的掌握度生成 · 每周日晚自动调整下周节奏
          </div>
        </div>
        <button className="btn btn-ghost">
          <Pencil size={14} /> 调整目标
        </button>
      </div>

      <SectionHead num="01" title="本周安排" note="WEEK 4 / 6" />
      <div className="rise" style={{ animationDelay: "0.08s" }}>
        {weekPlan.map((d) => (
          <div key={d.date} className={`plan-day ${d.isToday ? "today" : ""}`}>
            <div className="plan-day-head">
              <span className="wd">{d.weekday}</span>
              <span className="dt">{d.date}</span>
              <span className="right">
                {d.tasks.filter((t) => t.done).length}/{d.tasks.length} 完成 ·{" "}
                {d.tasks.reduce((s, t) => s + t.minutes, 0)} min
              </span>
            </div>
            <div>
              {d.tasks.map((t) => {
                taskNo += 1;
                return (
                  <div key={t.id} className={`t-item ${t.done ? "done" : ""}`}>
                    <span className="t-idx">{t.done ? "✓" : String(taskNo).padStart(2, "0")}</span>
                    <span className="t-body">
                      <span className="t-title">{t.title}</span>
                    </span>
                    <span className="t-meta">
                      <span className={`tag ${t.kind === "复习" ? "red" : t.kind === "练" ? "gold" : ""}`}>
                        {t.kind}
                      </span>
                      {t.minutes}min
                      {t.kind === "练" && (
                        <button className="link-btn" onClick={goChat}>
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

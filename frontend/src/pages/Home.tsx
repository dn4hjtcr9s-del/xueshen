// 首页 / 今日：社论式 hero（超大标题 + 巨型 λ + 压界印章）+ 编号任务清单 + 活动与掌握度。
import { ArrowRight, MessageCircle, RotateCcw } from "lucide-react";
import { planMeta, todayTasks, user, weekMinutes } from "../data";
import type { PageKey } from "../data";
import { MasteryRadar, SectionHead } from "../ui";

const WEEKDAYS = ["五", "六", "日", "一", "二", "三", "四"];

export function HomePage({ goChat, go }: { goChat: () => void; go: (p: PageKey) => void }) {
  const done = todayTasks.filter((t) => t.done).length;
  const maxMin = Math.max(...weekMinutes, 1);

  return (
    <>
      <div className="hero rise">
        <div className="hero-ghost">λ</div>
        <div className="hero-kicker">TUESDAY · 2026.08.04 · 第 {user.streakDays} 天连续学习</div>
        <h1 className="hero-greet">
          晚上好，{user.name}。
          <br />
          今天继续攻克 <span className="mark">特征值与对角化</span>。
        </h1>
        <div className="hero-sub">
          「{planMeta.goal}」进行到{planMeta.weekLabel}，今日 {todayTasks.length} 项任务已完成 {done} 项。
          还有 3 条错题到期，复习完正好闭环。
        </div>
        <div className="hero-actions">
          <button className="btn btn-red" onClick={goChat}>
            <MessageCircle size={15} /> 去问 AI
          </button>
          <button className="btn btn-ghost" onClick={() => go("notebook")}>
            <RotateCcw size={15} /> 复习到期错题
          </button>
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
            {todayTasks.map((t, i) => (
              <div key={t.id} className={`t-item ${t.done ? "done" : ""}`}>
                <span className="t-idx">{t.done ? "✓" : String(i + 1).padStart(2, "0")}</span>
                <span className="t-body">
                  <span className="t-title">{t.title}</span>
                </span>
                <span className="t-meta">
                  <span className={`tag ${t.kind === "复习" ? "red" : t.kind === "练" ? "gold" : ""}`}>
                    {t.kind}
                  </span>
                  {t.minutes}min
                </span>
              </div>
            ))}
          </div>

          <SectionHead num="02" title="继续学习" note="RESUME" />
          <button className="continue-strip rise" style={{ animationDelay: "0.16s" }} onClick={goChat}>
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
              {weekMinutes.map((m, i) => (
                <div
                  key={i}
                  className={`week-bar ${m > 0 ? "filled" : ""} ${i === 4 ? "today" : ""}`}
                  title={`${m} 分钟`}
                >
                  <div className="bar" style={{ height: `${Math.max((m / maxMin) * 100, 5)}%` }} />
                  <span>{WEEKDAYS[i]}</span>
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
              <div className="stat-num">5<em>篇</em></div>
              <div className="stat-label">错题本收藏</div>
            </div>
          </div>
        </div>
      </div>
    </>
  );
}

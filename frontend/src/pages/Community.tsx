// 社区页：讨论区（编号社论清单）/ 学习小组 / 打卡圈（月历 + 点线排行榜）。
import { useState } from "react";
import { Heart, MessageSquare, Pin, CheckCircle2 } from "lucide-react";
import { checkin, communityPosts, studyGroups } from "../data";

const TABS = [
  { key: "讨论区", cnt: communityPosts.length },
  { key: "学习小组", cnt: studyGroups.length },
  { key: "打卡圈", cnt: null },
] as const;

function DiscussionTab() {
  return (
    <div className="rise">
      {communityPosts.map((p, i) => (
        <div key={p.id} className="post-row">
          <span className="post-idx">{String(i + 1).padStart(2, "0")}</span>
          <div className="post-main">
            <div className="post-title">
              {p.pinned && <Pin size={13} className="pin" />}
              {p.solved && <CheckCircle2 size={14} color="var(--pine)" style={{ marginRight: 6, verticalAlign: -2 }} />}
              {p.title}
            </div>
            <div className="post-meta">
              <span className="tag">{p.board}</span>
              <span>{p.author}</span>
              <span>{p.time}</span>
            </div>
          </div>
          <div className="post-stats">
            <span><MessageSquare size={11} style={{ verticalAlign: -1 }} /> {p.replies}</span>
            <span><Heart size={11} style={{ verticalAlign: -1 }} /> {p.likes}</span>
          </div>
        </div>
      ))}
    </div>
  );
}

function GroupsTab() {
  return (
    <div className="group-grid rise">
      {studyGroups.map((g) => (
        <div key={g.id} className="card group-card">
          <div className="group-name">{g.name}</div>
          <div className="group-desc">{g.desc}</div>
          <div className="group-foot">
            <span className="group-members">
              {g.members} 人 · 今日 {g.todayActive} 人活跃
            </span>
            <button className={`btn ${g.joined ? "btn-ghost" : "btn-primary"}`} style={{ padding: "6px 13px", fontSize: 12.5 }}>
              {g.joined ? "已加入" : "加入"}
            </button>
          </div>
        </div>
      ))}
    </div>
  );
}

function CheckinTab() {
  const weekHeads = ["一", "二", "三", "四", "五", "六", "日"];
  return (
    <div className="checkin-layout rise">
      <div>
        <div className="cal-grid">
          {weekHeads.map((w) => (
            <div key={w} className="cal-head">{w}</div>
          ))}
          {checkin.monthDays.map((hit, i) => (
            <div key={i} className={`cal-cell ${hit ? "hit" : ""} ${i === 3 ? "today" : ""}`}>
              {i + 1}
            </div>
          ))}
        </div>
      </div>
      <div>
        <div className="section-head" style={{ marginTop: 0 }}>
          <div className="section-title" style={{ fontSize: 16 }}>8 月打卡榜</div>
          <div className="section-note">线代攻坚小队</div>
        </div>
        {checkin.leaderboard.map((r, i) => (
          <div key={r.name} className={`lb-row ${r.me ? "me" : ""}`}>
            <span className="lb-rank">{i + 1}</span>
            <span className="lb-name">{r.name}{r.me && "（我）"}</span>
            <span className="lb-leader" />
            <span className="lb-days">{r.days} 天</span>
          </div>
        ))}
      </div>
    </div>
  );
}

export function CommunityPage() {
  const [tab, setTab] = useState<(typeof TABS)[number]["key"]>("讨论区");
  return (
    <>
      <div className="comm-tabs rise">
        {TABS.map((t) => (
          <button key={t.key} className={`comm-tab ${tab === t.key ? "active" : ""}`} onClick={() => setTab(t.key)}>
            {t.key}
            {t.cnt !== null && <span className="cnt">{t.cnt}</span>}
          </button>
        ))}
      </div>
      {tab === "讨论区" && <DiscussionTab />}
      {tab === "学习小组" && <GroupsTab />}
      {tab === "打卡圈" && <CheckinTab />}
    </>
  );
}

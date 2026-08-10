// 知识地图页：论文插图式星图（硬框 + 图注）+ 侧边详情卡（剪贴感）。
import { useMemo, useState } from "react";
import { MessageCircle } from "lucide-react";
import { knowledgeNodes } from "../data";
import { masteryColor, masteryLabel, SectionHead } from "../ui";

export function KnowledgeMapPage({ goChat }: { goChat: () => void }) {
  const [selectedId, setSelectedId] = useState("k5");
  const selected = useMemo(
    () => knowledgeNodes.find((n) => n.id === selectedId) ?? knowledgeNodes[0],
    [selectedId]
  );

  const edges = useMemo(
    () =>
      knowledgeNodes.flatMap((n) =>
        n.linkTo.map((to) => {
          const target = knowledgeNodes.find((t) => t.id === to)!;
          return { from: n, to: target, key: `${n.id}-${to}` };
        })
      ),
    []
  );

  return (
    <div style={{ maxWidth: 1160, margin: "0 auto" }}>
      <SectionHead
        num="01"
        title="知识点星图"
        note="节点颜色 = AI 记忆中的掌握度，随学习实时更新"
      />
      <div className="map-layout" style={{ marginTop: 18 }}>
        <div className="rise">
          <div className="map-figure">
            <svg viewBox="0 0 700 560">
              {edges.map(({ from, to, key }) => (
                <line
                  key={key}
                  x1={from.x}
                  y1={from.y}
                  x2={to.x}
                  y2={to.y}
                  stroke="var(--line)"
                  strokeWidth="1.4"
                  strokeDasharray="5 4"
                />
              ))}
              {knowledgeNodes.map((n) => {
                const active = n.id === selectedId;
                const color = masteryColor(n.mastery);
                return (
                  <g
                    key={n.id}
                    onClick={() => setSelectedId(n.id)}
                    style={{ cursor: "pointer" }}
                  >
                    {active && (
                      <circle cx={n.x} cy={n.y} r={34} fill="none" stroke={color} strokeWidth="1.5" strokeDasharray="3 4" opacity={0.8} />
                    )}
                    <circle
                      cx={n.x}
                      cy={n.y}
                      r={26}
                      fill="var(--card)"
                      stroke={color}
                      strokeWidth={active ? 3 : 2}
                    />
                    <circle
                      cx={n.x}
                      cy={n.y}
                      r={26}
                      fill="none"
                      stroke={color}
                      strokeWidth="4"
                      strokeLinecap="round"
                      strokeDasharray={2 * Math.PI * 26}
                      strokeDashoffset={2 * Math.PI * 26 * (1 - n.mastery)}
                      transform={`rotate(-90 ${n.x} ${n.y})`}
                      opacity={0.45}
                    />
                    <text x={n.x} y={n.y - 2} textAnchor="middle" className="map-node-label" fontWeight={600}>
                      {n.name}
                    </text>
                    <text x={n.x} y={n.y + 13} textAnchor="middle" className="map-node-label">
                      <tspan className="pct">{Math.round(n.mastery * 100)}%</tspan>
                    </text>
                  </g>
                );
              })}
            </svg>
            <div className="map-caption">图 1 · 知识点星图 — FIG. 1 KNOWLEDGE CONSTELLATION（虚线 = 先修依赖）</div>
          </div>
          <div className="map-legend">
            <span><i style={{ background: "var(--pine)" }} />已掌握 ≥75%</span>
            <span><i style={{ background: "var(--gold)" }} />巩固中 45–75%</span>
            <span><i style={{ background: "var(--cinnabar)" }} />薄弱 &lt;45%</span>
          </div>
        </div>

        <div className="card map-detail rise" style={{ animationDelay: "0.1s" }}>
          <div className="map-detail-name">{selected.name}</div>
          <div className="map-detail-domain">
            <span className="tag">{selected.domain}</span>{" "}
            <span className={`tag ${selected.mastery >= 0.75 ? "green" : selected.mastery >= 0.45 ? "gold" : "red"}`}>
              {masteryLabel(selected.mastery)}
            </span>
          </div>
          <div className="section-note">掌握度 {Math.round(selected.mastery * 100)}%</div>
          <div className="mastery-bar">
            <i style={{ width: `${selected.mastery * 100}%`, background: masteryColor(selected.mastery) }} />
          </div>
          <div className="map-detail-note">
            {selected.mastery < 0.45
              ? "AI 判断这个知识点是当前短板，建议安排一次针对性讲解 + 一组追问检验。"
              : selected.mastery < 0.75
                ? "概念框架已建立，细节还不够稳，适合通过错题复习巩固。"
                : "掌握扎实，可以在社区里尝试给别人讲一遍——讲清楚才算真懂。"}
          </div>
          <button className="btn btn-red" style={{ width: "100%", justifyContent: "center" }} onClick={goChat}>
            <MessageCircle size={15} /> 针对「{selected.name}」提问
          </button>
        </div>
      </div>
    </div>
  );
}

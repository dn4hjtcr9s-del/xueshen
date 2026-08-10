// 共享 UI 小组件：刊头、章节标题、掌握度雷达、掌握度配色。
import type { ReactNode } from "react";

/** 掌握度 → 设计系统配色（朱砂=薄弱，金=巩固中，松绿=已掌握） */
export function masteryColor(m: number): string {
  if (m >= 0.75) return "var(--pine)";
  if (m >= 0.45) return "var(--gold)";
  return "var(--cinnabar)";
}

export function masteryLabel(m: number): string {
  if (m >= 0.75) return "已掌握";
  if (m >= 0.45) return "巩固中";
  return "薄弱";
}

/** 报纸式刊头：红色 kicker + 超大宋体标题 + 右侧 mono 元信息 */
export function Masthead({
  kicker,
  title,
  aside,
}: {
  kicker: string;
  title: string;
  aside?: ReactNode;
}) {
  return (
    <div className="masthead rise">
      <div>
        <div className="masthead-kicker">{kicker}</div>
        <h1 className="masthead-title">{title}</h1>
      </div>
      {aside && <div className="masthead-aside">{aside}</div>}
    </div>
  );
}

export function SectionHead({
  num,
  title,
  note,
  action,
}: {
  num?: string;
  title: string;
  note?: string;
  action?: ReactNode;
}) {
  return (
    <div className="section-head">
      {num && <span className="sec-num">{num}</span>}
      <div className="section-title">{title}</div>
      {note && <div className="section-note">{note}</div>}
      <div className="spacer" />
      {action}
    </div>
  );
}

/** 掌握度雷达图（首页缩略版），axes: [标签, 0-1][] */
export function MasteryRadar({ axes, size = 190 }: { axes: [string, number][]; size?: number }) {
  const cx = size / 2;
  const cy = size / 2;
  const r = size / 2 - 30;
  const n = axes.length;
  const point = (i: number, ratio: number): [number, number] => {
    const angle = (Math.PI * 2 * i) / n - Math.PI / 2;
    return [cx + r * ratio * Math.cos(angle), cy + r * ratio * Math.sin(angle)];
  };
  const polygon = (ratio: (i: number) => number) =>
    axes.map((_, i) => point(i, ratio(i)).join(",")).join(" ");
  return (
    <svg width={size} height={size} viewBox={`0 0 ${size} ${size}`}>
      {[0.33, 0.66, 1].map((rr) => (
        <polygon
          key={rr}
          points={polygon(() => rr)}
          fill="none"
          stroke="var(--line)"
          strokeWidth="1"
          strokeDasharray={rr === 1 ? "none" : "3 3"}
        />
      ))}
      {axes.map(([label], i) => {
        const [x, y] = point(i, 1.18);
        const [x1, y1] = point(i, 0);
        const [x2, y2] = point(i, 1);
        return (
          <g key={label}>
            <line x1={x1} y1={y1} x2={x2} y2={y2} stroke="var(--line)" strokeWidth="1" />
            <text x={x} y={y} textAnchor="middle" dominantBaseline="middle" fontSize="10" fill="var(--ink-soft)" fontFamily="var(--ui)">
              {label}
            </text>
          </g>
        );
      })}
      <polygon
        points={polygon((i) => axes[i][1])}
        fill="rgba(189, 50, 26, 0.14)"
        stroke="var(--cinnabar)"
        strokeWidth="1.8"
        strokeLinejoin="round"
      />
      {axes.map(([, v], i) => {
        const [x, y] = point(i, v);
        return <circle key={i} cx={x} cy={y} r="3" fill="var(--cinnabar)" />;
      })}
    </svg>
  );
}

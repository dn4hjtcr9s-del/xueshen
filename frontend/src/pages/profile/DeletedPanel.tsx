// 已删除记忆（30 天恢复窗口）面板（规格 §20.1 / §2.3）。
import { RotateCcw } from "lucide-react";
import type { DeletedMemoryItem } from "../../api/memory";
import { formatTime } from "./useMemoryCommand";

export function DeletedPanel({
  items,
  pending,
  onRestore,
}: {
  items: DeletedMemoryItem[];
  pending: boolean;
  onRestore: (item: DeletedMemoryItem) => void;
}) {
  if (items.length === 0) return null;
  return (
    <div style={{ marginTop: 26 }}>
      <div className="section-note" style={{ marginBottom: 8 }}>
        已删除 · 30 天内可恢复
      </div>
      {items.map((item) => (
        <div key={item.memory_id} className="memory-item" data-testid={`deleted-${item.memory_id}`}>
          <span className="tag">{item.memory_type === "learner" ? "学习者档案" : "掌握档案"}</span>
          <span className="memory-text" style={{ color: "var(--ink-faint)" }}>
            {item.title}
            <span className="memory-time" style={{ marginLeft: 10 }}>
              删除于 {formatTime(item.deleted_at)} · 可恢复至 {formatTime(item.restore_until)}
            </span>
          </span>
          <span className="memory-actions">
            <button aria-label={`恢复 ${item.title}`} disabled={pending} onClick={() => onRestore(item)}>
              <RotateCcw size={13} />
            </button>
          </span>
        </div>
      ))}
    </div>
  );
}

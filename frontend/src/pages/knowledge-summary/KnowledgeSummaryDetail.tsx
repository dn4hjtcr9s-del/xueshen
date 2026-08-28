/** 知识总结详情、编辑、删除和结构化冲突处理。 */
import { useEffect, useState } from "react";
import { ArrowLeft, Check, Lock, MessageCircle, Pencil, Plus, Save, Trash2, Unlock, X } from "lucide-react";
import {
  deleteKnowledgeSummary,
  dismissKnowledgeSummaryReview,
  getKnowledgeSummary,
  patchKnowledgeSummary,
} from "../../api/knowledgeSummaries";
import { MemoryApiError } from "../../api/client";
import type {
  KnowledgeSummaryArraySection,
  KnowledgeSummaryDetailResponse,
  KnowledgeSummaryItem,
  KnowledgeSummaryPatchRequest,
  KnowledgeSummarySection,
} from "../../types/knowledgeSummary";
import { KnowledgeSummarySources } from "./KnowledgeSummarySources";

const sectionLabels: Record<KnowledgeSummarySection, string> = {
  overview: "概览",
  definitions: "定义",
  theorems: "定理",
  formulas: "公式",
  properties: "性质",
  methods: "方法",
  pitfalls: "易混点",
};
const arraySections: KnowledgeSummaryArraySection[] = [
  "definitions", "theorems", "formulas", "properties", "methods", "pitfalls",
];

type DraftItem = {
  item_id: string | null;
  text: string;
};

type Draft = {
  topic_group_title: string;
  topic_title: string;
  overview: string;
  sections: Record<KnowledgeSummaryArraySection, DraftItem[]>;
};

function toDraft(detail: KnowledgeSummaryDetailResponse): Draft {
  return {
    topic_group_title: detail.topic_group_title,
    topic_title: detail.topic_title,
    overview: detail.content.overview?.text ?? "",
    sections: Object.fromEntries(
      arraySections.map((section) => [
        section,
        detail.content[section].map(({ item_id, text }) => ({ item_id, text })),
      ]),
    ) as Record<KnowledgeSummaryArraySection, DraftItem[]>,
  };
}

function normalizedText(text: string): string {
  return text.trim();
}

function sameItems(current: KnowledgeSummaryItem[], draft: DraftItem[]): boolean {
  const next = draft
    .map((item) => ({ item_id: item.item_id, text: normalizedText(item.text) }))
    .filter((item) => item.text);
  return (
    current.length === next.length &&
    current.every(
      (item, index) => item.item_id === next[index].item_id && item.text === next[index].text,
    )
  );
}

/** 仅提交真正变化的章节，避免把未编辑章节误判为用户修改。 */
function buildPatch(
  detail: KnowledgeSummaryDetailResponse,
  draft: Draft,
  unlockSections: KnowledgeSummarySection[],
): KnowledgeSummaryPatchRequest {
  const patch: KnowledgeSummaryPatchRequest = { expected_version: detail.version };
  if (draft.topic_group_title !== detail.topic_group_title) {
    patch.topic_group_title = draft.topic_group_title;
  }
  if (draft.topic_title !== detail.topic_title) {
    patch.topic_title = draft.topic_title;
  }

  const currentOverview = detail.content.overview;
  const nextOverview = normalizedText(draft.overview);
  if (nextOverview !== (currentOverview?.text ?? "")) {
    patch.overview = nextOverview
      ? { item_id: currentOverview?.item_id ?? null, text: nextOverview }
      : null;
  }

  const changedSections: NonNullable<KnowledgeSummaryPatchRequest["sections"]> =
    Object.fromEntries(
      arraySections
        .filter((section) => !sameItems(detail.content[section], draft.sections[section]))
        .map((section) => [
          section,
          draft.sections[section]
            .map((item) => ({ item_id: item.item_id, text: normalizedText(item.text) }))
            .filter((item) => item.text),
        ]),
    ) as NonNullable<KnowledgeSummaryPatchRequest["sections"]>;
  if (Object.keys(changedSections).length > 0) {
    patch.sections = changedSections;
  }
  if (unlockSections.length > 0) {
    patch.unlock_sections = unlockSections;
  }
  return patch;
}

export function KnowledgeSummaryDetail({
  summaryId,
  onBack,
  onOpenChat,
  onDeleted,
}: {
  summaryId: string;
  onBack: () => void;
  onOpenChat: (threadId: string, turnId: string) => void;
  onDeleted: () => void;
}) {
  const [detail, setDetail] = useState<KnowledgeSummaryDetailResponse | null>(null);
  const [draft, setDraft] = useState<Draft | null>(null);
  const [editing, setEditing] = useState(false);
  const [saving, setSaving] = useState(false);
  const [message, setMessage] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  const [unlockSections, setUnlockSections] = useState<KnowledgeSummarySection[]>([]);

  const refresh = async () => {
    setLoading(true);
    try {
      const next = await getKnowledgeSummary(summaryId);
      setDetail(next);
      setDraft(toDraft(next));
      setUnlockSections([]);
    } catch (cause) {
      setMessage(cause instanceof Error ? cause.message : "总结加载失败");
    } finally {
      setLoading(false);
    }
  };
  // summaryId 变化时重新读取服务端快照。
  // eslint-disable-next-line react-hooks/exhaustive-deps
  useEffect(() => { void refresh(); }, [summaryId]);

  const save = async () => {
    if (!detail || !draft) return;
    setSaving(true);
    setMessage(null);
    try {
      const next = await patchKnowledgeSummary(
        detail.summary_id,
        buildPatch(detail, draft, unlockSections),
      );
      setDetail(next);
      setDraft(toDraft(next));
      setUnlockSections([]);
      setEditing(false);
      setMessage("已保存，当前章节将由你维护。");
    } catch (cause) {
      if (cause instanceof MemoryApiError && cause.status === 409) {
        setMessage("当前总结已被更新，草稿仍保留；请刷新后重新确认。");
      } else {
        setMessage(cause instanceof Error ? cause.message : "保存失败");
      }
    } finally {
      setSaving(false);
    }
  };

  const remove = async () => {
    if (!detail || !window.confirm("删除后总结会立即隐藏，来源记录仍用于审计。确认删除？")) return;
    try {
      await deleteKnowledgeSummary(detail.summary_id, detail.version);
      onDeleted();
    } catch (cause) {
      setMessage(cause instanceof Error ? cause.message : "删除失败");
    }
  };

  if (loading) return <div className="skeleton-block" role="status">正在加载知识总结…</div>;
  if (!detail || !draft) return <div className="empty-state">{message ?? "知识总结不存在"}<button className="link-btn" onClick={onBack}>返回列表</button></div>;

  return (
    <div className="summary-detail rise">
      <button className="link-btn" onClick={onBack} type="button"><ArrowLeft size={14} /> 返回知识总结</button>
      <div className="summary-detail-head">
        <div>
          <span className="tag">{detail.topic_group_title}</span>
          {detail.review_state !== "clean" && (
            <span className="tag red" style={{ marginLeft: 8 }}>
              {detail.review_state === "conflict" ? "待确认" : "可能重复"}
            </span>
          )}
          {editing ? (
            <input className="summary-title-input" value={draft.topic_title} onChange={(e) => setDraft({ ...draft, topic_title: e.target.value })} />
          ) : <h2>{detail.topic_title}</h2>}
        </div>
        <div className="summary-detail-actions">
          {editing ? (
            <>
              <button className="btn btn-red" disabled={saving} onClick={() => void save()}><Save size={14} /> {saving ? "保存中…" : "保存"}</button>
              <button className="btn btn-ghost" onClick={() => { setDraft(toDraft(detail)); setUnlockSections([]); setEditing(false); }}><X size={14} /> 取消</button>
            </>
          ) : (
            <button className="btn btn-ghost" onClick={() => setEditing(true)}><Pencil size={14} /> 编辑</button>
          )}
          <button className="btn btn-ghost" onClick={() => onOpenChat("", "")}><MessageCircle size={14} /> 继续提问</button>
          <button className="btn btn-ghost danger" onClick={() => void remove()}><Trash2 size={14} /> 删除</button>
        </div>
      </div>
      {message && <div className="inline-notice">{message}</div>}
      {detail.review_state !== "clean" && (
        <section className="summary-review-panel">
          <div className="summary-detail-section-head"><div><span className="eyebrow">REVIEW · 需要确认</span><h3>这次更新需要你判断</h3></div></div>
          {detail.pending_reviews.map((review) => (
            <div className="review-proposal" key={review.review_id}>
              <div><strong>{review.proposed_topic_title}</strong><span>{review.reason_code}</span></div>
              <button className="link-btn" onClick={() => void dismissKnowledgeSummaryReview(review.generation_id, review.review_id).then(refresh)} type="button"><Check size={13} /> 忽略这条建议</button>
            </div>
          ))}
          {detail.possible_duplicates.map((duplicate) => <div className="review-proposal" key={duplicate.duplicate_id}><span>可能与「{duplicate.topic_title}」重复（{Math.round(duplicate.match_score * 100)}%）</span></div>)}
        </section>
      )}
      <section className="summary-content">
        <div className="summary-detail-section-head"><div><span className="eyebrow">KNOWLEDGE · 结构化内容</span><h3>可复用知识</h3></div><span className="section-note">v{detail.version}</span></div>
        <div className="summary-ai-notice">✦ 此总结由 AI 根据来源问答整理，你可以随时编辑和校对。</div>
        {editing ? (
          <>
            <EditableOverview
              value={draft.overview}
              protectedSection={detail.protected_sections.includes("overview")}
              unlockRequested={unlockSections.includes("overview")}
              onChange={(value) => {
                setDraft({ ...draft, overview: value });
                setUnlockSections((current) => current.filter((item) => item !== "overview"));
              }}
              onUnlock={() => {
                setUnlockSections((current) =>
                  current.includes("overview")
                    ? current.filter((item) => item !== "overview")
                    : [...current, "overview"],
                );
              }}
            />
            {arraySections.map((section) => (
              <EditableSection
                key={section}
                section={section}
                items={draft.sections[section]}
                protectedSection={detail.protected_sections.includes(section)}
                unlockRequested={unlockSections.includes(section)}
                onChange={(items) => {
                  setDraft({ ...draft, sections: { ...draft.sections, [section]: items } });
                  setUnlockSections((current) => current.filter((item) => item !== section));
                }}
                onUnlock={() => {
                  setUnlockSections((current) =>
                    current.includes(section)
                      ? current.filter((item) => item !== section)
                      : [...current, section],
                  );
                }}
              />
            ))}
            <div className="editor-protection-hint"><Lock size={14} /> 保存后本次修改章节将由你维护，AI 不会自动覆盖；如需恢复自动更新，可在章节旁选择“允许 AI 继续更新”。</div>
          </>
        ) : (
          <>
            {detail.content.overview && <KnowledgeSection title="概览" items={[detail.content.overview]} protectedSection={detail.protected_sections.includes("overview")} />}
            {arraySections.map((section) => detail.content[section].length > 0 && <KnowledgeSection key={section} title={sectionLabels[section]} items={detail.content[section]} protectedSection={detail.protected_sections.includes(section)} />)}
          </>
        )}
      </section>
      <KnowledgeSummarySources summaryId={detail.summary_id} onOpenChat={onOpenChat} />
    </div>
  );
}

function EditableOverview({
  value,
  protectedSection,
  unlockRequested,
  onChange,
  onUnlock,
}: {
  value: string;
  protectedSection: boolean;
  unlockRequested: boolean;
  onChange: (value: string) => void;
  onUnlock: () => void;
}) {
  return (
    <label className="editor-label">
      <div className="editor-section-head">
        <span>概览</span>
        {protectedSection && (
          <button className="link-btn" type="button" onClick={onUnlock}>
            {unlockRequested ? <Unlock size={13} /> : <Lock size={13} />}
            {unlockRequested ? "取消允许 AI 更新" : "允许 AI 继续更新"}
          </button>
        )}
      </div>
      <textarea value={value} onChange={(event) => onChange(event.target.value)} />
    </label>
  );
}

function EditableSection({
  section,
  items,
  protectedSection,
  unlockRequested,
  onChange,
  onUnlock,
}: {
  section: KnowledgeSummaryArraySection;
  items: DraftItem[];
  protectedSection: boolean;
  unlockRequested: boolean;
  onChange: (items: DraftItem[]) => void;
  onUnlock: () => void;
}) {
  return (
    <div className="editor-label">
      <div className="editor-section-head">
        <span>{sectionLabels[section]}</span>
        {protectedSection && (
          <button className="link-btn" type="button" onClick={onUnlock}>
            {unlockRequested ? <Unlock size={13} /> : <Lock size={13} />}
            {unlockRequested ? "取消允许 AI 更新" : "允许 AI 继续更新"}
          </button>
        )}
      </div>
      <div className="editor-items">
        {items.map((item, index) => (
          <div className="editor-item-row" key={item.item_id ?? `new-${index}`}>
            <input
              value={item.text}
              onChange={(event) => {
                const next = items.slice();
                next[index] = { ...item, text: event.target.value };
                onChange(next);
              }}
              placeholder="输入一个条目"
            />
            <button
              className="icon-btn danger"
              type="button"
              aria-label={`删除${sectionLabels[section]}条目`}
              title="删除条目"
              onClick={() => onChange(items.filter((_, itemIndex) => itemIndex !== index))}
            >
              <X size={14} />
            </button>
          </div>
        ))}
      </div>
      <button
        className="link-btn"
        type="button"
        onClick={() => onChange([...items, { item_id: null, text: "" }])}
      >
        <Plus size={13} /> 添加条目
      </button>
    </div>
  );
}

function KnowledgeSection({ title, items, protectedSection }: { title: string; items: KnowledgeSummaryItem[]; protectedSection: boolean }) {
  return <section className="knowledge-section"><div className="knowledge-section-title"><h4>{title}</h4>{protectedSection ? <Lock size={13} aria-label="由你维护" /> : <Unlock size={13} aria-label="AI 可更新" />}</div><ul>{items.map((item) => <li key={item.item_id}>{item.text}</li>)}</ul></section>;
}

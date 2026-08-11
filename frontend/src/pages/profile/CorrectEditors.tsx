// 记忆纠正内联编辑器（规格 §20.1）：携带 expected_version 的受限结构化替换。
// 只允许规格公开字段（§6.2），不支持任意 Markdown / Patch。
import { useState } from "react";
import type {
  LearnerMemoryView,
  LearnerReplacement,
  MasteryMemoryView,
  MasteryReplacement,
} from "../../api/memory";

interface CommonProps {
  pending: boolean;
  onCancel: () => void;
}

interface LearnerEditorProps extends CommonProps {
  memory: LearnerMemoryView;
  onSubmit: (replacement: LearnerReplacement, reason?: string) => void;
}

interface MasteryEditorProps extends CommonProps {
  memory: MasteryMemoryView;
  onSubmit: (replacement: MasteryReplacement, reason?: string) => void;
}

function linesToList(text: string): string[] {
  return text
    .split("\n")
    .map((line) => line.trim())
    .filter((line) => line.length > 0);
}

function ListField({
  label,
  value,
  onChange,
}: {
  label: string;
  value: string;
  onChange: (value: string) => void;
}) {
  return (
    <label className="editor-field">
      <span className="editor-label">{label}（每行一条）</span>
      <textarea rows={3} value={value} onChange={(e) => onChange(e.target.value)} />
    </label>
  );
}

export function LearnerCorrectEditor({ memory, pending, onSubmit, onCancel }: LearnerEditorProps) {
  const [preferences, setPreferences] = useState(memory.preferences.join("\n"));
  const [goals, setGoals] = useState(memory.goals.join("\n"));
  const [plans, setPlans] = useState(memory.plans.join("\n"));
  const [reason, setReason] = useState("");
  return (
    <div className="memory-editor">
      <ListField label="学习偏好" value={preferences} onChange={setPreferences} />
      <ListField label="学习目标" value={goals} onChange={setGoals} />
      <ListField label="当前计划" value={plans} onChange={setPlans} />
      <label className="editor-field">
        <span className="editor-label">纠正原因（可选）</span>
        <input value={reason} onChange={(e) => setReason(e.target.value)} maxLength={500} />
      </label>
      <div className="editor-actions">
        <button
          className="btn btn-red"
          disabled={pending}
          onClick={() =>
            onSubmit(
              {
                replacement_type: "learner",
                preferences: linesToList(preferences),
                goals: linesToList(goals),
                plans: linesToList(plans),
              },
              reason || undefined,
            )
          }
        >
          提交纠正
        </button>
        <button className="btn btn-ghost" disabled={pending} onClick={onCancel}>
          取消
        </button>
      </div>
    </div>
  );
}

export function MasteryCorrectEditor({ memory, pending, onSubmit, onCancel }: MasteryEditorProps) {
  const [topicTitle, setTopicTitle] = useState(memory.topic_title);
  const [overview, setOverview] = useState(memory.overview);
  const [understood, setUnderstood] = useState(memory.understood.join("\n"));
  const [difficulties, setDifficulties] = useState(memory.difficulties.join("\n"));
  const [reviewAdvice, setReviewAdvice] = useState(memory.review_advice.join("\n"));
  const [reason, setReason] = useState("");
  return (
    <div className="memory-editor">
      <label className="editor-field">
        <span className="editor-label">主题名称</span>
        <input
          value={topicTitle}
          onChange={(e) => setTopicTitle(e.target.value)}
          maxLength={120}
        />
      </label>
      <label className="editor-field">
        <span className="editor-label">掌握概况</span>
        <textarea
          rows={3}
          value={overview}
          onChange={(e) => setOverview(e.target.value)}
          maxLength={1200}
        />
      </label>
      <ListField label="已掌握" value={understood} onChange={setUnderstood} />
      <ListField label="仍有困难" value={difficulties} onChange={setDifficulties} />
      <ListField label="建议复习" value={reviewAdvice} onChange={setReviewAdvice} />
      <label className="editor-field">
        <span className="editor-label">纠正原因（可选）</span>
        <input value={reason} onChange={(e) => setReason(e.target.value)} maxLength={500} />
      </label>
      <div className="editor-actions">
        <button
          className="btn btn-red"
          disabled={pending || topicTitle.trim().length === 0}
          onClick={() =>
            onSubmit(
              {
                replacement_type: "mastery",
                topic_title: topicTitle.trim(),
                overview,
                understood: linesToList(understood),
                difficulties: linesToList(difficulties),
                review_advice: linesToList(reviewAdvice),
                evidence_refs: memory.evidence_refs,
              },
              reason || undefined,
            )
          }
        >
          提交纠正
        </button>
        <button className="btn btn-ghost" disabled={pending} onClick={onCancel}>
          取消
        </button>
      </div>
    </div>
  );
}

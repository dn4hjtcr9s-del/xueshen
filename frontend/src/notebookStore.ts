// 错题本前端本地存储：在真实错题收藏 API 接入前，让“存入错题本 →
// 筛选 → 间隔重复复习”的交互可以完整跑通。数据保存在浏览器 localStorage，
// 新用户默认为空，不写入任何示例错题。
import type { NoteItem } from "./data";

const STORAGE_KEY = "gewu-math-notebook-v1";

function makeId(): string {
  if (typeof crypto !== "undefined" && "randomUUID" in crypto) {
    return crypto.randomUUID();
  }
  return `note-${Date.now()}-${Math.random().toString(16).slice(2)}`;
}

function formatAddedAt(date: Date): string {
  return `${date.getMonth() + 1} 月 ${date.getDate()} 日`;
}

function isNoteItem(value: unknown): value is NoteItem {
  if (typeof value !== "object" || value === null) return false;
  const note = value as Record<string, unknown>;
  return (
    typeof note.id === "string" &&
    typeof note.question === "string" &&
    typeof note.answerExcerpt === "string" &&
    Array.isArray(note.tags) &&
    note.tags.every((tag) => typeof tag === "string") &&
    typeof note.source === "string" &&
    typeof note.addedAt === "string" &&
    typeof note.nextReview === "string" &&
    typeof note.reviewStage === "number" &&
    (note.mastery === "薄弱" || note.mastery === "巩固中" || note.mastery === "已掌握")
  );
}

export function loadNotes(): NoteItem[] {
  if (typeof window === "undefined") return [];
  try {
    const raw = window.localStorage.getItem(STORAGE_KEY);
    if (!raw) return [];
    const parsed: unknown = JSON.parse(raw);
    if (!Array.isArray(parsed)) return [];
    return parsed.filter(isNoteItem);
  } catch {
    return [];
  }
}

function persist(notes: NoteItem[]): void {
  if (typeof window === "undefined") return;
  try {
    window.localStorage.setItem(STORAGE_KEY, JSON.stringify(notes));
  } catch {
    // 隐私模式或存储不可用时仅本次会话内不持久化，不阻断收藏交互。
  }
}

export function addNote(input: {
  question: string;
  answerExcerpt: string;
  source: string;
}): NoteItem | null {
  const question = input.question.trim();
  const answerExcerpt = input.answerExcerpt.trim().slice(0, 280);
  if (!question || !answerExcerpt) return null;

  const note: NoteItem = {
    id: makeId(),
    question,
    answerExcerpt,
    tags: [],
    source: input.source.trim() || "AI 对话",
    addedAt: formatAddedAt(new Date()),
    nextReview: "今天",
    reviewStage: 1,
    mastery: "薄弱",
  };
  persist([note, ...loadNotes()]);
  return note;
}

export function reviewNote(
  noteId: string,
  outcome: "again" | "good",
): NoteItem[] {
  const notes: NoteItem[] = loadNotes().map((note): NoteItem => {
    if (note.id !== noteId) return note;

    if (outcome === "again") {
      return {
        ...note,
        nextReview: "明天",
        reviewStage: 1,
        mastery: "薄弱",
      };
    }

    const nextStage = Math.min(note.reviewStage + 1, 5);
    const intervals: Record<number, string> = {
      2: "3 天后",
      3: "7 天后",
      4: "14 天后",
      5: "30 天后",
    };
    const mastery: NoteItem["mastery"] =
      nextStage >= 4 ? "已掌握" : nextStage >= 2 ? "巩固中" : "薄弱";
    return {
      ...note,
      nextReview: intervals[nextStage] ?? "3 天后",
      reviewStage: nextStage,
      mastery,
    };
  });
  persist(notes);
  return notes;
}

export function removeNote(noteId: string): NoteItem[] {
  const notes = loadNotes().filter((note) => note.id !== noteId);
  persist(notes);
  return notes;
}

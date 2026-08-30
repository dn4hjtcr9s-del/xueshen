// 发帖视图：标题 + textarea + 图片选择（≤3，objectURL 本地预览，可拖拽排序）。
// 上传交互（§九/D24）：发布时并行上传 → 全部成功才提交；失败项可重试/移除；
// 发帖失败保留文本与 attachment_ids（预览改用返回 URL），重试不重传；
// 移除已上传图不调后端删除（交 24h 孤儿清理）。
import { useEffect, useRef, useState } from "react";
import {
  ArrowLeft,
  GripVertical,
  ImagePlus,
  Loader2,
  RotateCcw,
  Trash2,
  UploadCloud,
} from "lucide-react";
import {
  createPost,
  uploadAttachment,
  type CommunityAttachmentUpload,
  type CommunityBoard,
} from "../../api/community";
import { communityErrorMessage } from "./format";

type PickedImage = {
  key: string;
  file?: File;
  /** 本地 objectURL 预览；上传成功后优先显示返回 URL */
  previewUrl?: string;
  uploaded?: CommunityAttachmentUpload;
  status: "local" | "uploading" | "uploaded" | "error";
  error?: string;
};

let pickedImageSeq = 0;
function nextPickedImageKey(): string {
  pickedImageSeq += 1;
  return `img-${Date.now()}-${pickedImageSeq}`;
}

export default function CreatePost({
  boardId,
  boards,
  onDone,
  onCancel,
  isLoggedIn,
  onLoginRequired,
}: {
  boardId?: string;
  boards: CommunityBoard[];
  onDone: () => void;
  onCancel: () => void;
  isLoggedIn: boolean;
  onLoginRequired: () => void;
}) {
  const initialBoardId =
    boardId ?? boards.find((b) => b.slug === "linear-algebra")?.board_id ?? boards[0]?.board_id ?? "";
  const [selectedBoardId, setSelectedBoardId] = useState(initialBoardId);
  const [title, setTitle] = useState("");
  const [body, setBody] = useState("");
  const [images, setImages] = useState<PickedImage[]>([]);
  const [error, setError] = useState<string | null>(null);
  const [submitting, setSubmitting] = useState(false);
  const [draggedIndex, setDraggedIndex] = useState<number | null>(null);
  const controllersRef = useRef<Set<AbortController>>(new Set());
  const objectUrlsRef = useRef<string[]>([]);
  // 上传结果按 key 暂存：attachment_ids 必须按当前图片顺序（拖拽排序后顺序敏感，= position 序），
  // 不能用"已上传 + 新上传"拼接，否则重排后顺序错乱
  const uploadResultsRef = useRef<Map<string, CommunityAttachmentUpload>>(new Map());

  useEffect(() => {
    const controllers = controllersRef.current;
    const objectUrls = objectUrlsRef.current;
    return () => {
      controllers.forEach((controller) => controller.abort());
      controllers.clear();
      objectUrls.forEach((url) => URL.revokeObjectURL(url));
    };
  }, []);

  useEffect(() => {
    if (!selectedBoardId) {
      setSelectedBoardId(boards[0]?.board_id ?? "");
    }
  }, [boards, selectedBoardId]);

  const addFiles = (files: File[]) => {
    const room = 3 - images.length;
    if (room <= 0) return;
    const accepted = files.slice(0, room);
    const next = accepted.map<PickedImage>((file) => {
      const previewUrl = URL.createObjectURL(file);
      objectUrlsRef.current.push(previewUrl);
      return { key: nextPickedImageKey(), file, previewUrl, status: "local" };
    });
    setImages((prev) => [...prev, ...next]);
  };

  const removeImage = (key: string) => {
    uploadResultsRef.current.delete(key);
    setImages((prev) => {
      const target = prev.find((item) => item.key === key);
      if (target?.previewUrl) URL.revokeObjectURL(target.previewUrl);
      return prev.filter((item) => item.key !== key);
    });
  };

  const retryUpload = (key: string) => {
    const target = images.find((item) => item.key === key);
    if (!target?.file) return;
    setImages((prev) =>
      prev.map((item) => (item.key === key ? { ...item, status: "uploading", error: undefined } : item)),
    );
    void uploadOne(target.key, target.file);
  };

  const uploadOne = async (key: string, file: File): Promise<CommunityAttachmentUpload | null> => {
    const controller = new AbortController();
    controllersRef.current.add(controller);
    try {
      const uploaded = await uploadAttachment(file, controller.signal);
      uploadResultsRef.current.set(key, uploaded);
      setImages((prev) =>
        prev.map((item) =>
          item.key === key ? { ...item, uploaded, status: "uploaded", error: undefined } : item,
        ),
      );
      return uploaded;
    } catch (e) {
      if (controller.signal.aborted) return null;
      uploadResultsRef.current.delete(key);
      setImages((prev) =>
        prev.map((item) =>
          item.key === key
            ? { ...item, status: "error", error: communityErrorMessage(e, "图片上传失败") }
            : item,
        ),
      );
      return null;
    } finally {
      controllersRef.current.delete(controller);
    }
  };

  const uploadAll = async (): Promise<CommunityAttachmentUpload[] | null> => {
    const snapshot = images;
    const pending = snapshot.filter((item) => item.status === "local" || item.status === "error");
    if (pending.length > 0) {
      setImages((prev) =>
        prev.map((item) =>
          item.status === "local" || item.status === "error"
            ? { ...item, status: "uploading", error: undefined }
            : item,
        ),
      );
      // 并行上传（§九/D24）；单图失败不阻塞其他图片
      await Promise.allSettled(
        pending.map((item) => (item.file ? uploadOne(item.key, item.file) : Promise.resolve(null))),
      );
    }
    // 按点发布时的图片顺序取结果（snapshot 即展示顺序；uploadOne 已把新结果写入 ref）
    const ordered = snapshot.map((item) => item.uploaded ?? uploadResultsRef.current.get(item.key));
    if (ordered.some((u) => !u)) return null;
    return ordered as CommunityAttachmentUpload[];
  };

  const submit = async () => {
    if (!isLoggedIn) {
      onLoginRequired();
      return;
    }
    const board = selectedBoardId;
    const trimmedTitle = title.trim();
    const trimmedBody = body.trim();
    if (!board || !trimmedTitle || !trimmedBody) return;
    if (images.some((item) => item.status === "uploading")) return;
    setSubmitting(true);
    setError(null);
    try {
      const uploaded = await uploadAll();
      if (uploaded === null) {
        setError("部分图片上传失败，请重试或移除失败图片后再发布");
        setSubmitting(false);
        return;
      }
      const payload: {
        board_id: string;
        title: string;
        body: string;
        attachment_ids?: string[];
      } = { board_id: board, title: trimmedTitle, body: trimmedBody };
      if (uploaded.length > 0) payload.attachment_ids = uploaded.map((item) => item.attachment_id);
      await createPost(payload);
      onDone();
    } catch (e) {
      if (e instanceof Error && e.name === "AbortError") return;
      setError(communityErrorMessage(e, "发布失败"));
      setSubmitting(false);
    }
  };

  const busy = submitting || images.some((item) => item.status === "uploading");
  const canSubmit =
    isLoggedIn &&
    Boolean(selectedBoardId) &&
    Boolean(title.trim()) &&
    Boolean(body.trim()) &&
    !busy &&
    !images.some((item) => item.status === "error");

  return (
    <div className="rise">
      <button className="comm-back" onClick={onCancel}>
        <ArrowLeft size={14} /> 返回
      </button>

      <div className="card comm-compose">
        <div className="comm-compose-title">发起讨论</div>

        <label className="comm-label">
          选择板块
          <select
            className="comm-input"
            value={selectedBoardId}
            onChange={(e) => setSelectedBoardId(e.target.value)}
          >
            {boards.map((b) => (
              <option key={b.board_id} value={b.board_id}>
                {b.name}
              </option>
            ))}
          </select>
        </label>

        <label className="comm-label">
          标题
          <input
            className="comm-input"
            placeholder="标题（1–200 字符）"
            value={title}
            maxLength={200}
            onChange={(e) => setTitle(e.target.value)}
          />
        </label>

        <label className="comm-label">
          正文
          <textarea
            className="comm-input comm-textarea"
            placeholder="正文（纯文本，保留换行）"
            value={body}
            onChange={(e) => setBody(e.target.value)}
          />
        </label>

        <div className="comm-label">配图（最多 3 张，jpeg/png/webp，单图 ≤ 5MiB）</div>
        <div className="img-picker">
          <div className="img-preview-grid">
            {images.map((image, index) => (
              <div
                key={image.key}
                className={`img-preview ${image.status === "error" ? "error" : ""}`}
                draggable
                onDragStart={() => setDraggedIndex(index)}
                onDragOver={(e) => {
                  e.preventDefault();
                  if (draggedIndex === null || draggedIndex === index) return;
                  setImages((prev) => {
                    const next = [...prev];
                    const [moved] = next.splice(draggedIndex, 1);
                    next.splice(index, 0, moved);
                    return next;
                  });
                  setDraggedIndex(index);
                }}
                onDragEnd={() => setDraggedIndex(null)}
              >
                <img
                  src={image.uploaded?.url ?? image.previewUrl ?? ""}
                  alt={image.file?.name ?? image.uploaded?.mime ?? "图片"}
                  style={{ maxWidth: "100%", height: "auto" }}
                />
                <div className="img-preview-actions">
                  <GripVertical size={12} />
                  <span className="img-preview-status">
                    {image.status === "uploading" && <Loader2 className="spin" size={12} />}
                    {image.status === "uploaded" && "已上传"}
                    {image.status === "error" && "上传失败"}
                    {image.status === "local" && "待上传"}
                  </span>
                  {image.status === "error" && image.file && (
                    <button
                      type="button"
                      className="link-btn"
                      onClick={() => retryUpload(image.key)}
                    >
                      <RotateCcw size={12} /> 重试
                    </button>
                  )}
                  <button
                    type="button"
                    className="link-btn"
                    onClick={() => removeImage(image.key)}
                  >
                    <Trash2 size={12} /> 移除
                  </button>
                </div>
                {image.status === "error" && image.error && (
                  <div className="img-preview-error">{image.error}</div>
                )}
              </div>
            ))}
          </div>

          {images.length < 3 && (
            <label className="img-picker-add">
              <ImagePlus size={14} /> 选择图片
              <input
                type="file"
                accept="image/jpeg,image/png,image/webp"
                multiple
                style={{ display: "none" }}
                onChange={(e) => {
                  const files = Array.from(e.target.files ?? []);
                  if (files.length > 0) addFiles(files);
                  e.target.value = "";
                }}
              />
            </label>
          )}
          {images.length >= 3 && <span className="comm-hint">已达 3 张上限</span>}
        </div>

        {error && <div className="comm-error">{error}</div>}

        <div className="comm-compose-actions">
          <button className="btn btn-ghost" onClick={onCancel}>
            取消
          </button>
          <button className="btn btn-primary" disabled={!canSubmit} onClick={() => void submit()}>
            {busy ? (
              <>
                <Loader2 className="spin" size={13} /> 发布中…
              </>
            ) : (
              <>
                <UploadCloud size={13} /> 发布
              </>
            )}
          </button>
        </div>
        <p className="comm-memory-hint">
          你的发言可能用于更新你的个人学习记忆；记忆仅对你可见，可在个人中心管理。
        </p>
      </div>
    </div>
  );
}

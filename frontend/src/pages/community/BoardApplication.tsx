// 申请建吧：上半申请表单；下半我的申请列表（§九 申请建吧）。
import { useCallback, useEffect, useRef, useState } from "react";
import { ArrowLeft, Loader2 } from "lucide-react";
import {
  createBoardApplication,
  listMyBoardApplications,
  type CommunityBoardApplication,
} from "../../api/community";
import { communityErrorMessage, relativeTime } from "./format";

const STATUS_LABEL: Record<CommunityBoardApplication["status"], string> = {
  pending: "审核中",
  approved: "已通过",
  rejected: "已拒绝",
};

const STATUS_CLASS: Record<CommunityBoardApplication["status"], string> = {
  pending: "gold",
  approved: "green",
  rejected: "red",
};

export default function BoardApplication({
  onBack,
  isLoggedIn,
  onLoginRequired,
}: {
  onBack: () => void;
  isLoggedIn: boolean;
  onLoginRequired: () => void;
}) {
  const [name, setName] = useState("");
  const [slug, setSlug] = useState("");
  const [description, setDescription] = useState("");
  const [reason, setReason] = useState("");
  const [formError, setFormError] = useState<string | null>(null);
  const [submitting, setSubmitting] = useState(false);
  const [applications, setApplications] = useState<CommunityBoardApplication[]>([]);
  const [nextCursor, setNextCursor] = useState<string | null>(null);
  const [hasMore, setHasMore] = useState(false);
  const [loading, setLoading] = useState(true);
  const [loadingMore, setLoadingMore] = useState(false);
  const [listError, setListError] = useState<string | null>(null);
  const requestSeq = useRef(0);

  const load = useCallback(async (cursor: string | null, append: boolean) => {
    const requestId = requestSeq.current + 1;
    requestSeq.current = requestId;
    if (append) setLoadingMore(true);
    else setLoading(true);
    setListError(null);
    try {
      const page = await listMyBoardApplications({ cursor: cursor ?? undefined, limit: 20 });
      if (requestId !== requestSeq.current) return;
      setApplications((prev) => (append ? [...prev, ...page.items] : page.items));
      setNextCursor(page.next_cursor);
      setHasMore(page.has_more);
    } catch (e) {
      if (requestId !== requestSeq.current) return;
      setListError(communityErrorMessage(e, "申请列表加载失败"));
    } finally {
      if (requestId === requestSeq.current) {
        setLoading(false);
        setLoadingMore(false);
      }
    }
  }, []);

  useEffect(() => {
    void load(null, false);
  }, [load]);

  if (!isLoggedIn) {
    return (
      <div className="comm-empty">
        <p>登录后才能申请建吧</p>
        <button className="btn btn-primary" onClick={onLoginRequired}>
          去登录
        </button>
      </div>
    );
  }

  const submit = async () => {
    const trimmedName = name.trim();
    const trimmedSlug = slug.trim().toLowerCase();
    const trimmedDescription = description.trim();
    const trimmedReason = reason.trim();
    if (!trimmedName || !trimmedSlug || !trimmedReason) {
      setFormError("请填写吧名、标识与申请理由");
      return;
    }
    setSubmitting(true);
    setFormError(null);
    try {
      await createBoardApplication({
        name: trimmedName,
        slug: trimmedSlug,
        description: trimmedDescription,
        reason: trimmedReason,
      });
      setName("");
      setSlug("");
      setDescription("");
      setReason("");
      void load(null, false);
    } catch (e) {
      setFormError(communityErrorMessage(e, "申请提交失败"));
    } finally {
      setSubmitting(false);
    }
  };

  return (
    <div className="rise">
      <button className="comm-back" onClick={onBack}>
        <ArrowLeft size={14} /> 返回社区首页
      </button>

      <section className="card comm-compose">
        <div className="comm-compose-title">申请建吧</div>
        <div className="comm-form">
          <label className="comm-label">
            吧名（1–20 字符）
            <input
              className="comm-input"
              value={name}
              maxLength={20}
              placeholder="例如：数学分析"
              onChange={(e) => setName(e.target.value)}
            />
          </label>
          <label className="comm-label">
            标识 slug（2–30 位小写字母/数字，中间可单个连字符）
            <input
              className="comm-input"
              value={slug}
              maxLength={30}
              placeholder="例如：math-analysis"
              onChange={(e) => setSlug(e.target.value)}
            />
          </label>
          <label className="comm-label">
            简介（可留空，≤100 字符）
            <input
              className="comm-input"
              value={description}
              maxLength={100}
              placeholder="一句话介绍这个板块"
              onChange={(e) => setDescription(e.target.value)}
            />
          </label>
          <label className="comm-label">
            申请理由（1–500 字符）
            <textarea
              className="comm-input comm-textarea"
              value={reason}
              maxLength={500}
              placeholder="为什么想建立这个板块？计划如何交流？"
              onChange={(e) => setReason(e.target.value)}
            />
          </label>
          {formError && <div className="comm-error">{formError}</div>}
          <div className="comm-compose-actions">
            <button className="btn btn-primary" disabled={submitting} onClick={() => void submit()}>
              {submitting ? "提交中…" : "提交申请"}
            </button>
          </div>
        </div>
      </section>

      <section className="comm-section">
        <div className="comm-section-head">
          <div>
            <span className="sec-num">01</span>
            <span className="section-title">我的申请</span>
          </div>
        </div>

        {loading && (
          <div className="comm-detail-loading">
            <Loader2 className="spin" size={20} />
          </div>
        )}

        {listError && applications.length === 0 && (
          <div className="comm-empty">
            <p>{listError}</p>
            <button className="btn btn-primary" onClick={() => void load(null, false)}>
              重试
            </button>
          </div>
        )}

        {!loading && !listError && applications.length === 0 && (
          <div className="comm-empty">
            <p>还没有申请记录</p>
          </div>
        )}

        {applications.length > 0 && (
          <div className="app-list">
            {applications.map((app) => (
              <div key={app.application_id} className="app-item">
                <div className="app-item-main">
                  <div className="app-item-title">
                    {app.name}
                    <span className={`tag ${STATUS_CLASS[app.status]}`}>
                      {STATUS_LABEL[app.status]}
                    </span>
                  </div>
                  <div className="app-item-meta">
                    <span>/{app.slug}</span>
                    <span>{relativeTime(app.created_at)}</span>
                  </div>
                  {app.reject_reason && (
                    <div className="app-item-reject">拒绝理由：{app.reject_reason}</div>
                  )}
                </div>
              </div>
            ))}
          </div>
        )}

        {hasMore && (
          <div className="comm-loadmore">
            <button
              className="btn btn-ghost"
              disabled={loadingMore}
              onClick={() => void load(nextCursor, true)}
            >
              {loadingMore ? "加载中…" : "加载更多"}
            </button>
          </div>
        )}
      </section>
    </div>
  );
}

// 管理员审核：审核列表 + approve/reject（§九 管理员审核）。
// 仅 permissions.is_community_admin 为 true 时由导航层渲染入口；
// 已登录非管理员直达本视图时显示 403 提示卡（§九 登录与权限行为③）。
import { useCallback, useEffect, useRef, useState } from "react";
import { ArrowLeft, Loader2, ShieldCheck } from "lucide-react";
import {
  approveBoardApplication,
  listAdminBoardApplications,
  rejectBoardApplication,
  type CommunityBoardApplication,
} from "../../api/community";
import { communityErrorMessage, relativeTime } from "./format";

type StatusFilter = "pending" | "approved" | "rejected" | "all";

const FILTERS: { key: StatusFilter; label: string }[] = [
  { key: "pending", label: "待审核" },
  { key: "approved", label: "已通过" },
  { key: "rejected", label: "已拒绝" },
  { key: "all", label: "全部" },
];

const STATUS_LABEL: Record<CommunityBoardApplication["status"], string> = {
  pending: "审核中",
  approved: "已通过",
  rejected: "已拒绝",
};

export default function AdminApplications({
  onBack,
  isAdmin,
}: {
  onBack: () => void;
  isAdmin: boolean;
}) {
  const [status, setStatus] = useState<StatusFilter>("pending");
  const [applications, setApplications] = useState<CommunityBoardApplication[]>([]);
  const [nextCursor, setNextCursor] = useState<string | null>(null);
  const [hasMore, setHasMore] = useState(false);
  const [loading, setLoading] = useState(true);
  const [loadingMore, setLoadingMore] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [busyId, setBusyId] = useState<string | null>(null);
  const [rejectReasons, setRejectReasons] = useState<Record<string, string>>({});
  const requestSeq = useRef(0);

  const load = useCallback(
    async (cursor: string | null, append: boolean) => {
      const requestId = requestSeq.current + 1;
      requestSeq.current = requestId;
      if (append) setLoadingMore(true);
      else setLoading(true);
      setError(null);
      try {
        const page = await listAdminBoardApplications({
          status,
          cursor: cursor ?? undefined,
          limit: 20,
        });
        if (requestId !== requestSeq.current) return;
        setApplications((prev) => (append ? [...prev, ...page.items] : page.items));
        setNextCursor(page.next_cursor);
        setHasMore(page.has_more);
      } catch (e) {
        if (requestId !== requestSeq.current) return;
        setError(communityErrorMessage(e, "审核列表加载失败"));
      } finally {
        if (requestId === requestSeq.current) {
          setLoading(false);
          setLoadingMore(false);
        }
      }
    },
    [status],
  );

  useEffect(() => {
    setApplications([]);
    setNextCursor(null);
    setHasMore(false);
    void load(null, false);
  }, [load]);

  if (!isAdmin) {
    return (
      <div className="comm-empty">
        <p>403 · 需要社区管理员权限</p>
        <button className="btn btn-primary" onClick={onBack}>
          返回社区
        </button>
      </div>
    );
  }

  const act = async (id: string, fn: () => Promise<unknown>) => {
    setBusyId(id);
    setError(null);
    try {
      await fn();
      await load(null, false);
    } catch (e) {
      setError(communityErrorMessage(e, "审核操作失败"));
    } finally {
      setBusyId(null);
    }
  };

  return (
    <div className="rise">
      <button className="comm-back" onClick={onBack}>
        <ArrowLeft size={14} /> 返回社区首页
      </button>

      <div className="comm-section-head">
        <div>
          <span className="sec-num">01</span>
          <span className="section-title">建吧审核</span>
        </div>
        <span className="tag green">
          <ShieldCheck size={12} style={{ verticalAlign: -2 }} /> 管理员
        </span>
      </div>

      <div className="comm-toolbar">
        <div className="comm-filters">
          {FILTERS.map((f) => (
            <button
              key={f.key}
              className={`comm-chip ${status === f.key ? "active" : ""}`}
              onClick={() => setStatus(f.key)}
            >
              {f.label}
            </button>
          ))}
        </div>
      </div>

      {error && (
        <div className="comm-error-inline" role="alert">
          <span>{error}</span>
        </div>
      )}

      {loading && applications.length === 0 && (
        <div className="comm-detail-loading">
          <Loader2 className="spin" size={20} />
        </div>
      )}

      {!loading && !error && applications.length === 0 && (
        <div className="comm-empty">
          <p>暂无申请</p>
        </div>
      )}

      {applications.length > 0 && (
        <div className="app-list">
          {applications.map((app) => (
            <div key={app.application_id} className="app-item">
              <div className="app-item-main">
                <div className="app-item-title">
                  {app.name}
                  <span className="tag">{STATUS_LABEL[app.status]}</span>
                </div>
                <div className="app-item-meta">
                  <span>/{app.slug}</span>
                  <span>{app.reason}</span>
                  <span>{relativeTime(app.created_at)}</span>
                </div>
                {app.description && (
                  <div className="app-item-desc">简介：{app.description}</div>
                )}
                {app.reject_reason && (
                  <div className="app-item-reject">拒绝理由：{app.reject_reason}</div>
                )}
              </div>
              {app.status === "pending" && (
                <div className="app-item-actions">
                  <input
                    className="comm-input"
                    placeholder="拒绝理由（1–200 字符）"
                    value={rejectReasons[app.application_id] ?? ""}
                    maxLength={200}
                    onChange={(e) =>
                      setRejectReasons((prev) => ({
                        ...prev,
                        [app.application_id]: e.target.value,
                      }))
                    }
                  />
                  <button
                    className="btn btn-primary"
                    disabled={busyId === app.application_id}
                    onClick={() => void act(app.application_id, () => approveBoardApplication(app.application_id))}
                  >
                    {busyId === app.application_id ? "处理中…" : "通过"}
                  </button>
                  <button
                    className="btn btn-red"
                    disabled={busyId === app.application_id}
                    onClick={() => {
                      const reason = (rejectReasons[app.application_id] ?? "").trim();
                      if (!reason) {
                        setError("拒绝理由不能为空");
                        return;
                      }
                      void act(app.application_id, () =>
                        rejectBoardApplication(app.application_id, reason),
                      );
                    }}
                  >
                    拒绝
                  </button>
                </div>
              )}
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
    </div>
  );
}

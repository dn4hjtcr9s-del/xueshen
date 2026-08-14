// 社区页：讨论区（真实 API）/ 学习小组 / 打卡圈。
// PR-B 只读纵切 + PR-C 写纵切：发帖/回复/点赞/解决/删除；targetPostId 支持
// 通知点击跳转（§6.5）；学习小组/打卡圈保留占位（PR-E 统一"即将开放"）。
import { useCallback, useEffect, useRef, useState } from "react";
import {
  ArrowLeft,
  CheckCircle2,
  Heart,
  Loader2,
  MessageSquare,
  Pin,
  Send,
  Trash2,
} from "lucide-react";
import {
  createPost,
  createReply,
  deletePost,
  deleteReply,
  getPostDetail,
  likePost,
  listBoards,
  listPosts,
  resolvePost,
  unlikePost,
  type CommunityBoard,
  type CommunityPostDetail,
  type CommunityPostSummary,
  type CommunityReplyView,
} from "../api/community";
import { checkin, studyGroups } from "../data";

const TABS = [
  { key: "讨论区" as const },
  { key: "学习小组" as const },
  { key: "打卡圈" as const },
];

type TabKey = (typeof TABS)[number]["key"];

// ---------------------------------------------------------------------------
// 小组件
// ---------------------------------------------------------------------------

function RelativeTime(iso: string): string {
  const then = new Date(iso).getTime();
  const diff = Date.now() - then;
  if (Number.isNaN(then)) return "";
  const minute = 60_000;
  const hour = 60 * minute;
  const day = 24 * hour;
  if (diff < minute) return "刚刚";
  if (diff < hour) return `${Math.floor(diff / minute)} 分钟前`;
  if (diff < day) return `${Math.floor(diff / hour)} 小时前`;
  if (diff < 30 * day) return `${Math.floor(diff / day)} 天前`;
  return new Date(iso).toLocaleDateString("zh-CN");
}

function PostRow({
  post,
  index,
  onClick,
}: {
  post: CommunityPostSummary;
  index: number;
  onClick: () => void;
}) {
  return (
    <div className="post-row" onClick={onClick} style={{ cursor: "pointer" }}>
      <span className="post-idx">{String(index + 1).padStart(2, "0")}</span>
      <div className="post-main">
        <div className="post-title">
          {post.pinned && <Pin size={13} className="pin" />}
          {post.solved && (
            <CheckCircle2
              size={14}
              color="var(--pine)"
              style={{ marginRight: 6, verticalAlign: -2 }}
            />
          )}
          {post.title}
        </div>
        <div className="post-meta">
          <span className="tag">{post.board.name}</span>
          <span>{post.author.display_name}</span>
          <span>{RelativeTime(post.last_activity_at)}</span>
        </div>
      </div>
      <div className="post-stats">
        <span>
          <MessageSquare size={11} style={{ verticalAlign: -1 }} /> {post.reply_count}
        </span>
        <span>
          <Heart size={11} style={{ verticalAlign: -1 }} /> {post.like_count}
        </span>
      </div>
    </div>
  );
}

function SkeletonRows() {
  return (
    <div className="comm-skeleton">
      {[0, 1, 2, 3].map((i) => (
        <div key={i} className="post-row">
          <span className="skeleton-line" style={{ width: 24 }} />
          <div className="post-main">
            <div className="skeleton-line" style={{ width: "55%" }} />
            <div className="skeleton-line" style={{ width: "30%", marginTop: 8 }} />
          </div>
        </div>
      ))}
    </div>
  );
}

// ---------------------------------------------------------------------------
// 发帖面板（§6.2：板块/标题/正文；"发布"按钮）
// ---------------------------------------------------------------------------

function ComposePanel({
  boards,
  onDone,
  onCancel,
}: {
  boards: CommunityBoard[];
  onDone: () => void;
  onCancel: () => void;
}) {
  const [boardId, setBoardId] = useState(boards[0]?.board_id ?? "");
  const [title, setTitle] = useState("");
  const [body, setBody] = useState("");
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const canSubmit = boardId && title.trim() && body.trim() && !submitting;

  const submit = async () => {
    if (!canSubmit) return;
    setSubmitting(true);
    setError(null);
    try {
      await createPost({ board_id: boardId, title: title.trim(), body: body.trim() });
      onDone();
    } catch (e) {
      setError(e instanceof Error ? e.message : "发布失败");
      setSubmitting(false);
    }
  };

  return (
    <div className="card comm-compose">
      <div className="comm-compose-title">发起讨论</div>
      <select
        className="comm-input"
        value={boardId}
        onChange={(e) => setBoardId(e.target.value)}
      >
        {boards.map((b) => (
          <option key={b.board_id} value={b.board_id}>
            {b.name}
          </option>
        ))}
      </select>
      <input
        className="comm-input"
        placeholder="标题（1–200 字符）"
        value={title}
        maxLength={200}
        onChange={(e) => setTitle(e.target.value)}
      />
      <textarea
        className="comm-input comm-textarea"
        placeholder="正文（纯文本，保留换行）"
        value={body}
        onChange={(e) => setBody(e.target.value)}
      />
      {error && <div className="comm-error">{error}</div>}
      <div className="comm-compose-actions">
        <button className="btn btn-ghost" onClick={onCancel}>取消</button>
        <button className="btn btn-primary" disabled={!canSubmit} onClick={() => void submit()}>
          {submitting ? "发布中…" : "发布"}
        </button>
      </div>
      <p className="comm-memory-hint">你的发言可能用于更新你的个人学习记忆；记忆仅对你可见，可在个人中心管理。</p>
    </div>
  );
}

// ---------------------------------------------------------------------------
// 讨论区列表（§6.1）
// ---------------------------------------------------------------------------

type Sort = "latest" | "unanswered";

function DiscussionList({ onOpenPost }: { onOpenPost: (postId: string) => void }) {
  const [boards, setBoards] = useState<CommunityBoard[]>([]);
  const [boardId, setBoardId] = useState<string>("");
  const [sort, setSort] = useState<Sort>("latest");
  const [posts, setPosts] = useState<CommunityPostSummary[]>([]);
  const [nextCursor, setNextCursor] = useState<string | null>(null);
  const [hasMore, setHasMore] = useState(false);
  const [loading, setLoading] = useState(true);
  const [loadingMore, setLoadingMore] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [composing, setComposing] = useState(false);

  const load = useCallback(
    async (cursor: string | null, append: boolean) => {
      if (append) setLoadingMore(true);
      else setLoading(true);
      setError(null);
      try {
        const page = await listPosts({
          board_id: boardId || undefined,
          sort,
          cursor: cursor ?? undefined,
          limit: 20,
        });
        setPosts((prev) => (append ? [...prev, ...page.items] : page.items));
        setNextCursor(page.next_cursor);
        setHasMore(page.has_more);
      } catch (e) {
        setError(e instanceof Error ? e.message : "加载失败");
      } finally {
        setLoading(false);
        setLoadingMore(false);
      }
    },
    [boardId, sort],
  );

  useEffect(() => {
    void load(null, false);
  }, [load]);

  useEffect(() => {
    void listBoards()
      .then(setBoards)
      .catch(() => setBoards([]));
  }, []);

  return (
    <div className="rise">
      <div className="comm-toolbar">
        <div className="comm-filters">
          <button
            className={`comm-chip ${boardId === "" ? "active" : ""}`}
            onClick={() => setBoardId("")}
          >
            全部
          </button>
          {boards.map((b) => (
            <button
              key={b.board_id}
              className={`comm-chip ${boardId === b.board_id ? "active" : ""}`}
              onClick={() => setBoardId(b.board_id)}
            >
              {b.name}
            </button>
          ))}
        </div>
        <div className="comm-sort">
          <button
            className={`comm-chip ${sort === "latest" ? "active" : ""}`}
            onClick={() => setSort("latest")}
          >
            最新
          </button>
          <button
            className={`comm-chip ${sort === "unanswered" ? "active" : ""}`}
            onClick={() => setSort("unanswered")}
          >
            未解决
          </button>
          <button
            className="btn btn-primary"
            style={{ padding: "5px 14px", fontSize: 12.5 }}
            onClick={() => setComposing(true)}
          >
            发起讨论
          </button>
        </div>
      </div>

      {composing && (
        <ComposePanel
          boards={boards}
          onCancel={() => setComposing(false)}
          onDone={() => {
            setComposing(false);
            void load(null, false);
          }}
        />
      )}

      {loading && <SkeletonRows />}

      {error && (
        <div className="comm-empty">
          <p>{error}</p>
          <button className="btn btn-primary" onClick={() => void load(null, false)}>
            重试
          </button>
        </div>
      )}

      {!loading && !error && posts.length === 0 && (
        <div className="comm-empty">
          <p>还没有帖子，来发起第一个讨论吧</p>
        </div>
      )}

      {!loading && !error && posts.length > 0 && (
        <div className="post-list">
          {posts.map((p, i) => (
            <PostRow key={p.post_id} post={p} index={i} onClick={() => onOpenPost(p.post_id)} />
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

// ---------------------------------------------------------------------------
// 帖子详情（§6.3：点赞/解决/删除/回复；写操作后刷新详情）
// ---------------------------------------------------------------------------

function DiscussionDetail({ postId, onBack }: { postId: string; onBack: () => void }) {
  const [post, setPost] = useState<CommunityPostDetail | null>(null);
  const [replies, setReplies] = useState<CommunityReplyView[]>([]);
  const [replyCursor, setReplyCursor] = useState<string | null>(null);
  const [replyHasMore, setReplyHasMore] = useState(false);
  const [replyBody, setReplyBody] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  // §6.6 冻结文案：删除确认使用自定义弹层（"确认删除/取消"），不用原生 confirm
  const [confirm, setConfirm] = useState<{ kind: "post" | "reply"; replyId?: string } | null>(
    null,
  );

  const load = useCallback(async (cursor?: string | null) => {
    try {
      // §8.4：回复分页（reply_cursor/reply_limit；追加模式保留已加载回复）
      const resp = await getPostDetail({ post_id: postId, reply_cursor: cursor ?? undefined });
      if (cursor) {
        setReplies((prev) => [...prev, ...resp.replies.items]);
      } else {
        setReplies(resp.replies.items);
      }
      setReplyCursor(resp.replies.next_cursor);
      setReplyHasMore(resp.replies.has_more);
      setPost(resp.post);
      setError(null);
    } catch (e) {
      setError(e instanceof Error ? e.message : "加载失败");
    }
  }, [postId]);

  useEffect(() => {
    setPost(null);
    setError(null);
    void load();
  }, [load]);

  const acting = async (fn: () => Promise<unknown>) => {
    if (busy) return;
    setBusy(true);
    try {
      await fn();
      await load();
    } catch (e) {
      setError(e instanceof Error ? e.message : "操作失败");
    } finally {
      setBusy(false);
    }
  };

  if (error) {
    return (
      <div className="comm-empty">
        <p>{error}</p>
        <button className="btn btn-primary" onClick={onBack}>返回讨论区</button>
      </div>
    );
  }
  if (!post) {
    return (
      <div className="comm-detail-loading">
        <Loader2 className="spin" size={20} />
      </div>
    );
  }

  const canInteract = !post.deleted && post.discussion_status === "open";

  return (
    <div className="rise">
      <button className="comm-back" onClick={onBack}>
        <ArrowLeft size={14} /> 返回讨论区
      </button>
      <article className="comm-detail">
        <div className="comm-detail-title">
          {post.deleted ? "该帖子已被作者删除" : post.title}
        </div>
        <div className="post-meta">
          <span className="tag">{post.board.name}</span>
          <span>{post.author.display_name}</span>
          <span>{RelativeTime(post.created_at)}</span>
          {post.solved && <span className="tag">已解决</span>}
          {!post.deleted && post.pinned && <span className="tag">置顶</span>}
        </div>
        {!post.deleted && <div className="comm-detail-body">{post.body}</div>}

        {!post.deleted && (
          <div className="comm-detail-actions">
            <button
              className={`comm-action ${post.viewer_liked ? "liked" : ""}`}
              onClick={() => void acting(() => (post.viewer_liked ? unlikePost(postId) : likePost(postId)))}
            >
              <Heart size={14} fill={post.viewer_liked ? "currentColor" : "none"} /> {post.like_count}
            </button>
            {post.viewer_is_author && (
              <button className="comm-action danger" onClick={() => setConfirm({ kind: "post" })}>
                <Trash2 size={14} /> 删除
              </button>
            )}
          </div>
        )}

        <div className="comm-replies-head">
          <MessageSquare size={13} /> {post.reply_count} 条回复
        </div>

        {replies.length === 0 && <div className="comm-empty">暂无回复，来写下第一条吧</div>}
        {replies.map((r) => (
          <div key={r.reply_id} className="comm-reply">
            <div className="post-meta">
              <span>{r.author.display_name}</span>
              <span>{RelativeTime(r.created_at)}</span>
              {r.solved && <span className="tag">解决答案</span>}
              {!r.deleted && post.viewer_is_author && canInteract && (
                <button
                  className="link-btn"
                  onClick={() => void acting(() => resolvePost(postId, r.solved ? null : r.reply_id))}
                >
                  {r.solved ? "取消解决" : "标记为解决"}
                </button>
              )}
              {!r.deleted && r.viewer_is_author && (
                <button
                  className="link-btn"
                  onClick={() => setConfirm({ kind: "reply", replyId: r.reply_id })}
                >
                  删除
                </button>
              )}
            </div>
            <div className="comm-reply-body">{r.deleted ? "内容已删除" : r.body}</div>
          </div>
        ))}

        {replyHasMore && (
          <div className="comm-loadmore">
            <button className="btn btn-ghost" onClick={() => void load(replyCursor)}>
              加载更多
            </button>
          </div>
        )}

        {canInteract && (
          <div className="comm-reply-box">
            <textarea
              className="comm-input comm-textarea"
              placeholder="写下你的回复…"
              value={replyBody}
              onChange={(e) => setReplyBody(e.target.value)}
            />
            <button
              className="btn btn-primary"
              disabled={!replyBody.trim() || busy}
              onClick={() =>
                void acting(async () => {
                  await createReply(postId, replyBody.trim());
                  setReplyBody("");
                })
              }
            >
              <Send size={13} /> 发布
            </button>
          </div>
        )}
      </article>

      {confirm && (
        <div className="comm-confirm-mask" onClick={() => setConfirm(null)}>
          <div className="comm-confirm" onClick={(e) => e.stopPropagation()}>
            <div className="comm-confirm-title">
              {confirm.kind === "post" ? "删除这条帖子？" : "删除这条回复？"}
            </div>
            <div className="comm-confirm-body">删除后正文不再展示，且不可恢复。</div>
            <div className="comm-confirm-actions">
              <button className="btn btn-ghost" onClick={() => setConfirm(null)}>
                取消
              </button>
              <button
                className="btn btn-primary"
                disabled={busy}
                onClick={() => {
                  setConfirm(null);
                  if (confirm.kind === "post") {
                    void acting(() => deletePost(postId)).then(() => onBack());
                  } else if (confirm.replyId) {
                    void acting(() => deleteReply(postId, confirm.replyId!));
                  }
                }}
              >
                确认删除
              </button>
            </div>
          </div>
        </div>
      )}
    </div>
  );
}

// ---------------------------------------------------------------------------
// 学习小组 / 打卡圈（占位；PR-E 统一改"即将开放"）
// ---------------------------------------------------------------------------

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
            <button
              className={`btn ${g.joined ? "btn-ghost" : "btn-primary"}`}
              style={{ padding: "6px 13px", fontSize: 12.5 }}
              disabled
            >
              即将开放
            </button>
          </div>
        </div>
      ))}
    </div>
  );
}

function CheckinTab() {
  return (
    <div className="checkin-layout rise">
      <div>
        <div className="cal-grid">
          {checkin.monthDays.map((hit, i) => (
            <div key={i} className={`cal-cell ${hit ? "hit" : ""} ${i === 3 ? "today" : ""}`}>
              {i + 1}
            </div>
          ))}
        </div>
      </div>
      <div>
        <div className="section-head" style={{ marginTop: 0 }}>
          <div className="section-title" style={{ fontSize: 16 }}>打卡圈</div>
          <div className="section-note">即将开放</div>
        </div>
      </div>
    </div>
  );
}

// ---------------------------------------------------------------------------
// 页面（列表 ↔ 详情 页面内状态切换；targetPostId 通知跳转，§6.5）
// ---------------------------------------------------------------------------

export function CommunityPage({
  targetPostId,
  onTargetConsumed,
}: {
  targetPostId?: string | null;
  onTargetConsumed?: () => void;
} = {}) {
  const [tab, setTab] = useState<TabKey>("讨论区");
  const [openPostId, setOpenPostId] = useState<string | null>(null);
  const consumedRef = useRef(false);

  // §6.5：Community 通知点击 → 先切到讨论区 Tab，再打开详情；
  // 详情成功打开或用户关闭后清空 target
  useEffect(() => {
    if (targetPostId) {
      setTab("讨论区");
      setOpenPostId(targetPostId);
      consumedRef.current = true;
      onTargetConsumed?.();
    }
  }, [targetPostId, onTargetConsumed]);

  return (
    <>
      <div className="comm-tabs rise">
        {TABS.map((t) => (
          <button
            key={t.key}
            className={`comm-tab ${tab === t.key ? "active" : ""}`}
            onClick={() => {
              setTab(t.key);
              setOpenPostId(null);
            }}
          >
            {t.key}
          </button>
        ))}
      </div>
      {tab === "讨论区" &&
        (openPostId ? (
          <DiscussionDetail postId={openPostId} onBack={() => setOpenPostId(null)} />
        ) : (
          <DiscussionList onOpenPost={setOpenPostId} />
        ))}
      {tab === "学习小组" && <GroupsTab />}
      {tab === "打卡圈" && <CheckinTab />}
    </>
  );
}

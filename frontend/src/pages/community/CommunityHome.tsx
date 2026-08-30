// 社区首页：板块宫格 + 最新帖子流（§九 社区首页）。
// 布局参照 bbs-go 首页板块宫格 + 帖子流，按 styles.css 纸张风格重写。
import { useCallback, useEffect, useRef, useState } from "react";
import {
  CheckCircle2,
  Heart,
  Loader2,
  MessageSquare,
  PenLine,
  Pin,
  ShieldCheck,
  Users,
} from "lucide-react";
import {
  listPosts,
  type CommunityBoard,
  type CommunityPostSummary,
} from "../../api/community";
import { communityErrorMessage, relativeTime } from "./format";

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
            <CheckCircle2 size={14} color="var(--pine)" style={{ marginRight: 6, verticalAlign: -2 }} />
          )}
          {post.title}
        </div>
        <div className="post-meta">
          <span className="tag">{post.board.name}</span>
          <span>{post.author.display_name}</span>
          <span>{relativeTime(post.last_activity_at)}</span>
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

export default function CommunityHome({
  boards,
  boardsLoading,
  onOpenBoard,
  onOpenPost,
  onCreatePost,
  onApply,
  onAdmin,
  isAdmin,
  isLoggedIn,
  onLoginRequired,
}: {
  boards: CommunityBoard[];
  boardsLoading: boolean;
  onOpenBoard: (slug: string) => void;
  onOpenPost: (postId: string) => void;
  onCreatePost: (boardId?: string) => void;
  onApply: () => void;
  onAdmin: () => void;
  isAdmin: boolean;
  isLoggedIn: boolean;
  onLoginRequired: () => void;
}) {
  const [posts, setPosts] = useState<CommunityPostSummary[]>([]);
  const [nextCursor, setNextCursor] = useState<string | null>(null);
  const [hasMore, setHasMore] = useState(false);
  const [loadingPosts, setLoadingPosts] = useState(true);
  const [loadingMore, setLoadingMore] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const requestSeq = useRef(0);

  const load = useCallback(async (cursor: string | null, append: boolean) => {
    const requestId = requestSeq.current + 1;
    requestSeq.current = requestId;
    if (append) setLoadingMore(true);
    else setLoadingPosts(true);
    setError(null);
    try {
      const page = await listPosts({ cursor: cursor ?? undefined, limit: 20 });
      if (requestId !== requestSeq.current) return;
      setPosts((prev) => (append ? [...prev, ...page.items] : page.items));
      setNextCursor(page.next_cursor);
      setHasMore(page.has_more);
    } catch (e) {
      if (requestId !== requestSeq.current) return;
      setError(communityErrorMessage(e, "帖子加载失败"));
    } finally {
      if (requestId === requestSeq.current) {
        setLoadingPosts(false);
        setLoadingMore(false);
      }
    }
  }, []);

  useEffect(() => {
    void load(null, false);
  }, [load]);

  return (
    <div className="rise">
      <section className="comm-section">
        <div className="comm-section-head">
          <div>
            <span className="sec-num">01</span>
            <span className="section-title">板块</span>
          </div>
          <button
            className="btn btn-primary"
            style={{ padding: "6px 14px", fontSize: 12.5 }}
            onClick={() => (isLoggedIn ? onCreatePost() : onLoginRequired())}
          >
            <PenLine size={13} /> 发起讨论
          </button>
          <button
            className="btn btn-ghost"
            style={{ padding: "6px 14px", fontSize: 12.5 }}
            onClick={() => (isLoggedIn ? onApply() : onLoginRequired())}
          >
            <Users size={13} /> 申请建吧
          </button>
          {isAdmin && (
            <button
              className="btn btn-ghost"
              style={{ padding: "6px 14px", fontSize: 12.5 }}
              onClick={onAdmin}
            >
              <ShieldCheck size={13} /> 建吧审核
            </button>
          )}
        </div>
        {boardsLoading && boards.length === 0 && (
          <div className="comm-detail-loading">
            <Loader2 className="spin" size={20} />
          </div>
        )}
        <div className="board-grid">
          {boards.map((board) => (
            <button
              key={board.board_id}
              className="board-card"
              onClick={() => onOpenBoard(board.slug)}
              type="button"
            >
              <div className="board-card-title">{board.name}</div>
              <div className="board-card-desc">
                {board.description || "暂无简介"}
              </div>
              <div className="board-card-foot">
                <span>{board.post_count ?? 0} 帖</span>
                <span className="board-card-go">进入板块 →</span>
              </div>
            </button>
          ))}
        </div>
        {!boardsLoading && boards.length === 0 && (
          <div className="comm-empty">暂无板块</div>
        )}
      </section>

      <section className="comm-section">
        <div className="comm-section-head">
          <div>
            <span className="sec-num">02</span>
            <span className="section-title">最新帖子</span>
          </div>
        </div>

        {loadingPosts && <SkeletonRows />}

        {error && posts.length === 0 && (
          <div className="comm-empty">
            <p>{error}</p>
            <button className="btn btn-primary" onClick={() => void load(null, false)}>
              重试
            </button>
          </div>
        )}

        {error && posts.length > 0 && (
          <div className="comm-error-inline" role="alert">
            <span>{error}</span>
            <button className="link-btn" onClick={() => void load(null, false)}>
              重试
            </button>
          </div>
        )}

        {!loadingPosts && !error && posts.length === 0 && (
          <div className="comm-empty">
            <p>还没有帖子，来发起第一个讨论吧</p>
          </div>
        )}

        {posts.length > 0 && (
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
              {loadingMore ? (
                <>
                  <Loader2 className="spin" size={13} /> 加载中…
                </>
              ) : (
                "加载更多"
              )}
            </button>
          </div>
        )}
      </section>
    </div>
  );
}

// 板块详情：板块信息 + 帖子游标分页列表（§九 板块详情）。
import { useCallback, useEffect, useRef, useState } from "react";
import {
  ArrowLeft,
  CheckCircle2,
  Heart,
  Loader2,
  MessageSquare,
  PenLine,
  Pin,
} from "lucide-react";
import {
  getBoardDetail,
  listPosts,
  type CommunityBoardDetail,
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

export default function BoardDetail({
  slug,
  onBack,
  onOpenPost,
  onCreatePost,
  isLoggedIn,
  onLoginRequired,
}: {
  slug: string;
  onBack: () => void;
  onOpenPost: (postId: string) => void;
  onCreatePost: (boardId: string) => void;
  isLoggedIn: boolean;
  onLoginRequired: () => void;
}) {
  const [board, setBoard] = useState<CommunityBoardDetail | null>(null);
  const [posts, setPosts] = useState<CommunityPostSummary[]>([]);
  const [nextCursor, setNextCursor] = useState<string | null>(null);
  const [hasMore, setHasMore] = useState(false);
  const [loadingBoard, setLoadingBoard] = useState(true);
  const [loadingPosts, setLoadingPosts] = useState(true);
  const [loadingMore, setLoadingMore] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const requestSeq = useRef(0);

  const loadBoard = useCallback(async () => {
    setLoadingBoard(true);
    setError(null);
    try {
      const resp = await getBoardDetail(slug);
      setBoard(resp);
    } catch (e) {
      setError(communityErrorMessage(e, "板块加载失败"));
    } finally {
      setLoadingBoard(false);
    }
  }, [slug]);

  const loadPosts = useCallback(
    async (cursor: string | null, append: boolean) => {
      const requestId = requestSeq.current + 1;
      requestSeq.current = requestId;
      if (append) setLoadingMore(true);
      else setLoadingPosts(true);
      setError(null);
      try {
        const page = await listPosts({
          board_id: board?.board_id,
          cursor: cursor ?? undefined,
          limit: 20,
        });
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
    },
    [board?.board_id],
  );

  useEffect(() => {
    void loadBoard();
  }, [loadBoard]);

  useEffect(() => {
    if (board) void loadPosts(null, false);
  }, [board, loadPosts]);

  if (error && !board && !loadingBoard) {
    return (
      <div className="comm-empty">
        <p>{error}</p>
        <button className="btn btn-primary" onClick={onBack}>
          返回社区
        </button>
      </div>
    );
  }

  return (
    <div className="rise">
      <button className="comm-back" onClick={onBack}>
        <ArrowLeft size={14} /> 返回社区首页
      </button>

      {loadingBoard && (
        <div className="comm-detail-loading">
          <Loader2 className="spin" size={20} />
        </div>
      )}

      {board && (
        <section className="board-detail-head">
          <div className="board-detail-title">{board.name}</div>
          <div className="board-detail-desc">
            {board.description || "暂无简介"}
          </div>
          <div className="board-detail-meta">
            <span className="tag">{board.post_count} 帖</span>
            <span className="tag">创建于 {relativeTime(board.created_at)}</span>
            {board.viewer_is_owner && <span className="tag green">吧主</span>}
            <div style={{ flex: 1 }} />
            <button
              className="btn btn-primary"
              style={{ padding: "6px 14px", fontSize: 12.5 }}
              onClick={() => (isLoggedIn ? onCreatePost(board.board_id) : onLoginRequired())}
            >
              <PenLine size={13} /> 发帖
            </button>
          </div>
        </section>
      )}

      {error && posts.length === 0 && (
        <div className="comm-empty">
          <p>{error}</p>
          <button className="btn btn-primary" onClick={() => void loadPosts(null, false)}>
            重试
          </button>
        </div>
      )}

      {loadingPosts && posts.length === 0 && (
        <div className="comm-skeleton">
          {[0, 1, 2].map((i) => (
            <div key={i} className="post-row">
              <span className="skeleton-line" style={{ width: 24 }} />
              <div className="post-main">
                <div className="skeleton-line" style={{ width: "55%" }} />
                <div className="skeleton-line" style={{ width: "30%", marginTop: 8 }} />
              </div>
            </div>
          ))}
        </div>
      )}

      {!loadingPosts && !error && posts.length === 0 && (
        <div className="comm-empty">
          <p>这个板块还没有帖子，来发第一帖吧</p>
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
            onClick={() => void loadPosts(nextCursor, true)}
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
    </div>
  );
}

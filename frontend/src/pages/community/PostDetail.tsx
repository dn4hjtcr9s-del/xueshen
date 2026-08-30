// 帖子详情：正文 + 配图（position 序，原图 max-width:100%）+ 回复区（§九 帖子详情）。
// 所有用户内容走 React 默认转义；图片 URL 仅用于 <img src>，不做 dangerouslySetInnerHTML。
import { useCallback, useEffect, useState } from "react";
import {
  ArrowLeft,
  Heart,
  Loader2,
  MessageSquare,
  Send,
  Trash2,
} from "lucide-react";
import {
  createReply,
  deletePost,
  deleteReply,
  getPostDetail,
  likePost,
  resolvePost,
  unlikePost,
  type CommunityPostDetail,
  type CommunityReplyView,
} from "../../api/community";
import { communityErrorMessage, relativeTime } from "./format";

export default function PostDetail({
  postId,
  onBack,
  isLoggedIn,
  onLoginRequired,
}: {
  postId: string;
  onBack: () => void;
  isLoggedIn: boolean;
  onLoginRequired: () => void;
}) {
  const [post, setPost] = useState<CommunityPostDetail | null>(null);
  const [replies, setReplies] = useState<CommunityReplyView[]>([]);
  const [replyCursor, setReplyCursor] = useState<string | null>(null);
  const [replyHasMore, setReplyHasMore] = useState(false);
  const [replyBody, setReplyBody] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [confirm, setConfirm] = useState<{ kind: "post" | "reply"; replyId?: string } | null>(
    null,
  );

  const load = useCallback(async (cursor?: string | null) => {
    try {
      const resp = await getPostDetail({
        post_id: postId,
        reply_cursor: cursor ?? undefined,
      });
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
      setError(communityErrorMessage(e, "帖子加载失败"));
    }
  }, [postId]);

  useEffect(() => {
    setPost(null);
    setReplies([]);
    setReplyBody("");
    setError(null);
    void load();
  }, [load]);

  const acting = async (fn: () => Promise<unknown>) => {
    if (busy) return;
    setBusy(true);
    setError(null);
    try {
      await fn();
      await load();
    } catch (e) {
      setError(communityErrorMessage(e, "操作失败"));
    } finally {
      setBusy(false);
    }
  };

  if (error) {
    return (
      <div className="comm-empty">
        <p>{error}</p>
        <button className="btn btn-primary" onClick={onBack}>
          返回社区
        </button>
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
  const attachments = post.attachments ?? [];

  return (
    <div className="rise">
      <button className="comm-back" onClick={onBack}>
        <ArrowLeft size={14} /> 返回
      </button>

      <article className="comm-detail">
        <div className="comm-detail-title">
          {post.deleted ? "该帖子已被作者删除" : post.title}
        </div>
        <div className="post-meta">
          <span className="tag">{post.board.name}</span>
          <span>{post.author.display_name}</span>
          <span>{relativeTime(post.created_at)}</span>
          {post.solved && <span className="tag green">已解决</span>}
          {!post.deleted && post.pinned && <span className="tag">置顶</span>}
        </div>

        {!post.deleted && <div className="comm-detail-body">{post.body}</div>}

        {!post.deleted && attachments.length > 0 && (
          <div className="comm-attachments">
            {attachments.map((a) => (
              <figure key={a.attachment_id} className="comm-attachment">
                <img
                  src={a.url}
                  alt={a.mime}
                  style={{ maxWidth: "100%", height: "auto" }}
                />
                <figcaption>
                  {a.width} × {a.height}
                </figcaption>
              </figure>
            ))}
          </div>
        )}

        {!post.deleted && (
          <div className="comm-detail-actions">
            <button
              className={`comm-action ${post.viewer_liked ? "liked" : ""}`}
              onClick={() => {
                if (!isLoggedIn) {
                  onLoginRequired();
                  return;
                }
                void acting(() =>
                  post.viewer_liked ? unlikePost(postId) : likePost(postId),
                );
              }}
            >
              <Heart size={14} fill={post.viewer_liked ? "currentColor" : "none"} />{" "}
              {post.like_count}
            </button>
            {post.viewer_is_author && (
              <button
                className="comm-action danger"
                onClick={() => {
                  if (!isLoggedIn) {
                    onLoginRequired();
                    return;
                  }
                  setConfirm({ kind: "post" });
                }}
              >
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
              <span>{relativeTime(r.created_at)}</span>
              {r.solved && <span className="tag green">解决答案</span>}
              {!r.deleted && post.viewer_is_author && canInteract && (
                <button
                  className="link-btn"
                  onClick={() => {
                    if (!isLoggedIn) {
                      onLoginRequired();
                      return;
                    }
                    void acting(() => resolvePost(postId, r.solved ? null : r.reply_id));
                  }}
                >
                  {r.solved ? "取消解决" : "标记为解决"}
                </button>
              )}
              {!r.deleted && r.viewer_is_author && (
                <button
                  className="link-btn"
                  onClick={() => {
                    if (!isLoggedIn) {
                      onLoginRequired();
                      return;
                    }
                    setConfirm({ kind: "reply", replyId: r.reply_id });
                  }}
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
            <button
              className="btn btn-ghost"
              onClick={() => void load(replyCursor)}
            >
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
              onClick={() => {
                if (!isLoggedIn) {
                  onLoginRequired();
                  return;
                }
                void acting(async () => {
                  await createReply(postId, replyBody.trim());
                  setReplyBody("");
                });
              }}
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
                    void (async () => {
                      if (busy) return;
                      setBusy(true);
                      setError(null);
                      try {
                        await deletePost(postId);
                        onBack();
                      } catch (e) {
                        setError(communityErrorMessage(e, "删除失败"));
                      } finally {
                        setBusy(false);
                      }
                    })();
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

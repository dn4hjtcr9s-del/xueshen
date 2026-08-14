// Community API 客户端（方案 §6.5 / §6.6，v1.6 冻结）。
// 经由共享请求层 client.ts 发出（挂 Bearer、带 credentials、401 刷新、
// PublicError 信封与 idempotencyKey）；前端不提交 user_id（§9.1）。

import { idempotencyKey, request } from "./client";

// ---------------------------------------------------------------------------
// 契约镜像类型（后端 backend/community/contracts/api.py，§6.6 冻结）
// ---------------------------------------------------------------------------

export interface CommunityBoard {
  board_id: string;
  slug: string;
  name: string;
  description: string;
}

export interface CommunityAuthor {
  display_name: string;
}

export interface CommunityPostSummary {
  post_id: string;
  board: CommunityBoard;
  author: CommunityAuthor;
  title: string; // Summary 恒非 null（列表只含 active）
  pinned: boolean;
  solved: boolean;
  reply_count: number;
  like_count: number;
  viewer_liked: boolean;
  created_at: string;
  last_activity_at: string;
}

// §6.6：Detail 的 title 可空（deleted 墓碑）；Summary 恒非 null，
// 故不用 extends，显式组合全部字段
export interface CommunityPostDetail {
  post_id: string;
  board: CommunityBoard;
  author: CommunityAuthor;
  title: string | null; // deleted 时为 null（墓碑）
  pinned: boolean;
  solved: boolean;
  reply_count: number;
  like_count: number;
  viewer_liked: boolean;
  created_at: string;
  last_activity_at: string;
  body: string | null; // deleted 时为 null（墓碑）
  deleted: boolean;
  discussion_status: "open" | "closed";
  viewer_is_author: boolean;
  solved_reply_id: string | null;
  deleted_at: string | null;
}

export interface CommunityReplyView {
  reply_id: string;
  author: CommunityAuthor;
  body: string | null; // deleted 时为 null（墓碑占位行）
  deleted: boolean;
  viewer_is_author: boolean;
  solved: boolean;
  created_at: string;
}

export interface CommunityNotification {
  notification_id: string;
  event_type: "post_replied" | "reply_marked_solved";
  title: string;
  body: string;
  read_at: string | null;
  created_at: string;
  post_id: string | null;
  reply_id: string | null;
}

// §19.45 Page 信封：{items, next_cursor, has_more}；通知页额外顶层 unread_count
export interface CursorPage<T> {
  items: T[];
  next_cursor: string | null;
  has_more: boolean;
}

export type CommunityPostListPage = CursorPage<CommunityPostSummary>;

export interface CommunityPostDetailResponse {
  post: CommunityPostDetail;
  replies: CursorPage<CommunityReplyView>;
}

export interface CommunityNotificationPage extends CursorPage<CommunityNotification> {
  unread_count: number;
}

// ---------------------------------------------------------------------------
// 只读接口（PR-B；写接口在 PR-C 追加）
// ---------------------------------------------------------------------------

/** 板块列表（§8.1）：只返回 active 板块。 */
export async function listBoards(): Promise<CommunityBoard[]> {
  const body = await request<{ items: CommunityBoard[] }>("GET", "/community/boards");
  return body.items;
}

/** 帖子列表（§8.2）：latest/unanswered + 板块筛选 + 游标分页。 */
export async function listPosts(params: {
  board_id?: string;
  sort?: "latest" | "unanswered";
  cursor?: string;
  limit?: number;
}): Promise<CommunityPostListPage> {
  const search = new URLSearchParams();
  if (params.board_id) search.set("board_id", params.board_id);
  if (params.sort && params.sort !== "latest") search.set("sort", params.sort);
  if (params.cursor) search.set("cursor", params.cursor);
  if (params.limit) search.set("limit", String(params.limit));
  const qs = search.toString();
  return request<CommunityPostListPage>(
    "GET",
    `/community/posts${qs ? `?${qs}` : ""}`,
  );
}

/** 帖子详情 + 一页回复（§8.4）。 */
export async function getPostDetail(params: {
  post_id: string;
  reply_cursor?: string;
  reply_limit?: number;
}): Promise<CommunityPostDetailResponse> {
  const search = new URLSearchParams();
  if (params.reply_cursor) search.set("reply_cursor", params.reply_cursor);
  if (params.reply_limit) search.set("reply_limit", String(params.reply_limit));
  const qs = search.toString();
  return request<CommunityPostDetailResponse>(
    "GET",
    `/community/posts/${params.post_id}${qs ? `?${qs}` : ""}`,
  );
}

// ---------------------------------------------------------------------------
// 写接口（§8.3–§8.5，PR-C）：均携带 Idempotency-Key（幂等重试不重复创建）
// ---------------------------------------------------------------------------

/** 发帖（§8.3）：user_id 由服务端认证上下文取得。 */
export async function createPost(payload: {
  board_id: string;
  title: string;
  body: string;
}): Promise<CommunityPostDetail> {
  return request<CommunityPostDetail>("POST", "/community/posts", {
    body: payload,
    idempotencyKey: idempotencyKey(),
  });
}

/** 回复（§8.4）。 */
export async function createReply(
  post_id: string,
  body: string,
): Promise<CommunityReplyView> {
  return request<CommunityReplyView>("POST", `/community/posts/${post_id}/replies`, {
    body: { body },
    idempotencyKey: idempotencyKey(),
  });
}

/** 点赞（§8.5：幂等）。 */
export async function likePost(post_id: string): Promise<void> {
  await request("POST", `/community/posts/${post_id}/like`);
}

/** 取消点赞（§8.5：幂等）。 */
export async function unlikePost(post_id: string): Promise<void> {
  await request("DELETE", `/community/posts/${post_id}/like`);
}

/** 标记解决/取消解决（§8.5：reply_id=null 表示取消解决）。 */
export async function resolvePost(post_id: string, reply_id: string | null): Promise<void> {
  await request("POST", `/community/posts/${post_id}/resolve`, {
    body: { reply_id },
  });
}

/** 删除帖子（§11.1：作者本人；重复删除幂等成功）。 */
export async function deletePost(post_id: string): Promise<void> {
  await request("DELETE", `/community/posts/${post_id}`);
}

/** 删除回复（§11.1）。 */
export async function deleteReply(post_id: string, reply_id: string): Promise<void> {
  await request("DELETE", `/community/posts/${post_id}/replies/${reply_id}`);
}

// ---------------------------------------------------------------------------
// 社区通知（§8.6）
// ---------------------------------------------------------------------------

/** 通知列表：只返回当前用户记录 + 全部未读数。 */
export async function listCommunityNotifications(params: {
  unread_only?: boolean;
  cursor?: string;
  limit?: number;
} = {}): Promise<CommunityNotificationPage> {
  const search = new URLSearchParams();
  if (params.unread_only) search.set("unread_only", "true");
  if (params.cursor) search.set("cursor", params.cursor);
  if (params.limit) search.set("limit", String(params.limit));
  const qs = search.toString();
  return request<CommunityNotificationPage>(
    "GET",
    `/community/notifications${qs ? `?${qs}` : ""}`,
  );
}

/** 全部已读（§8.6）：只更新当前认证用户的未读记录。 */
export async function markAllCommunityNotificationsRead(): Promise<{
  unread_count: number;
}> {
  return request<{ unread_count: number }>(
    "POST",
    "/community/notifications/read-all",
  );
}

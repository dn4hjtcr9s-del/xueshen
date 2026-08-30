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
  /** 0002 新增，列表恒返回；旧 fixture 可能缺省，视图层用 ?? 0 兜底 */
  post_count?: number;
  /** 0002 新增，列表恒返回；仅用于排序展示 */
  sort_order?: number;
}

/** 板块详情（§八 #2）：viewer_is_owner 恒 bool，匿名 false */
export interface CommunityBoardDetail {
  board_id: string;
  slug: string;
  name: string;
  description: string;
  post_count: number;
  created_at: string;
  viewer_is_owner: boolean;
}

/** §八 #2：板块详情为平铺对象（与后端 BoardDetailResponse 一致，无包裹层）。 */
export type CommunityBoardDetailResponse = CommunityBoardDetail;

/** 附件视图（D37 恒返回，按 position ASC；无附件为 []） */
export interface CommunityAttachment {
  attachment_id: string;
  url: string;
  width: number;
  height: number;
  mime: string;
  position: number;
}

/** POST /uploads 成功响应（201） */
export interface CommunityAttachmentUpload {
  attachment_id: string;
  url: string;
  mime: string;
  width: number;
  height: number;
  size_bytes: number;
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
  /** 0002 新增，恒返回数组；旧 fixture 可能缺省 */
  attachments?: CommunityAttachment[];
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
  /** 0002 新增，恒返回数组；旧 fixture 可能缺省 */
  attachments?: CommunityAttachment[];
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
  event_type:
    | "post_replied"
    | "reply_marked_solved"
    | "application_approved"
    | "application_rejected";
  title: string;
  body: string;
  read_at: string | null;
  created_at: string;
  post_id: string | null;
  reply_id: string | null;
  /** 0002 新增：四类通知恒非空；审核通知 post_id/reply_id 为 null */
  board_slug?: string | null;
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

/** 发帖（§8.3）：user_id 由服务端认证上下文取得；attachment_ids 顺序即展示顺序。 */
export async function createPost(payload: {
  board_id: string;
  title: string;
  body: string;
  attachment_ids?: string[];
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

// ---------------------------------------------------------------------------
// 社区重建新增接口（docs/community-rebuild-plan.md §八 #2/#12–#18，v3.9 冻结）
// 注意：这些路由仅在后端 COMMUNITY_V2_ENABLED=true 时挂载
// ---------------------------------------------------------------------------

export type CommunityBoardApplicationStatus = "pending" | "approved" | "rejected";

/** 申请对象（D44：不返回 reviewer_id；mine 与 admin 列表同一形状） */
export interface CommunityBoardApplication {
  application_id: string;
  name: string;
  slug: string;
  description: string;
  reason: string;
  status: CommunityBoardApplicationStatus;
  board_id: string | null;
  reviewed_at: string | null;
  reject_reason: string | null;
  created_at: string;
}

export type CommunityBoardApplicationPage = CursorPage<CommunityBoardApplication>;

export interface CommunityPermissions {
  is_community_admin: boolean;
}

/** 板块详情（§八 #2）。 */
export async function getBoardDetail(slug: string): Promise<CommunityBoardDetailResponse> {
  return request<CommunityBoardDetailResponse>("GET", `/community/boards/${slug}`);
}

/** 当前用户社区权限（§八 #18）：管理员入口展示依据（D23）。 */
export async function getCommunityPermissions(): Promise<CommunityPermissions> {
  return request<CommunityPermissions>("GET", "/community/permissions");
}

/** 图片上传（§八 #12）：multipart 字段 file + Idempotency-Key；支持 AbortController 取消。 */
export async function uploadAttachment(
  file: File,
  signal?: AbortSignal,
): Promise<CommunityAttachmentUpload> {
  const formData = new FormData();
  formData.append("file", file);
  return request<CommunityAttachmentUpload>("POST", "/community/uploads", {
    formData,
    idempotencyKey: idempotencyKey(),
    signal,
  });
}

/** 提交建吧申请（§八 #13）：携带 Idempotency-Key。 */
export async function createBoardApplication(payload: {
  name: string;
  slug: string;
  description: string;
  reason: string;
}): Promise<CommunityBoardApplication> {
  return request<CommunityBoardApplication>("POST", "/community/applications", {
    body: payload,
    idempotencyKey: idempotencyKey(),
  });
}

/** 我的申请列表（§八 #14）：created_at DESC 游标分页。 */
export async function listMyBoardApplications(params: {
  cursor?: string;
  limit?: number;
} = {}): Promise<CommunityBoardApplicationPage> {
  const search = new URLSearchParams();
  if (params.cursor) search.set("cursor", params.cursor);
  if (params.limit) search.set("limit", String(params.limit));
  const qs = search.toString();
  return request<CommunityBoardApplicationPage>(
    "GET",
    `/community/applications/mine${qs ? `?${qs}` : ""}`,
  );
}

/** 管理员审核列表（§八 #15）：status ∈ pending/approved/rejected/all，created_at ASC。 */
export async function listAdminBoardApplications(params: {
  status?: "pending" | "approved" | "rejected" | "all";
  cursor?: string;
  limit?: number;
} = {}): Promise<CommunityBoardApplicationPage> {
  const search = new URLSearchParams();
  // 后端缺省 status=None 视为 all；审核台默认 pending 必须显式传
  search.set("status", params.status ?? "pending");
  if (params.cursor) search.set("cursor", params.cursor);
  if (params.limit) search.set("limit", String(params.limit));
  const qs = search.toString();
  return request<CommunityBoardApplicationPage>(
    "GET",
    `/community/admin/applications${qs ? `?${qs}` : ""}`,
  );
}

/** 审核通过（§八 #16）：无 Idempotency-Key（D38 行锁 + 状态门）。 */
export async function approveBoardApplication(
  applicationId: string,
): Promise<CommunityBoardApplication> {
  return request<CommunityBoardApplication>(
    "POST",
    `/community/admin/applications/${applicationId}/approve`,
  );
}

/** 审核拒绝（§八 #17）：reason 1–200 字符。 */
export async function rejectBoardApplication(
  applicationId: string,
  reason: string,
): Promise<CommunityBoardApplication> {
  return request<CommunityBoardApplication>(
    "POST",
    `/community/admin/applications/${applicationId}/reject`,
    { body: { reason } },
  );
}

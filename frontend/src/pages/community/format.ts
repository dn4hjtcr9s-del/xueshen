// 社区视图共享工具：时间格式与错误文案映射（§九 冻结文案）。
import { MemoryApiError } from "../../api/client";

export function relativeTime(iso: string): string {
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

/**
 * 前端只按 code 处理，不读取 retryable（§八 COMMUNITY_UPLOAD_FAILED 实现冻结）。
 * 未匹配 code 时使用后端 message 兜底。
 */
export function communityErrorMessage(e: unknown, fallback: string): string {
  if (e instanceof MemoryApiError) {
    switch (e.code) {
      case "COMMUNITY_RATE_LIMITED":
        return "操作太频繁，请稍后再试";
      case "COMMUNITY_UPLOAD_FAILED":
        return "服务繁忙，请稍后再试";
      case "UPLOAD_TOO_LARGE":
        return "图片不能超过 5MiB";
      case "UPLOAD_INVALID_TYPE":
        return "仅支持 jpeg/png/webp 图片";
      case "UPLOAD_BOMB_REJECTED":
        return "图片像素过大，请更换图片";
      case "ATTACHMENT_LIMIT_EXCEEDED":
        return "每帖最多 3 张图片";
      case "ATTACHMENT_FORBIDDEN":
        return "只能使用自己上传的图片";
      case "ATTACHMENT_CONFLICT":
        return "图片状态已变化，请移除后重试";
      case "BOARD_NAME_CONFLICT":
        return e.message || "该名称或标识已被占用";
      case "BOARD_SLUG_RESERVED":
        return "该标识为保留字，请更换";
      case "APPLICATION_DUPLICATE_PENDING":
        return "你已有待审核的申请";
      case "APPLICATION_ALREADY_REVIEWED":
        return "该申请已审核，请勿重复操作";
      case "REJECT_REASON_INVALID":
        return "拒绝理由需为 1–200 字符";
      case "ADMIN_REQUIRED":
        return "需要社区管理员权限";
      case "COMMUNITY_NOT_FOUND":
        return "内容不存在或已删除";
      case "COMMUNITY_BOARD_DISABLED":
        return "该板块暂不可发帖";
      case "COMMUNITY_POST_CLOSED":
        return "该帖子已关闭，无法操作";
      case "COMMUNITY_CONTENT_INVALID":
        return e.message || "内容不符合规范";
      case "COMMUNITY_IDEMPOTENCY_CONFLICT":
        return "请求冲突，请重试";
      case "COMMUNITY_CURSOR_INVALID":
        return "分页游标已失效，请刷新";
      case "REQUEST_EXTRA_FIELD":
      case "INVALID_PAYLOAD":
        return e.message || "请求格式有误";
      default:
        return e.message || fallback;
    }
  }
  if (e instanceof TypeError) return "网络异常，请重试";
  if (e instanceof Error) return e.message;
  return fallback;
}

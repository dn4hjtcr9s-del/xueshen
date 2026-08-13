// Conversation API 客户端（方案 §17）：经由共享请求层 client.ts 发出。
import { request, type PublicError } from "./client";
import type {
  ConversationDetail,
  ConversationListPage,
  CreateTurnRequest,
  CreateTurnResponse,
  TurnStatus,
} from "../types/conversation";

/** Conversation API 路径前缀（开发代理 /memory-api 映射同一 FastAPI App）。 */
const V1 = "/memory-api/api/v1/conversations";

export class ConversationApiError extends Error {
  readonly status: number;
  readonly code: string;
  readonly retryable: boolean;
  readonly currentVersion: number | null;

  constructor(status: number, error: Partial<PublicError> | undefined, fallback: string) {
    super(error?.message ?? fallback);
    this.status = status;
    this.code = error?.code ?? "INTERNAL_ERROR";
    this.retryable = error?.retryable ?? false;
    this.currentVersion = error?.current_version ?? null;
  }
}

export async function listConversations(
  cursor?: string | null,
  limit = 50,
): Promise<ConversationListPage> {
  const query = new URLSearchParams({ limit: String(limit) });
  if (cursor) query.set("cursor", cursor);
  return request<ConversationListPage>("GET", `${V1}?${query.toString()}`);
}

export async function getConversation(
  threadId: string,
  beforeSequence?: number,
  limit = 50,
): Promise<ConversationDetail> {
  const query = new URLSearchParams({ limit: String(limit) });
  if (beforeSequence !== undefined) query.set("before_sequence", String(beforeSequence));
  return request<ConversationDetail>("GET", `${V1}/${threadId}?${query.toString()}`);
}

export async function createConversation(): Promise<{ thread_id: string; version: number }> {
  return request<{ thread_id: string; version: number }>("POST", V1);
}

export async function deleteConversation(threadId: string): Promise<{ status: string }> {
  return request<{ status: string }>("DELETE", `${V1}/${threadId}`);
}

export async function createTurn(
  threadId: string,
  body: CreateTurnRequest,
): Promise<CreateTurnResponse> {
  try {
    return await request<CreateTurnResponse>("POST", `${V1}/${threadId}/turns`, {
      body: JSON.stringify(body),
    });
  } catch (error) {
    if (error instanceof Error && "code" in error) {
      // 评审 P2：保留 current_version（THREAD_VERSION_CONFLICT 时前端用于恢复）
      const apiError = error as unknown as {
        code: string;
        message: string;
        status: number;
        current_version?: number | null;
      };
      throw new ConversationApiError(
        apiError.status,
        {
          code: apiError.code,
          message: apiError.message,
          current_version: apiError.current_version,
        },
        "创建 Turn 失败",
      );
    }
    throw error;
  }
}

export async function getTurnStatus(
  threadId: string,
  turnId: string,
): Promise<TurnStatus> {
  return request<TurnStatus>("GET", `${V1}/${threadId}/turns/${turnId}`);
}

export async function cancelTurn(
  threadId: string,
  turnId: string,
): Promise<{ status: string }> {
  return request<{ status: string }>("DELETE", `${V1}/${threadId}/turns/${turnId}`);
}

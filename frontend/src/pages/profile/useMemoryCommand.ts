// Profile「AI 记住了我什么」命令提交 Hook（规格 §20.1 / §20.3）。
// P0 快速路径 200 直接生效；202 进入 §20.3 轮询；409 冲突刷新后提示重新确认。
import { useCallback, useEffect, useState } from "react";
import { MemoryApiError, type MemoryOperationResult } from "../../api/memory";
import { isTerminalStatus, useOperationPolling } from "../../api/operations";

export interface CommandNotice {
  kind: "success" | "info" | "error" | "conflict";
  text: string;
}

function noticeForTerminal(result: MemoryOperationResult): CommandNotice {
  if (result.status === "succeeded") return { kind: "success", text: "已完成。" };
  if (result.status === "needs_review")
    return { kind: "info", text: "已提交，进入待确认候选。" };
  return { kind: "error", text: result.error?.message ?? "任务未能完成。" };
}

export function useMemoryCommand(refresh: () => void) {
  const [operationId, setOperationId] = useState<string | null>(null);
  const [notice, setNotice] = useState<CommandNotice | null>(null);
  const { result, pending, timedOut } = useOperationPolling(operationId);

  useEffect(() => {
    if (!timedOut) return;
    setNotice({ kind: "info", text: "任务仍在后台处理，稍后刷新即可看到结果。" });
    setOperationId(null);
  }, [timedOut]);

  useEffect(() => {
    if (!result || !isTerminalStatus(result.status)) return;
    setNotice(noticeForTerminal(result));
    setOperationId(null);
    if (result.status === "succeeded" || result.status === "needs_review") refresh();
  }, [result, refresh]);

  const submit = useCallback(
    async (fn: () => Promise<MemoryOperationResult>) => {
      setNotice(null);
      try {
        const operation = await fn();
        if (isTerminalStatus(operation.status)) {
          setNotice(noticeForTerminal(operation));
          if (operation.status === "succeeded" || operation.status === "needs_review") refresh();
        } else {
          setOperationId(operation.operation_id);
          setNotice({ kind: "info", text: "已提交，正在处理…" });
        }
      } catch (error) {
        if (error instanceof MemoryApiError && error.status === 409) {
          // §20.1：409 冲突刷新数据后提示用户重新确认
          setNotice({ kind: "conflict", text: "数据已被更新，请查看最新内容后重新确认。" });
          refresh();
        } else if (error instanceof MemoryApiError) {
          setNotice({ kind: "error", text: error.message });
        } else {
          setNotice({ kind: "error", text: "网络错误，请稍后重试。" });
        }
      }
    },
    [refresh],
  );

  return {
    submit,
    pending,
    notice,
    dismissNotice: () => setNotice(null),
  };
}

export function formatTime(iso: string | null): string {
  if (!iso) return "—";
  const date = new Date(iso);
  return date.toLocaleString("zh-CN", { hour12: false });
}

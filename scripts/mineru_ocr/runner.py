"""MinerU OCR 可恢复编排器：按批次提交分片，并把每次状态变化持久化到本地。"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

from .client import MinerUClient, atomic_write_json, download_and_extract_result

MAX_BATCH_SIZE = 40
RETRYABLE_STATUSES = {"prepared", "pending", "retry"}
ACTIVE_JOB_STATES = {"created", "uploading", "processing"}
TERMINAL_JOB_STATES = {"done", "failed", "abandoned"}


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def iter_chunks(manifest: dict[str, Any]) -> Iterable[dict[str, Any]]:
    """按 manifest 书籍顺序遍历分片，不改变书籍边界。"""
    for book in manifest.get("books", []):
        if not isinstance(book, dict):
            continue
        for chunk in book.get("chunks", []):
            if isinstance(chunk, dict):
                yield chunk


def find_chunks(manifest: dict[str, Any], data_ids: Iterable[str]) -> list[dict[str, Any]]:
    """根据 API 的 data_id 找回本地分片。"""
    wanted = set(data_ids)
    return [chunk for chunk in iter_chunks(manifest) if str(chunk.get("data_id")) in wanted]


def select_runnable_chunks(manifest: dict[str, Any], batch_size: int = MAX_BATCH_SIZE) -> list[dict[str, Any]]:
    """选择最多 40 个可提交分片，跳过已下载或仍在运行的任务。"""
    if batch_size <= 0:
        raise ValueError("batch_size 必须大于 0")
    effective_batch_size = min(batch_size, MAX_BATCH_SIZE)
    selected: list[dict[str, Any]] = []
    for chunk in iter_chunks(manifest):
        if chunk.get("status") in RETRYABLE_STATUSES:
            selected.append(chunk)
        if len(selected) >= effective_batch_size:
            break
    return selected


def _job_path(output_dir: Path, batch_id: str) -> Path:
    return output_dir / "_jobs" / f"{batch_id}.json"


class OCRRunner:
    """协调本地 manifest、MinerU 批次状态和逐分片结果下载。"""

    def __init__(
        self,
        output_dir: Path,
        manifest: dict[str, Any],
        client: MinerUClient,
        *,
        poll_interval_seconds: float = 10,
        poll_timeout_seconds: float = 45 * 60,
        max_attempts: int = 3,
    ) -> None:
        self.output_dir = output_dir.resolve()
        self.manifest = manifest
        self.client = client
        self.poll_interval_seconds = poll_interval_seconds
        self.poll_timeout_seconds = poll_timeout_seconds
        self.max_attempts = max_attempts
        self.output_dir.mkdir(parents=True, exist_ok=True)
        (self.output_dir / "_jobs").mkdir(parents=True, exist_ok=True)

    def save_manifest(self) -> None:
        """原子保存总清单，并更新时间。"""
        self.manifest["updated_at"] = _utc_now()
        atomic_write_json(self.output_dir / "manifest.json", self.manifest)

    def _save_job(self, path: Path, job: dict[str, Any]) -> None:
        job["updated_at"] = _utc_now()
        self.client.persist_batch(path, job)

    def _set_status(self, chunks: Iterable[dict[str, Any]], status: str, *, error: str | None = None) -> None:
        for chunk in chunks:
            chunk["status"] = status
            if error is not None:
                chunk["error"] = error

    def _mark_retry_or_error(self, chunk: dict[str, Any], error: str) -> None:
        """达到最大尝试次数后进入 error，避免失败分片形成无限提交循环。"""
        chunk["status"] = (
            "retry" if int(chunk.get("attempt_count", 0) or 0) < self.max_attempts else "error"
        )
        chunk["error"] = error

    def _result_map(self, results: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
        return {str(item.get("data_id")): item for item in results if item.get("data_id") is not None}

    def _finish_results(self, job: dict[str, Any], job_path: Path, results: list[dict[str, Any]]) -> None:
        """处理终态结果并下载 done ZIP；失败项转入 retry。"""
        result_by_id = self._result_map(results)
        chunks = find_chunks(self.manifest, job.get("data_ids", []))
        for chunk in chunks:
            result = result_by_id.get(str(chunk["data_id"]))
            if result is None:
                chunk["status"] = "retry"
                chunk["error"] = "MinerU 批次结果缺少该 data_id"
                continue
            state = str(result.get("state", "unknown"))
            if state == "done":
                if chunk.get("status") == "downloaded":
                    continue
                try:
                    validation = download_and_extract_result(result, Path(str(chunk["chunk_dir"])))
                    chunk["status"] = "downloaded"
                    chunk["error"] = None
                    chunk["result_validation"] = validation
                except Exception as exc:  # 下载/完整性错误必须保留并允许恢复
                    self._mark_retry_or_error(
                        chunk, f"结果下载或校验失败: {type(exc).__name__}: {exc}"
                    )
            elif state in {"failed", "error"}:
                message = str(result.get("err_msg") or result.get("error") or "MinerU 未提供错误信息")
                if int(chunk.get("attempt_count", 0) or 0) < self.max_attempts:
                    chunk["status"] = "retry"
                else:
                    chunk["status"] = "error"
                chunk["error"] = message
            else:
                chunk["status"] = "processing"
        job["last_results"] = results
        job["state"] = "done" if all(chunk.get("status") == "downloaded" for chunk in chunks) else "failed"
        if job["state"] == "failed" and any(chunk.get("status") == "retry" for chunk in chunks):
            job["state"] = "partial"
        self._save_job(job_path, job)
        self.save_manifest()

    def _poll_and_finish(self, job: dict[str, Any], job_path: Path) -> None:
        def on_poll(results: list[dict[str, Any]]) -> None:
            job["last_results"] = results
            job["state"] = "processing"
            self._save_job(job_path, job)
            result_by_id = self._result_map(results)
            for chunk in find_chunks(self.manifest, job.get("data_ids", [])):
                result = result_by_id.get(str(chunk["data_id"]))
                if result is not None and str(result.get("state")) not in {"done", "failed", "error"}:
                    chunk["status"] = "processing"
            self.save_manifest()

        results = self.client.poll_batch(
            str(job["batch_id"]),
            interval_seconds=self.poll_interval_seconds,
            timeout_seconds=self.poll_timeout_seconds,
            on_poll=on_poll,
        )
        self._finish_results(job, job_path, results)

    def run_next_batch(self, batch_size: int = MAX_BATCH_SIZE) -> int:
        """提交并完成下一批分片，返回本批分片数量。"""
        chunks = select_runnable_chunks(self.manifest, batch_size)
        if not chunks:
            return 0
        batch = self.client.submit_batch(chunks)
        batch_id = str(batch["batch_id"])
        job_path = _job_path(self.output_dir, batch_id)
        job = {
            **batch,
            "state": "created",
            "data_ids": [str(chunk["data_id"]) for chunk in chunks],
            "uploaded_data_ids": [],
            "chunk_ids": [str(chunk["chunk_id"]) for chunk in chunks],
        }
        # 必须先保存 batch_id 和签名地址，进程中断后才有机会恢复或重新申请。
        self._save_job(job_path, job)
        for chunk in chunks:
            chunk["batch_id"] = batch_id
            chunk["status"] = "uploading"
            chunk["attempt_count"] = int(chunk.get("attempt_count", 0) or 0) + 1
        self.save_manifest()

        def on_uploaded(chunk: dict[str, Any]) -> None:
            job["uploaded_data_ids"].append(str(chunk["data_id"]))
            chunk["status"] = "submitted"
            self._save_job(job_path, job)
            self.save_manifest()

        try:
            job["state"] = "uploading"
            self._save_job(job_path, job)
            self.client.upload_batch(batch, chunks, on_uploaded=on_uploaded)
        except Exception as exc:
            message = f"上传失败: {type(exc).__name__}: {exc}"
            for chunk in chunks:
                self._mark_retry_or_error(chunk, message)
            job["state"] = "partial"
            job["error"] = message
            self._save_job(job_path, job)
            self.save_manifest()
            return len(chunks)

        job["state"] = "processing"
        self._save_job(job_path, job)
        self.save_manifest()
        self._poll_and_finish(job, job_path)
        return len(chunks)

    def _resume_job(self, job_path: Path) -> None:
        job = json.loads(job_path.read_text(encoding="utf-8"))
        if str(job.get("state")) in TERMINAL_JOB_STATES:
            return
        chunks = find_chunks(self.manifest, job.get("data_ids", []))
        by_id = {str(chunk["data_id"]): chunk for chunk in chunks}
        last_results = job.get("last_results") or []
        if last_results and all(str(item.get("state")) in {"done", "failed", "error"} for item in last_results):
            self._finish_results(job, job_path, last_results)
            return
        missing_ids = [data_id for data_id in job.get("data_ids", []) if data_id not in set(job.get("uploaded_data_ids", []))]
        if missing_ids:
            missing_chunks = [by_id[data_id] for data_id in missing_ids if data_id in by_id]
            try:
                self.client.upload_batch(
                    {"file_urls": [job["file_urls"][job["data_ids"].index(data_id)] for data_id in missing_ids]},
                    missing_chunks,
                    on_uploaded=lambda chunk: job["uploaded_data_ids"].append(str(chunk["data_id"])),
                )
            except Exception as exc:
                message = f"恢复上传失败: {type(exc).__name__}: {exc}"
                self._set_status(chunks, "retry", error=message)
                job["state"] = "partial"
                job["error"] = message
                self._save_job(job_path, job)
                self.save_manifest()
                return
            for chunk in missing_chunks:
                chunk["status"] = "submitted"
            job["state"] = "processing"
            self._save_job(job_path, job)
            self.save_manifest()
        self._poll_and_finish(job, job_path)

    def resume_jobs(self) -> None:
        """恢复 `_jobs` 中未进入终态的批次，不重复提交已有 batch_id。"""
        for job_path in sorted((self.output_dir / "_jobs").glob("*.json")):
            self._resume_job(job_path)

    def run_until_idle(self, batch_size: int = MAX_BATCH_SIZE) -> None:
        """先恢复旧批次，再持续提交直到没有可运行分片。"""
        self.resume_jobs()
        while select_runnable_chunks(self.manifest, batch_size):
            self.run_next_batch(batch_size)


def status_summary(manifest: dict[str, Any]) -> dict[str, Any]:
    """生成不含密钥的当前进度摘要。"""
    chunks = list(iter_chunks(manifest))
    counts: dict[str, int] = {}
    for chunk in chunks:
        status = str(chunk.get("status", "unknown"))
        counts[status] = counts.get(status, 0) + 1
    return {
        "book_count": len(manifest.get("books", [])),
        "total_pages": sum(int(book.get("page_count", 0)) for book in manifest.get("books", []) if isinstance(book, dict)),
        "chunk_count": len(chunks),
        "chunk_status": counts,
        "books": [
            {
                "book_id": book.get("book_id"),
                "book_name": book.get("book_name"),
                "page_count": book.get("page_count"),
                "status": book.get("status"),
                "chunk_status": {
                    status: sum(1 for chunk in book.get("chunks", []) if chunk.get("status") == status)
                    for status in sorted({str(chunk.get("status")) for chunk in book.get("chunks", [])})
                },
            }
            for book in manifest.get("books", [])
            if isinstance(book, dict)
        ],
    }

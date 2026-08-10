"""MinerU 在线 API 客户端：负责批次申请、无额外请求头上传、轮询、下载和安全解压。"""

from __future__ import annotations

import http.client
import json
import os
import stat
import subprocess
import tempfile
import time
import urllib.error
import urllib.request
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlsplit

API_BASE = "https://mineru.net/api/v4"
TERMINAL_STATES = {"done", "failed", "error"}


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def load_api_key(project_root: Path) -> str:
    """只读取项目 `.env` 中的 `MinerU_API`，不读取或输出其他秘密。"""
    env_path = project_root / ".env"
    if not env_path.is_file():
        raise RuntimeError(f"MinerU 配置文件不存在: {env_path}")
    for raw_line in env_path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        if key.strip() == "MinerU_API":
            token = value.strip().strip('"').strip("'")
            if token:
                return token
    raise RuntimeError(f"未在 {env_path} 中找到 MinerU_API")


def _sanitize_for_disk(value: Any) -> Any:
    """递归清除日志中可能误带的认证字段。"""
    if isinstance(value, dict):
        cleaned: dict[str, Any] = {}
        for key, item in value.items():
            if key.lower() in {"authorization", "token", "api_key", "mineru_api"}:
                cleaned[key] = "<redacted>"
            else:
                cleaned[key] = _sanitize_for_disk(item)
        return cleaned
    if isinstance(value, list):
        return [_sanitize_for_disk(item) for item in value]
    return value


def atomic_write_json(path: Path, value: Any) -> None:
    """使用同目录临时文件原子写 JSON，避免进程中断留下半个状态文件。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(_sanitize_for_disk(value), stream, ensure_ascii=False, indent=2)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary_name, path)
    except BaseException:
        try:
            os.unlink(temporary_name)
        except FileNotFoundError:
            pass
        raise


def api_request(
    method: str,
    path_or_url: str,
    *,
    token: str | None = None,
    payload: dict[str, Any] | None = None,
    timeout: int = 60,
) -> tuple[int, Any]:
    """执行 MinerU JSON 请求，并保留服务端错误正文用于诊断。"""
    url = path_or_url if path_or_url.startswith(("http://", "https://")) else API_BASE + path_or_url
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8") if payload is not None else None
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    request = urllib.request.Request(url, data=body, headers=headers, method=method)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            status = response.status
            data = response.read()
    except urllib.error.HTTPError as exc:
        status = exc.code
        data = exc.read()
    except urllib.error.URLError as exc:
        raise RuntimeError(f"访问 MinerU 失败: {exc.reason}") from exc
    text = data.decode("utf-8", errors="replace")
    try:
        return status, json.loads(text)
    except json.JSONDecodeError:
        return status, {"raw": text}


def assert_api_ok(status: int, data: Any, context: str) -> dict[str, Any]:
    """同时检查 HTTP 状态与 MinerU 业务状态。"""
    if status < 200 or status >= 300:
        raise RuntimeError(f"{context} HTTP {status}: {json.dumps(data, ensure_ascii=False)[:1000]}")
    if not isinstance(data, dict):
        raise RuntimeError(f"{context} 返回格式异常: {data!r}")
    if data.get("code") not in (None, 0, 200):
        raise RuntimeError(f"{context} 业务错误: {json.dumps(data, ensure_ascii=False)[:1000]}")
    return data


def safe_extract_zip(zip_path: Path, destination: Path) -> None:
    """解压 ZIP，同时拒绝绝对路径、`..` 路径和符号链接。"""
    destination.mkdir(parents=True, exist_ok=True)
    root = destination.resolve()
    with zipfile.ZipFile(zip_path) as archive:
        for info in archive.infolist():
            member = Path(info.filename)
            unix_mode = (info.external_attr >> 16) & 0xFFFF
            if member.is_absolute() or ".." in member.parts:
                raise ValueError(f"ZIP 包含路径穿越条目: {info.filename}")
            if stat.S_ISLNK(unix_mode):
                raise ValueError(f"ZIP 包含不允许的符号链接: {info.filename}")
            target = (destination / member).resolve()
            try:
                target.relative_to(root)
            except ValueError as exc:
                raise ValueError(f"ZIP 包含路径穿越条目: {info.filename}") from exc
            if info.is_dir():
                target.mkdir(parents=True, exist_ok=True)
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            with archive.open(info) as source, target.open("wb") as output:
                while block := source.read(1024 * 1024):
                    output.write(block)


def _read_json_file(path: Path) -> tuple[bool, str | None]:
    try:
        json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        return False, str(exc)
    return True, None


def validate_result_files(raw_dir: Path) -> dict[str, Any]:
    """检查 VLM 结果中用于合并和回溯的核心文件是否齐全且 JSON 可解析。"""
    content_lists = sorted(raw_dir.glob("*_content_list.json"))
    content_lists_v2 = sorted(raw_dir.glob("*_content_list_v2.json"))
    required: dict[str, Path | None] = {
        "full.md": raw_dir / "full.md",
        "layout.json": raw_dir / "layout.json",
        "content_list": content_lists[0] if content_lists else None,
        "content_list_v2": content_lists_v2[0] if content_lists_v2 else None,
    }
    missing: list[str] = []
    invalid_json: dict[str, str] = {}
    for name, path in required.items():
        if path is None or not path.is_file():
            missing.append(name)
            continue
        if name != "full.md":
            valid, error = _read_json_file(path)
            if not valid:
                invalid_json[name] = error or "JSON 解析失败"
    return {
        "valid": not missing and not invalid_json,
        "missing": missing,
        "invalid_json": invalid_json,
        "full_md": "full.md" if (raw_dir / "full.md").is_file() else None,
        "layout": "layout.json" if (raw_dir / "layout.json").is_file() else None,
        "content_list": content_lists[0].name if content_lists else None,
        "content_list_v2": content_lists_v2[0].name if content_lists_v2 else None,
        "checked_at": _utc_now(),
    }


def download_zip(url: str, destination: Path) -> None:
    """使用 curl 下载签名 CDN 文件，规避部分环境中的 TLS EOF 问题。"""
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_suffix(destination.suffix + ".part")
    subprocess.run(
        ["curl", "-sS", "-L", "--fail", "--retry", "3", "-o", str(temporary), url],
        check=True,
    )
    if not zipfile.is_zipfile(temporary):
        temporary.unlink(missing_ok=True)
        raise RuntimeError(f"下载结果不是有效 ZIP: {destination.name}")
    os.replace(temporary, destination)


def download_and_extract_result(result: dict[str, Any], chunk_dir: Path) -> dict[str, Any]:
    """下载单个完成任务的 ZIP，安全解压到 chunk 的 raw 目录并执行完整性门禁。"""
    if str(result.get("state")) != "done":
        raise ValueError(f"只能下载 done 任务，当前状态: {result.get('state')}")
    url = result.get("full_zip_url") or result.get("zip_url") or result.get("result_url")
    if not isinstance(url, str) or not url:
        raise RuntimeError("完成任务缺少 ZIP 下载地址")
    chunk_dir.mkdir(parents=True, exist_ok=True)
    zip_path = chunk_dir / "result.zip"
    raw_dir = chunk_dir / "raw"
    download_zip(url, zip_path)
    if raw_dir.exists():
        # raw 只包含可重新生成的 MinerU 解压结果；逐文件清理避免依赖外部命令。
        for path in sorted(raw_dir.rglob("*"), reverse=True):
            if path.is_file() or path.is_symlink():
                path.unlink()
            elif path.is_dir():
                path.rmdir()
    safe_extract_zip(zip_path, raw_dir)
    validation = validate_result_files(raw_dir)
    atomic_write_json(chunk_dir / "result_metadata.json", {"result": result, "validation": validation})
    if not validation["valid"]:
        raise RuntimeError(f"MinerU 结果文件不完整: {json.dumps(validation, ensure_ascii=False)}")
    return validation


class MinerUClient:
    """封装 MinerU v4 的批量上传和批次查询接口。"""

    def __init__(
        self,
        api_token: str,
        *,
        api_base: str = API_BASE,
        model_version: str = "vlm",
        language: str = "ch",
        enable_formula: bool = True,
        enable_table: bool = True,
    ) -> None:
        if not api_token:
            raise ValueError("MinerU API Token 不能为空")
        self._api_token = api_token
        self.api_base = api_base.rstrip("/")
        self.model_version = model_version
        self.language = language
        self.enable_formula = enable_formula
        self.enable_table = enable_table

    def _request(self, method: str, path: str, payload: dict[str, Any] | None = None) -> dict[str, Any]:
        status, response = api_request(
            method,
            self.api_base + path,
            token=self._api_token,
            payload=payload,
        )
        return assert_api_ok(status, response, f"MinerU {method} {path}")

    def submit_batch(self, chunks: list[dict[str, Any]]) -> dict[str, Any]:
        """申请一批预签名上传地址；调用方需先持久化返回值再执行上传。"""
        if not chunks:
            raise ValueError("不能提交空批次")
        files = [
            {
                "name": f"{chunk['data_id']}.pdf",
                "is_ocr": True,
                "data_id": str(chunk["data_id"]),
            }
            for chunk in chunks
        ]
        payload = {
            "files": files,
            "model_version": self.model_version,
            "enable_formula": self.enable_formula,
            "enable_table": self.enable_table,
            "language": self.language,
        }
        response = self._request("POST", "/file-urls/batch", payload)
        data = response.get("data") or {}
        batch_id = data.get("batch_id")
        file_urls = data.get("file_urls") or []
        if not isinstance(batch_id, str) or not batch_id or len(file_urls) != len(chunks):
            raise RuntimeError(f"申请上传地址返回异常: {json.dumps(response, ensure_ascii=False)[:2000]}")
        return {
            "batch_id": batch_id,
            "state": "created",
            "created_at": _utc_now(),
            "updated_at": _utc_now(),
            "data_ids": [str(chunk["data_id"]) for chunk in chunks],
            "file_urls": [str(url) for url in file_urls],
            "request": payload,
            "last_results": [],
        }

    def persist_batch(self, path: Path, batch: dict[str, Any]) -> None:
        """原子保存可恢复批次；认证 Token 不属于批次数据且会被防御性脱敏。"""
        atomic_write_json(path, batch)

    def upload_presigned_file(self, upload_url: str, path: Path) -> None:
        """使用底层 PUT 上传，不发送未参与签名的 Content-Type。"""
        parsed = urlsplit(upload_url)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise RuntimeError(f"上传地址不是有效 HTTP(S) URL: {upload_url!r}")
        connection_type = http.client.HTTPSConnection if parsed.scheme == "https" else http.client.HTTPConnection
        connection = connection_type(parsed.netloc, timeout=180)
        target = parsed.path or "/"
        if parsed.query:
            target = f"{target}?{parsed.query}"
        try:
            connection.putrequest("PUT", target, skip_accept_encoding=True)
            connection.putheader("Content-Length", str(path.stat().st_size))
            connection.endheaders()
            with path.open("rb") as stream:
                while block := stream.read(1024 * 1024):
                    connection.send(block)
            response = connection.getresponse()
            response_body = response.read()
            if response.status < 200 or response.status >= 300:
                text = response_body.decode("utf-8", errors="replace")
                raise RuntimeError(f"预签名上传失败 HTTP {response.status}: {text[:1000]}")
        finally:
            connection.close()

    def upload_batch(
        self,
        batch: dict[str, Any],
        chunks: list[dict[str, Any]],
        *,
        on_uploaded: Callable[[dict[str, Any]], None] | None = None,
    ) -> None:
        """按申请顺序上传批次中的本地分片。"""
        urls = batch.get("file_urls") or []
        if len(urls) != len(chunks):
            raise RuntimeError("批次 URL 数量与分片数量不一致")
        for url, chunk in zip(urls, chunks):
            self.upload_presigned_file(str(url), Path(str(chunk["pdf_path"])))
            if on_uploaded:
                on_uploaded(chunk)

    def get_batch_results(self, batch_id: str) -> list[dict[str, Any]]:
        """查询一次批次状态并返回逐文件结果。"""
        response = self._request("GET", f"/extract-results/batch/{batch_id}")
        data = response.get("data") or {}
        results = data.get("extract_result") or data.get("results") or []
        if not isinstance(results, list):
            raise RuntimeError(f"批次结果格式异常: {type(results).__name__}")
        return [item for item in results if isinstance(item, dict)]

    def poll_batch(
        self,
        batch_id: str,
        *,
        interval_seconds: float = 10,
        timeout_seconds: float = 45 * 60,
        on_poll: Callable[[list[dict[str, Any]]], None] | None = None,
    ) -> list[dict[str, Any]]:
        """轮询到所有已返回任务进入终态；每轮可由调用方持久化。"""
        started = time.monotonic()
        while True:
            results = self.get_batch_results(batch_id)
            if on_poll:
                on_poll(results)
            states = [str(item.get("state", "unknown")) for item in results]
            if results and all(state in TERMINAL_STATES for state in states):
                return results
            if time.monotonic() - started > timeout_seconds:
                raise TimeoutError(f"MinerU 批次 {batch_id} 轮询超过 {timeout_seconds} 秒")
            time.sleep(interval_seconds)

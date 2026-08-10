"""MinerU 对照实验运行器：用相同样本分别提交 pipeline 和 vlm。"""

from __future__ import annotations

import json
import http.client
import subprocess
import sys
import time
import urllib.error
import urllib.request
import zipfile
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit


ROOT = Path(__file__).resolve().parents[2]
EXPERIMENT_DIR = Path(__file__).resolve().parent
INPUT_DIR = EXPERIMENT_DIR / "input"
RAW_DIR = EXPERIMENT_DIR / "raw"
MANIFEST_PATH = EXPERIMENT_DIR / "sample_manifest.json"
API_BASE = "https://mineru.net/api/v4"
POLL_INTERVAL_SECONDS = 10
POLL_TIMEOUT_SECONDS = 45 * 60


def load_api_key() -> str:
    """只从项目 .env 读取 MinerU_API，不把密钥写入实验产物或标准输出。"""
    env_path = ROOT / ".env"
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


def api_request(
    method: str,
    path_or_url: str,
    token: str | None = None,
    payload: dict[str, Any] | None = None,
    raw_body: bytes | None = None,
    content_type: str | None = "application/json",
) -> tuple[int, Any]:
    """执行 JSON API 请求，并在失败时保留服务端响应正文。"""
    url = path_or_url if path_or_url.startswith("http") else API_BASE + path_or_url
    body = raw_body if raw_body is not None else (
        json.dumps(payload, ensure_ascii=False).encode("utf-8") if payload is not None else None
    )
    headers = {"Content-Type": content_type} if content_type else {}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    request = urllib.request.Request(url, data=body, headers=headers, method=method)
    try:
        with urllib.request.urlopen(request, timeout=60) as response:
            status = response.status
            data = response.read()
    except urllib.error.HTTPError as exc:
        status = exc.code
        data = exc.read()
    except urllib.error.URLError as exc:
        raise RuntimeError(f"访问 MinerU 失败: {exc.reason}") from exc
    text = data.decode("utf-8", errors="replace")
    if content_type == "application/json" or text.lstrip().startswith(("{", "[")):
        try:
            return status, json.loads(text)
        except json.JSONDecodeError:
            return status, {"raw": text}
    return status, text


def assert_api_ok(status: int, data: Any, context: str) -> dict[str, Any]:
    """统一判断 HTTP 和 MinerU 业务状态，避免静默保存错误响应。"""
    if status < 200 or status >= 300:
        raise RuntimeError(f"{context} HTTP {status}: {json.dumps(data, ensure_ascii=False)[:1000]}")
    if not isinstance(data, dict):
        raise RuntimeError(f"{context} 返回格式异常: {data!r}")
    code = data.get("code")
    if code not in (None, 0, 200):
        raise RuntimeError(f"{context} 业务错误: {json.dumps(data, ensure_ascii=False)[:1000]}")
    return data


def upload_presigned_file(upload_url: str, path: Path) -> None:
    """用低层 HTTPS PUT 上传，避免 urllib 自动补 Content-Type 破坏预签名。"""
    parsed = urlsplit(upload_url)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise RuntimeError(f"上传地址不是有效 HTTP(S) URL: {upload_url!r}")
    connection_type = (
        http.client.HTTPSConnection if parsed.scheme == "https" else http.client.HTTPConnection
    )
    connection = connection_type(parsed.netloc, timeout=120)
    target = parsed.path or "/"
    if parsed.query:
        target = f"{target}?{parsed.query}"
    try:
        connection.putrequest("PUT", target, skip_accept_encoding=True)
        connection.putheader("Content-Length", str(path.stat().st_size))
        connection.endheaders()
        with path.open("rb") as stream:
            while chunk := stream.read(1024 * 1024):
                connection.send(chunk)
        response = connection.getresponse()
        body = response.read()
    finally:
        connection.close()
    if response.status < 200 or response.status >= 300:
        detail = body.decode("utf-8", errors="replace")[:1000]
        raise RuntimeError(f"预签名上传失败 HTTP {response.status}: {detail}")


def download_zip(download_url: str, destination: Path) -> None:
    """通过 curl 下载 CDN ZIP，规避 Python urllib 与该 CDN 的 TLS EOF 问题。"""
    subprocess.run(
        [
            "curl",
            "-sS",
            "-L",
            "--retry",
            "3",
            "--retry-all-errors",
            "--max-time",
            "300",
            download_url,
            "-o",
            str(destination),
        ],
        check=True,
    )


def submit_batch(token: str, model_version: str, samples: list[dict[str, Any]]) -> dict[str, Any]:
    """申请批量上传地址，并通过签名 URL 上传同一组实验样本。"""
    files = [
        {
            "name": Path(str(sample["sample_path"])).name,
            "is_ocr": True,
            "data_id": f"compare_{model_version}_{sample['sample_id']}",
        }
        for sample in samples
    ]
    request_payload = {
        "files": files,
        "model_version": model_version,
        "enable_formula": True,
        "enable_table": True,
        "language": "ch",
    }
    status, response = api_request("POST", "/file-urls/batch", token=token, payload=request_payload)
    response = assert_api_ok(status, response, f"{model_version} 申请上传地址")
    data = response.get("data") or {}
    batch_id = data.get("batch_id")
    file_urls = data.get("file_urls") or []
    if not batch_id or len(file_urls) != len(samples):
        raise RuntimeError(
            f"{model_version} 上传地址响应缺少 batch_id 或 URL 数量不匹配: "
            f"{json.dumps(response, ensure_ascii=False)[:2000]}"
        )

    model_dir = RAW_DIR / model_version
    model_dir.mkdir(parents=True, exist_ok=True)
    (model_dir / "request.json").write_text(
        json.dumps(request_payload, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    (model_dir / "batch_create_response.json").write_text(
        json.dumps(response, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    for sample, upload_url in zip(samples, file_urls):
        path = Path(str(sample["sample_path"]))
        # 阿里云 OSS 预签名 URL 未签入 Content-Type，不能让 urllib 自动注入该请求头。
        upload_presigned_file(str(upload_url), path)
        print(f"[{model_version}] 已上传 {path.name}")
    return {"batch_id": batch_id, "model_dir": str(model_dir), "request": request_payload}


def poll_batch(token: str, model_version: str, batch_id: str, model_dir: Path) -> list[dict[str, Any]]:
    """轮询批次状态，直到全部任务完成或出现失败/超时。"""
    started = time.monotonic()
    logs: list[dict[str, Any]] = []
    final_results: list[dict[str, Any]] = []
    while True:
        status, response = api_request(
            "GET", f"/extract-results/batch/{batch_id}", token=token
        )
        response = assert_api_ok(status, response, f"{model_version} 查询批次")
        now = time.strftime("%Y-%m-%dT%H:%M:%S%z")
        logs.append({"time": now, "response": response})
        data = response.get("data") or {}
        results = data.get("extract_result") or data.get("results") or []
        states = [str(item.get("state", "unknown")) for item in results if isinstance(item, dict)]
        print(f"[{model_version}] batch={batch_id} states={states or '等待结果'}")
        if results and all(state in {"done", "failed", "error"} for state in states):
            final_results = results
            break
        if time.monotonic() - started > POLL_TIMEOUT_SECONDS:
            raise TimeoutError(f"{model_version} 批次轮询超过 {POLL_TIMEOUT_SECONDS} 秒")
        time.sleep(POLL_INTERVAL_SECONDS)

    (model_dir / "poll_log.json").write_text(
        json.dumps(logs, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    (model_dir / "final_results.json").write_text(
        json.dumps(final_results, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return final_results


def download_results(model_version: str, final_results: list[dict[str, Any]], model_dir: Path) -> None:
    """下载每个文件的完整 ZIP，并解压到独立目录供后续检查。"""
    for item in final_results:
        name = str(item.get("file_name") or item.get("file", {}).get("name") or item.get("data_id") or "unknown")
        state = str(item.get("state", "unknown"))
        if state != "done":
            print(f"[{model_version}] 跳过失败任务 {name}: {item.get('err_msg') or item.get('error')}")
            continue
        zip_url = item.get("full_zip_url") or item.get("zip_url") or item.get("result_url")
        if not zip_url:
            print(f"[{model_version}] 完成任务没有 ZIP URL: {name}")
            continue
        safe_name = Path(name).stem
        zip_path = model_dir / f"{safe_name}.zip"
        download_zip(str(zip_url), zip_path)
        extract_dir = model_dir / safe_name
        extract_dir.mkdir(parents=True, exist_ok=True)
        with zipfile.ZipFile(zip_path) as archive:
            archive.extractall(extract_dir)
        print(f"[{model_version}] 已下载并解压 {name}: {extract_dir}")


def main() -> None:
    """按模型顺序执行对照实验，并将所有请求、状态和原始结果保存在本地。"""
    token = load_api_key()
    manifest = json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))
    RAW_DIR.mkdir(parents=True, exist_ok=True)
    experiment_log: dict[str, Any] = {"started_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"), "models": {}}
    for model_version in ("pipeline", "vlm"):
        batch = submit_batch(token, model_version, manifest)
        final_results = poll_batch(token, model_version, batch["batch_id"], Path(batch["model_dir"]))
        download_results(model_version, final_results, Path(batch["model_dir"]))
        experiment_log["models"][model_version] = {
            "batch_id": batch["batch_id"],
            "final_results": final_results,
        }
    (EXPERIMENT_DIR / "experiment_log.json").write_text(
        json.dumps(experiment_log, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(f"实验完成，结果目录: {RAW_DIR}")


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print(f"实验失败: {type(exc).__name__}: {exc}", file=sys.stderr)
        raise

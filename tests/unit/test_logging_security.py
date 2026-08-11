"""日志安全测试（§21.1 / 裁决 10）：JSON 输出、日志 HMAC 与隐私 HMAC 分离。"""

from __future__ import annotations

import io
import json
import logging
from typing import Any
from uuid import uuid4

from backend.memory.contracts.common import user_log_hash, user_privacy_hash
from backend.memory.logging_config import JsonLogFormatter, configure_logging
from backend.settings import Settings


def _settings(tmp_path: Any, **overrides: Any) -> Settings:
    base: dict[str, Any] = {
        "app_env": "development",
        "dev_auth_enabled": True,
        "memory_storage_root": str(tmp_path / "storage"),
    }
    base.update(overrides)
    return Settings(**base)


def _format_record(formatter: JsonLogFormatter, message: str) -> dict[str, Any]:
    record = logging.LogRecord(
        name="memory.test",
        level=logging.INFO,
        pathname=__file__,
        lineno=1,
        msg=message,
        args=(),
        exc_info=None,
    )
    line = formatter.format(record)
    return json.loads(line)


def test_json_formatter_outputs_json() -> None:
    payload = _format_record(JsonLogFormatter(), "worker 启动 concurrency=2")
    assert payload["level"] == "INFO"
    assert payload["logger"] == "memory.test"
    assert payload["message"] == "worker 启动 concurrency=2"
    assert "ts" in payload


def test_log_hmac_separated_from_privacy_hmac(tmp_path: Any) -> None:
    """裁决 10：同一 user_id 在 LOG_HMAC_KEY 与 PRIVACY_HMAC_KEY 下摘要不同。"""
    settings = _settings(tmp_path)
    user_id = str(uuid4())
    log_hash = user_log_hash(settings.log_hmac_key, user_id)
    privacy_hash = user_privacy_hash(settings.privacy_hmac_key, user_id)
    assert log_hash != privacy_hash
    assert len(log_hash) == 64
    # 日志 key 轮换/更换会得到不同摘要（域与 key 双重分离）
    assert user_log_hash("another-log-key", user_id) != log_hash


def test_log_line_never_contains_raw_user_id(tmp_path: Any) -> None:
    """§21.1：日志只出现 HMAC 摘要，不出现 user_id 原值。"""
    settings = _settings(tmp_path)
    user_id = str(uuid4())
    message = (
        f"break-glass 使用: admin={user_log_hash(settings.log_hmac_key, user_id)} grant={uuid4()}"
    )
    payload = _format_record(JsonLogFormatter(), message)
    line = json.dumps(payload, ensure_ascii=False)
    assert user_id not in line
    assert user_log_hash(settings.log_hmac_key, user_id) in line


def test_configure_logging_idempotent(tmp_path: Any) -> None:
    """重复装配只保留一个 JSON handler，且不移除既有 handler。"""
    root = logging.getLogger()
    before = list(root.handlers)
    configure_logging(_settings(tmp_path))
    configure_logging(_settings(tmp_path))
    json_handlers = [h for h in root.handlers if getattr(h, "_memory_json_handler", False)]
    assert len(json_handlers) == 1
    assert all(h in root.handlers for h in before)
    # 清理，避免影响其他测试的日志输出
    for h in json_handlers:
        root.removeHandler(h)


def test_json_formatter_stderr_stream(tmp_path: Any) -> None:
    """JSON handler 输出到 stream 的内容可直接被日志栈按行采集。"""
    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    handler.setFormatter(JsonLogFormatter())
    logger = logging.getLogger("memory.test.stream")
    logger.addHandler(handler)
    try:
        logger.info("清理过期通知: %d 条", 7)
    finally:
        logger.removeHandler(handler)
    payload = json.loads(stream.getvalue().strip())
    assert payload["message"] == "清理过期通知: 7 条"

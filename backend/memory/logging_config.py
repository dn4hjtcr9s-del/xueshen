"""JSON 日志装配（规格 §21.1）。

- 日志以 JSON 行输出到 stderr，字段：ts/level/logger/message[/exception]。
- `user_id` 只允许以 `user_log_hash(LOG_HMAC_KEY, user_id)` 形式进入日志，
  与长期隐私 HMAC（PRIVACY_HMAC_KEY）域分离（裁决 10）。
- configure_logging 是幂等、追加式的：不替换既有 handler（pytest 捕获等），
  只在没有 JSON handler 时添加一个。
"""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime

from backend.settings import Settings

_HANDLER_MARK = "_memory_json_handler"


class JsonLogFormatter(logging.Formatter):
    """单行 JSON 日志格式（§21.1：可被集中日志栈采集）。"""

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, str] = {
            "ts": datetime.now(UTC).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)
        return json.dumps(payload, ensure_ascii=False, default=str)


def configure_logging(settings: Settings) -> None:
    """幂等装配：root logger 上保证恰好一个 JSON stderr handler。"""
    root = logging.getLogger()
    root.setLevel(settings.log_level.upper())
    for handler in root.handlers:
        if getattr(handler, _HANDLER_MARK, False):
            return
    handler = logging.StreamHandler()
    handler.setFormatter(JsonLogFormatter())
    setattr(handler, _HANDLER_MARK, True)
    root.addHandler(handler)

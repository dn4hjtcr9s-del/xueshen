"""Rollout 对象存储工厂（memory-rebuild §5.5 Phase 3）。

把"按配置选 Local 还是 Kodo"收敛到一处，worker / app / CLI 三个装配点共用，
避免三处各写一遍 if-else 而漏掉校验。

配置校验分层（§5.2 Phase 0-B / §5.12）：
- ``local``：只需根目录可用，不校验凭据；
- ``kodo``：bucket / region / access key / secret key 必须齐备，缺失**直接失败**，
  绝不静默降级为 local——生产凭据故障被静默降级会变成"数据写到了本机磁盘"。

**热缓存能力随 factory 一起产出**（review-2 新发现 15）：recorder 无论哪种后端都把热段
写在本地 ``conversation_rollout_root``，因此 kodo store 也会挂一个
:class:`LocalHotSegmentCache`。删除/retention 由此可以统一走"删对象 + 删热段"，
不会在远端部署里留下删不掉的本地热缓存。
"""

from __future__ import annotations

from typing import Any

from backend.conversation.contracts.object_store import ObjectStoreNonRetryableError
from backend.conversation.rollout.object_store import (
    LocalHotSegmentCache,
    LocalRolloutObjectStore,
)

#: Kodo 模式下必须齐备的配置项（值取 Settings 字段名）。
_KODO_REQUIRED_FIELDS = ("kodo_bucket", "kodo_region", "kodo_access_key", "kodo_secret_key")


def build_rollout_object_store(settings: Any) -> Any:
    """按 ``conversation_rollout_object_store`` 构造对象存储实现。

    返回类型是 :class:`RolloutObjectStore` 协议；调用方不应依赖具体实现。
    """
    backend = str(getattr(settings, "conversation_rollout_object_store", "local"))
    if backend == "local":
        return LocalRolloutObjectStore(root=settings.conversation_rollout_root)
    if backend == "kodo":
        missing = [name for name in _KODO_REQUIRED_FIELDS if not getattr(settings, name, None)]
        if missing:
            aliases = ", ".join(name.upper() for name in missing)
            raise ObjectStoreNonRetryableError(
                f"CONVERSATION_ROLLOUT_OBJECT_STORE=kodo 但缺少配置: {aliases}；"
                "不会静默降级为 local"
            )
        from backend.conversation.rollout.qiniu_kodo import QiniuKodoRolloutObjectStore

        return QiniuKodoRolloutObjectStore(
            bucket=str(settings.kodo_bucket),
            access_key=str(settings.kodo_access_key),
            secret_key=str(settings.kodo_secret_key),
            region=str(settings.kodo_region or ""),
            domain=str(settings.kodo_cdn_domain or ""),
            connect_timeout_seconds=int(settings.kodo_connect_timeout_seconds),
            read_timeout_seconds=int(settings.kodo_read_timeout_seconds),
            # 与 recorder 同一个本地热缓存根：远端部署也能清掉本机热段（新发现 15）
            hot_cache=LocalHotSegmentCache(root=settings.conversation_rollout_root),
        )
    raise ObjectStoreNonRetryableError(
        f"未知的 CONVERSATION_ROLLOUT_OBJECT_STORE: {backend}（可选 local / kodo）"
    )


__all__ = ["build_rollout_object_store"]

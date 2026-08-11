"""Reader 接口与 Source deletion Handler 边界（§6.1 / §17）。

本期只定义接口与可注入测试适配器；Memory 模块不得读取外部系统的内部数据库表。
"""

from backend.memory.readers.base import (
    ActivityReader,
    ConversationReader,
    SourceDeletionHandler,
)
from backend.memory.readers.filtering import (
    DeletionAwareActivityReader,
    DeletionAwareConversationReader,
)
from backend.memory.readers.handler import RecordingSourceDeletionHandler

__all__ = [
    "ActivityReader",
    "ConversationReader",
    "DeletionAwareActivityReader",
    "DeletionAwareConversationReader",
    "RecordingSourceDeletionHandler",
    "SourceDeletionHandler",
]

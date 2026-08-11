"""失败恢复测试设施：复用 tests/integration 的真实 PostgreSQL 与存储 fixtures（§23.4）。"""

from tests.integration.conftest import (
    _migrate,  # noqa: F401  (pytest fixture, autouse)
    fake_activity_reader,  # noqa: F401
    fake_conversation_reader,  # noqa: F401
    fake_llm,  # noqa: F401
    memory_service,  # noqa: F401
    runner,  # noqa: F401
    runtime_context,  # noqa: F401
    session_factory,  # noqa: F401
    settings,  # noqa: F401
    store,  # noqa: F401
)

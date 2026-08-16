"""Study 内部账号清理路由（D19/§12.8）。

Phase 1 与公共 API 一并实现：POST /api/v1/internal/study-accounts/purge，
仅当 STUDY_ACCOUNT_PURGE_SERVICE_TOKEN 配置且调用方持 system actor +
study:account_purge scope 时挂载（fail-closed，§18.10）。当前为 Phase 0
占位：路由存在但无端点，保证 app.py 挂载分支的导入稳定。
"""

from __future__ import annotations

from fastapi import APIRouter

router = APIRouter(prefix="/api/v1/internal", tags=["internal-study"])

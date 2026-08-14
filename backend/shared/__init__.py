"""跨域共享组件（D24/D29）：cursor、限流、可信代理 IP、认证依赖。

Community 只依赖 backend/shared，不反向依赖 backend/memory/api 或
backend/auth_service 内部模块；Memory/Auth/Conversation 原位置保留 re-export。
"""

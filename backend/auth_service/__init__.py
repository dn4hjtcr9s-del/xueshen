"""认证服务：注册登录的统一签发方（方案 §2.3，内嵌于 memory-api 同进程）。

- errors / security / database / tokens / ratelimit / runtime / api 分层；
- 对外接口挂载 /api/v1/auth/，错误体对齐 PublicError（code/message/retryable/trace_id）。
"""

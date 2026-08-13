#!/usr/bin/env bash
# RSA 2048 认证密钥对生成脚本（方案 §6.2）。
# 私钥不加密、权限 0600、第一版不带 kid header。
# 本地密钥生成到 gitignore 的 .local/keys/（生产密钥在云服务器本地生成后手动上传）。
# 已有密钥时默认拒绝覆盖（覆盖会销毁既有 token 的验签能力），FORCE=1 可强制。
# 用法：bash scripts/generate_auth_keys.sh [输出目录，默认 .local/keys]
set -euo pipefail

cd "$(dirname "$0")/.."

OUT_DIR="${1:-.local/keys}"
PRIVATE="$OUT_DIR/auth_private.pem"
PUBLIC="$OUT_DIR/auth_public.pem"

mkdir -p "$OUT_DIR"

if [[ "${FORCE:-0}" != "1" ]] && { [[ -e "$PRIVATE" ]] || [[ -e "$PUBLIC" ]]; }; then
  echo "密钥已存在：$PRIVATE / $PUBLIC（设置 FORCE=1 可强制覆盖）" >&2
  exit 1
fi

umask 077
openssl genrsa -out "$PRIVATE" 2048
openssl rsa -in "$PRIVATE" -pubout -out "$PUBLIC"
chmod 600 "$PRIVATE"
chmod 644 "$PUBLIC"

echo "密钥对已生成（RSA 2048，无 kid）："
echo "  私钥（0600，仅认证服务持有）: $PRIVATE"
echo "  公钥（验签方使用）        : $PUBLIC"

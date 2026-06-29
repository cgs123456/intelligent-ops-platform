# ===================================================================
# 自签证书生成脚本（仅用于本地预演，生产请用 Let's Encrypt / 商业证书）
# 用法：bash nginx/gen-self-signed-cert.sh
# 产出：nginx/certs/fullchain.pem 与 nginx/certs/privkey.pem
# ===================================================================
set -e

CERT_DIR="$(cd "$(dirname "$0")" && pwd)/certs"
mkdir -p "$CERT_DIR"

openssl req -x509 -nodes -newkey rsa:2048 \
    -keyout "$CERT_DIR/privkey.pem" \
    -out    "$CERT_DIR/fullchain.pem" \
    -days   365 \
    -subj   "/C=CN/ST=Local/L=Local/O=OpsPlatform/CN=localhost" \
    -addext "subjectAltName=DNS:localhost,DNS:*.example.com,IP:127.0.0.1"

echo "证书已生成："
echo "  - $CERT_DIR/fullchain.pem"
echo "  - $CERT_DIR/privkey.pem"
echo "注意：自签证书仅用于本地预演，浏览器会警告不安全。"

#!/bin/bash
# 一键打包 + 上传到更新服务器（正式版 / 测试版双渠道）
#
# 用法：
#   bash release.sh                # 正式版（stable），版本取仓库根目录 VERSION
#   bash release.sh beta 2         # 测试版 0.2.8-beta.2（VERSION 仍是基线 0.2.8）
#
# 环境变量：
#   SERVER=47.101.175.39
#   SSH_PORT=22                 # 注意：20152 是 nginx 的 HTTP 端口，不是 SSH 端口
#   SSH_USER=developer01
#   REMOTE_PATH=/home/developer01/dljy/spider-gd/upload/releases
#   SKIP_UPLOAD=1               # 只打包 + 生成 history.json，不上传
#
# 发布服务器上的目录结构（与 nginx 的 /releases 映射对应）：
#   $REMOTE_PATH/           正式版：latest.yml、history.json、安装包、blockmap
#   $REMOTE_PATH/beta/      测试版：beta.yml、history.json、安装包、blockmap
#
# 版本中心读取的就是这两个目录里的 history.json；文件由
# release-history.json 经 tools/build_release_history.py 生成。

set -euo pipefail

CHANNEL="${1:-stable}"
BETA_NUMBER="${2:-}"
SERVER="${SERVER:-47.101.175.39}"
SSH_PORT="${SSH_PORT:-22}"
SSH_USER="${SSH_USER:-developer01}"
REMOTE_PATH="${REMOTE_PATH:-/home/developer01/dljy/spider-gd/upload/releases}"
SKIP_UPLOAD="${SKIP_UPLOAD:-0}"

cd "$(dirname "$0")"

if [ "$CHANNEL" != "stable" ] && [ "$CHANNEL" != "beta" ]; then
    echo "❌ 渠道只能是 stable 或 beta，当前：$CHANNEL"
    exit 1
fi
if [ "$CHANNEL" = "beta" ] && ! [[ "$BETA_NUMBER" =~ ^[1-9][0-9]*$ ]]; then
    echo "❌ 测试版必须给正整数编号，例如：bash release.sh beta 2"
    exit 1
fi

VERSION=$(tr -d '[:space:]' < VERSION)
RELEASE_VERSION="$VERSION"
if [ "$CHANNEL" = "beta" ]; then
    RELEASE_VERSION="${VERSION}-beta.${BETA_NUMBER}"
fi
if [ "$CHANNEL" = "beta" ]; then
    REMOTE_DIR="$REMOTE_PATH/beta"
    YML_FILE="beta.yml"
    PRODUCT_NAME="广东电力数据采集－测试版"
else
    REMOTE_DIR="$REMOTE_PATH"
    YML_FILE="latest.yml"
    PRODUCT_NAME="广东电力数据采集"
fi

echo "📦 渠道：$CHANNEL    版本：$RELEASE_VERSION"
echo ""

# 1. 编译后端
echo "=== 编译后端 ==="
if [ "$CHANNEL" = "beta" ]; then
    python build_backend.py --with-browsers --channel beta --beta-number "$BETA_NUMBER"
else
    python build_backend.py --with-browsers
fi

# 2. 编译前端
echo "=== 编译前端 ==="
cd frontend
if [ "$CHANNEL" = "beta" ]; then
    pnpm run build:win -- --channel beta --beta-number "$BETA_NUMBER"
else
    pnpm run build:win -- --channel stable
fi
cd ..

# 3. 生成版本中心用的 history.json（发布服务器静态文件）
echo "=== 生成 history.json ==="
python tools/build_release_history.py --channel "$CHANNEL" --out "frontend/dist/history.json"

DIST_DIR="frontend/dist"
SETUP_FILE="$DIST_DIR/${PRODUCT_NAME}-${RELEASE_VERSION}-setup.exe"
if [ ! -f "$SETUP_FILE" ]; then
    echo "❌ 未找到安装包：$SETUP_FILE"
    exit 1
fi

echo ""
echo "=== 本次产物 ==="
for f in "$SETUP_FILE" "$SETUP_FILE.blockmap" "$DIST_DIR/$YML_FILE" "$DIST_DIR/history.json"; do
    [ -f "$f" ] && echo "  $f"
done

if [ "$SKIP_UPLOAD" = "1" ]; then
    echo ""
    echo "✅ SKIP_UPLOAD=1：已打包并生成 history.json，未上传。"
    exit 0
fi

# 4. 上传（正式版 -> $REMOTE_PATH，测试版 -> $REMOTE_PATH/beta）
echo ""
echo "=== 上传到 ${SSH_USER}@${SERVER}:${SSH_PORT} ${REMOTE_DIR} ==="
SSH="ssh -p $SSH_PORT"
SCP="scp -P $SSH_PORT"
$SSH "$SSH_USER@$SERVER" "mkdir -p '$REMOTE_DIR'"
$SCP "$SETUP_FILE" "$SSH_USER@$SERVER:$REMOTE_DIR/"
$SCP "$SETUP_FILE.blockmap" "$SSH_USER@$SERVER:$REMOTE_DIR/"
$SCP "$DIST_DIR/$YML_FILE" "$SSH_USER@$SERVER:$REMOTE_DIR/"
$SCP "$DIST_DIR/history.json" "$SSH_USER@$SERVER:$REMOTE_DIR/"

BASE_URL="http://${SERVER}:20152/releases"
if [ "$CHANNEL" = "beta" ]; then
    CHECK_URL="$BASE_URL/beta"
else
    CHECK_URL="$BASE_URL"
fi

echo ""
echo "=== 线上校验 ==="
curl -fsS "$CHECK_URL/$YML_FILE" | head -5
curl -fsS "$CHECK_URL/history.json" | head -5
curl -fsSI "$SETUP_FILE" >/dev/null 2>&1 || true
echo "✅ v$RELEASE_VERSION 发布完成：$CHECK_URL"

#!/bin/bash
# 一键打包 + 上传到更新服务器
# 用法: bash release.sh

set -e

SERVER="47.101.175.39"
PORT="20152"
REMOTE_PATH="/home/developer01/dljy/spider-gd/releases"

cd "$(dirname "$0")"

# 1. 读取版本号
VERSION=$(node -p "require('./frontend/package.json').version")
echo "📦 版本: v$VERSION"
echo ""

# 2. 编译后端
echo "=== 编译后端 ==="
python build_backend.py --with-browsers
echo ""

# 3. 编译前端
echo "=== 编译前端 ==="
cd frontend
pnpm build:win
cd ..
echo ""

# 4. 上传
echo "=== 上传到 $SERVER ==="
EXE_FILE=$(ls frontend/dist/广东电力数据采集-${VERSION}-setup*.exe 2>/dev/null | head -1)
BLOCKMAP_FILE=$(ls frontend/dist/广东电力数据采集-${VERSION}-setup*.exe.blockmap 2>/dev/null | head -1)

if [ ! -f "$EXE_FILE" ]; then
    echo "❌ 未找到安装包: frontend/dist/广东电力数据采集-${VERSION}-setup*.exe"
    exit 1
fi

echo "  latest.yml"
scp -P $PORT frontend/dist/latest.yml developer01@${SERVER}:${REMOTE_PATH}/

echo "  $(basename "$EXE_FILE")"
scp -P $PORT "$EXE_FILE" developer01@${SERVER}:${REMOTE_PATH}/

if [ -f "$BLOCKMAP_FILE" ]; then
    echo "  $(basename "$BLOCKMAP_FILE")"
    scp -P $PORT "$BLOCKMAP_FILE" developer01@${SERVER}:${REMOTE_PATH}/
fi

echo ""
echo "✅ v$VERSION 发布完成！"
echo "   验证: curl http://${SERVER}:${PORT}/releases/latest.yml"

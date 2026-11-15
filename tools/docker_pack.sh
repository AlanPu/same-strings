#!/usr/bin/env bash
#
# 交叉构建 x86_64 镜像并导出成 tar.gz，供 NAS 侧 docker load。
#
# 为什么需要这个脚本：这台 Mac 是 arm64，直接 docker build 出来的是 arm64 镜像，
# 丢到 x86 的 NAS 上会报 "exec format error"。NAS 上自己 build 当然是原生的，
# 但把 2 GB 构建上下文传过去更慢，所以在本机交叉构建更省事。
#
# 用法：
#   tools/docker_pack.sh                      # linux/amd64，输出到 dist/docker/
#   PLATFORM=linux/arm64 tools/docker_pack.sh # 临时要 arm64 时
#   TAG=my/same-strings:v1 tools/docker_pack.sh
#
# 依赖：Docker Desktop（自带 buildx 与 QEMU 模拟）。基础镜像 nginx:1.27-alpine
# 本身是多架构的，Dockerfile 里只有一条 rm，所以交叉构建不需要编译，很快。
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

PLATFORM="${PLATFORM:-linux/amd64}"
ARCH="${PLATFORM#linux/}"
# 默认 tag 必须和 docker-compose.yml 里的 image 一致，否则 NAS 上
# `docker compose up --no-build` 找不到镜像，又回去 build 一次。
TAG="${TAG:-same-strings:latest}"
OUT_DIR="${OUT_DIR:-dist/docker}"
OUT="$OUT_DIR/same-strings-${ARCH}.tar.gz"

fail() { echo "  ✗ $*" >&2; exit 1; }

echo "== 预检 =="
command -v docker >/dev/null 2>&1 || fail "找不到 docker。这台机器上 Docker Desktop 还没装。"
docker info >/dev/null 2>&1 || fail "docker 守护进程没起来，先启动 Docker Desktop。"
docker buildx version >/dev/null 2>&1 || fail "没有 buildx，交叉构建需要它（Docker 19.03+ 自带）。"
[ -f dist/site/index.html ] || fail "缺少 dist/site/index.html，先跑 .venv/bin/python build.py"
echo "  ✓ docker / buildx 就绪"
echo "  ✓ 构建上下文 $(du -sh dist/site | cut -f1)（音频外置，参照页已排除）"

echo "== 构建 $PLATFORM =="
# --load：单平台构建直接装进本地镜像库，才能接着 docker save
docker buildx build \
  --platform "$PLATFORM" \
  --tag "$TAG" \
  --load \
  .

# 交叉构建最容易出的错是「以为构建了 amd64，其实还是宿主架构」。
# 这里直接问镜像本身，而不是相信命令行参数。
GOT="$(docker image inspect --format '{{.Os}}/{{.Architecture}}' "$TAG")"
echo "  镜像架构：$GOT"
[ "$GOT" = "$PLATFORM" ] || fail "构建产物架构是 ${GOT}，不是 ${PLATFORM}"

# 另打一个带架构名的 tag，方便本机同时留着 arm64/amd64 两份对照
docker tag "$TAG" "same-strings:${ARCH}"

echo "== 导出 =="
mkdir -p "$OUT_DIR"
docker save "$TAG" | gzip -9 > "$OUT"
SIZE="$(du -h "$OUT" | cut -f1)"
if command -v shasum >/dev/null 2>&1; then
  SHA="$(shasum -a 256 "$OUT" | cut -d' ' -f1)"
else
  SHA="$(sha256sum "$OUT" | cut -d' ' -f1)"
fi
# 注意：变量后面紧跟全角字符时必须写 ${VAR}。写成 $OUT（ 时，在某些 locale 下
# bash 会把多字节字符当成变量名的一部分，报 "OUT: unbound variable"。
echo "  ${OUT}（${SIZE}）"
echo "  sha256 $SHA"

cat <<EOF

== 下一步 ==
1) 传到 NAS（换成你的共享目录）：
   rsync -av --partial "$OUT" admin@nas:/volume1/docker/same-strings/

2) NAS 上导入：
   gunzip -c same-strings-${ARCH}.tar.gz | docker load
   docker image inspect --format '{{.Os}}/{{.Architecture}}' $TAG

3) 起容器（REF_DIR 填参照页目录；镜像已在本地，compose 不会再 build）：
   REF_DIR=/volume1/docker/same-strings/ref docker compose up -d --no-build

注意：compose 里默认 platform 是 linux/amd64，本地想跑 arm64 副本时用
   TARGET_PLATFORM=linux/arm64 docker compose up -d
EOF
